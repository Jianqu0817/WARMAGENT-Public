"""Cache current delta vectors; coalesce full generations in a local worker.

The frozen RecallService already merges base vectors with current delta records.
Keep its watermark gate intact: a pending generation never borrows old themes.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from myatri.memory.embeddings import LocalTfidfEmbeddingProvider
from myatri.memory.snapshot import read_snapshot
from myatri.providers.local_onnx import LocalOnnxEmbeddingProvider
from myatri.storage.archive import _atomic_write, _json

from .dsh_client import CREATION_FLAGS
from .embedding_cache import cache_provider
from .compact_index import CompactMemoryIndex

logger = logging.getLogger(__name__)


def _local_provider_spec(provider):
    while hasattr(provider, '_provider'):
        provider = provider._provider
    if provider is None or isinstance(provider, LocalTfidfEmbeddingProvider):
        return {'backend': 'tfidf'}
    if isinstance(provider, LocalOnnxEmbeddingProvider):
        return {'backend': 'bge', 'path': str(provider.path.resolve())}
    raise ValueError('Incremental worker supports local TF-IDF and local ONNX only')


class IncrementalMemoryIndex(CompactMemoryIndex):
    @classmethod
    def adapt(cls, index, *, merge_delay_seconds=30, cache_entries=10000):
        if isinstance(index, cls):
            return index
        if type(merge_delay_seconds) not in (int, float) or not 0 <= merge_delay_seconds <= 3600:
            raise ValueError('merge_delay_seconds must be between 0 and 3600')
        adapted = cls.__new__(cls)
        adapted.__dict__.update(index.__dict__)
        adapted._compact_enabled = isinstance(index, CompactMemoryIndex)
        adapted._provider_spec = _local_provider_spec(index.provider)
        adapted.merge_delay = merge_delay_seconds
        adapted.cache_entries = cache_entries
        adapted.vector_cache_path = index.path / 'incremental-vectors.sqlite3'
        adapted.request_path = index.path / 'incremental-request.json'
        adapted._update_lock = asyncio.Lock()
        adapted._merge_task = None
        adapted._merge_now = asyncio.Event()
        adapted._progress = asyncio.Event()
        adapted._completed_builds = 0
        adapted._closed = False
        adapted._force = False
        adapted._target = {'watermark': index.watermark, 'sequence': index._sequence}
        adapted.merge_state = 'idle'
        adapted.merge_error = None
        adapted._wrap_providers()
        return adapted

    def _wrap_providers(self):
        with self._state_lock:
            self.providers = {view: cache_provider(provider, self.vector_cache_path, view,
                self.cache_entries) for view, provider in self.providers.items() if provider is not None}

    def _warm_delta(self, snapshot, installed):
        # Ref includes revision. A revised record cannot reuse its old row as a
        # current record; text-identical vectors may still hit the content cache.
        for view, provider in installed['providers'].items():
            known = {row['ref'] for row in installed['indexes'][view].rows}
            texts = [self._texts(record).get(view, '') for record in snapshot.latest if record.ref not in known]
            texts = [text for text in texts if text.strip()]
            if texts:
                provider.transform(texts, installed['indexes'][view].space_id)

    def _persist_request(self):
        request = {'schema_version': 1, 'archive_root': str(self.store.root),
            'agent_id': self.store.agent_id, 'user_id': self.store.user_id,
            'provider': self._provider_spec, 'cache_entries': self.cache_entries,
            'compact_format': self._compact_enabled,
            'params': {'k': self.k, 'threshold': self.threshold, 'seed': self.seed},
            'base_generation': self.generation, 'base_watermark': self.watermark,
            'target': self._target, 'state': self.merge_state, 'error': self.merge_error}
        _atomic_write(self.request_path, _json(request))

    async def update(self):
        if self._closed:
            raise RuntimeError('index coordinator is closed')
        async with self._update_lock:
            snapshot = await asyncio.to_thread(read_snapshot, self.store)
            self._target = {'watermark': snapshot.watermark, 'sequence': snapshot.sequence}
            installed = self.capture_state()
            if installed['generation'] and installed['watermark'] == snapshot.watermark and not self._force:
                return installed['generation']
            await asyncio.to_thread(self._warm_delta, snapshot, installed)
            self.merge_state, self.merge_error = 'pending', None
            await asyncio.to_thread(self._persist_request)
            if self._merge_task is None or self._merge_task.done():
                self._merge_task = asyncio.create_task(self._merge_loop())
            return installed['generation']

    async def _build_once(self):
        # This child only reads original records and publishes derived indexes.
        # No chat configuration, API credentials, login or message code is loaded.
        env = dict(os.environ)
        env.update(OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1', PYTHONUTF8='1')
        with (self.path / 'incremental-worker.stderr.log').open('ab') as errors:
            process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'warmagent.index_worker',
                '--request', str(self.request_path), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=errors, env=env,
                creationflags=CREATION_FLAGS)
            try:
                code = await process.wait()
                if code:
                    raise RuntimeError('local index worker failed; inspect incremental-worker.stderr.log')
            except asyncio.CancelledError:
                if process.returncode is None:
                    process.terminate()
                    await process.wait()
                raise
        async with self._update_lock:
            await asyncio.to_thread(self._load_current_monotone)
            self._wrap_providers()

    async def _merge_loop(self):
        try:
            while not self._closed:
                if self.generation and not self._merge_now.is_set():
                    try:
                        await asyncio.wait_for(self._merge_now.wait(), self.merge_delay)
                    except TimeoutError:
                        pass
                self._merge_now.clear()
                self.merge_state, self._force = 'building', False
                await self._build_once()
                self._completed_builds += 1
                # Writes made while the worker was running remain pending;
                # never label the latest archive covered by an older result.
                snapshot = await asyncio.to_thread(read_snapshot, self.store)
                if snapshot.sequence >= self._target['sequence']:
                    self._target = {'watermark': snapshot.watermark, 'sequence': snapshot.sequence}
                if self.watermark == snapshot.watermark and not self._force:
                    self.merge_state, self.merge_error = 'idle', None
                    await asyncio.to_thread(self._persist_request)
                    self._notify_progress()
                    return
                self.merge_state = 'pending'
                await asyncio.to_thread(self._persist_request)
                self._notify_progress()
        except asyncio.CancelledError:
            self.merge_state = 'pending'
            await asyncio.to_thread(self._persist_request)
            raise
        except Exception as error:
            self.merge_state, self.merge_error = 'failed', type(error).__name__
            await asyncio.to_thread(self._persist_request)
            logger.warning('Index merge failed: %s; original records retained', type(error).__name__)
        finally:
            self._notify_progress()

    def _notify_progress(self):
        previous, self._progress = self._progress, asyncio.Event()
        previous.set()

    async def rebuild(self):
        """Explicit full merge barrier, used by deep maintenance and offline QA."""
        completed = self._completed_builds
        self._force = True
        await self.update()
        target_sequence = self._target['sequence']
        self._merge_now.set()
        # Wait for this caller's cutoff, not perpetual future chat writes.
        # The same background task can keep merging those later records.
        while self._completed_builds <= completed or self._sequence < target_sequence:
            progress = self._progress
            if self._closed or self.merge_error or self._merge_task.done():
                raise RuntimeError('index merge failed or coordinator closed')
            await progress.wait()
        self._force = False
        return self.generation

    async def close(self):
        self._closed = True
        if self._merge_task is not None and not self._merge_task.done():
            self._merge_task.cancel()
            await asyncio.gather(self._merge_task, return_exceptions=True)

    def inspect(self):
        return {**super().inspect(), 'incremental': self.incremental_status()}

    def incremental_status(self):
        return {'state': self.merge_state, 'error': self.merge_error,
            'base_watermark': self.watermark, 'target_watermark': self._target['watermark'],
            'target_sequence': self._target['sequence'], 'merge_delay_seconds': self.merge_delay}


def install_incremental_index(app, options):
    if not options.get('incremental_index_enabled', False):
        return
    index = IncrementalMemoryIndex.adapt(app.index,
        merge_delay_seconds=options.get('index_merge_delay_seconds', 30),
        cache_entries=options.get('embedding_cache_entries', 10000))
    app.index = app.recall.index = app.conversation.index = index
    app.conversation.context.index = app.conversation.context.selector.index = index
