"""Checksummed binary vector sidecars; legacy JSON generations remain readable."""
from __future__ import annotations

import hashlib
import io
import json

import numpy as np
from scipy import sparse

from myatri.contracts import digest
from myatri.memory.embeddings import LocalTfidfEmbeddingProvider
from myatri.storage.archive import _atomic_write, _json
from .compact_vectors import CompactVectorIndex, SparseTfidfProvider
from .index_builder import GraphMemoryIndex


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class CompactMemoryIndex(GraphMemoryIndex):
    vector_index_type = CompactVectorIndex

    def _prepare_rebuild(self):
        prepared = super()._prepare_rebuild()
        # External local embeddings are already content-cached in SQLite by
        # CachedBuildIndex. Do not repeat every vector in the generation cache.
        prepared['cache'] = {}
        for plan in prepared['plans']:
            if isinstance(plan['provider'], LocalTfidfEmbeddingProvider):
                plan['provider'] = SparseTfidfProvider.from_state(plan['provider'].state())
            plan.update(cached=[None] * len(plan['items']),
                missing_positions=list(range(len(plan['items']))), stable_identity=False)
        return prepared

    def _write_generation(self, state, indexes):
        packed = {**state, 'schema_version': 'warmagent.compact.v1', 'embedding_cache': {}, 'indexes': {}}
        directory = self.path / 'generations'
        for number, (view, index) in enumerate(indexes.items()):
            matrix = index.raw_matrix
            encoding = 'csr' if sparse.issparse(matrix) else 'npy'
            name = f"{state['generation']}.{number}.{'npz' if encoding == 'csr' else 'npy'}"
            buffer = io.BytesIO()
            if encoding == 'csr':
                sparse.save_npz(buffer, matrix, compressed=False)
            else:
                np.save(buffer, matrix, allow_pickle=False)
            _atomic_write(directory / name, buffer.getvalue())
            packed['indexes'][view] = {'space_id': index.space_id, 'dimension': index.dimension,
                'rows': [dict(row) for row in index.rows],
                'matrix': {'file': name, 'encoding': encoding, 'sha256': file_hash(directory / name)}}
        checksum = digest(packed)
        # Sidecars are immutable and complete before the JSON commit marker;
        # GraphMemoryIndex still publishes current last under archive writer.
        _atomic_write(directory / f"{state['generation']}.json", _json({'data': packed, 'sha256': checksum}))
        return checksum

    def _read_generation(self, generation, pointer=None):
        # Keep the original identity/watermark/sequence/hash verification.
        state = super()._read_generation(generation, pointer)
        if state.get('schema_version') == 'warmagent.compact.v1':
            state['_matrices'] = {view: self._matrix(data, generation) for view, data in state['indexes'].items()}
        return state

    def _matrix(self, data, generation):
        spec = data['matrix']
        name = spec['file']
        if '/' in name or '\\' in name or not name.startswith(generation + '.'):
            raise ValueError('Unsafe vector sidecar')
        path = self.path / 'generations' / name
        if file_hash(path) != spec['sha256']:
            raise ValueError('Vector sidecar checksum mismatch')
        if spec['encoding'] == 'csr':
            matrix = sparse.load_npz(path).tocsr()
        elif spec['encoding'] == 'npy':
            matrix = np.load(path, mmap_mode='r', allow_pickle=False)
        else:
            raise ValueError('Unknown vector encoding')
        if matrix.shape != (len(data['rows']), data['dimension']) or matrix.dtype != np.dtype('float64'):
            raise ValueError('Vector sidecar shape/dtype mismatch')
        return matrix

    def _install(self, state, *, indexes=None, providers=None, monotone=False):
        if state.get('schema_version') == 'warmagent.compact.v1':
            if indexes is None:
                indexes = {view: CompactVectorIndex(data['space_id'], data['dimension'], data['rows'],
                    matrix=state['_matrices'][view]) for view, data in state['indexes'].items()}
            if providers is None:
                providers = {view: SparseTfidfProvider.from_state(data) if data['type'] == 'local_tfidf' else self.provider
                    for view, data in state['providers'].items()}
        super()._install(state, indexes=indexes, providers=providers, monotone=monotone)

    def rollback(self, generation):
        state = self._read_generation(generation)
        if state.get('schema_version') != 'warmagent.compact.v1':
            return super().rollback(generation)
        # Prepare all matrices before changing the active pointer.
        indexes = {view: CompactVectorIndex(data['space_id'], data['dimension'], data['rows'],
            matrix=state['_matrices'][view]) for view, data in state['indexes'].items()}
        envelope = json.loads((self.path / 'generations' / f'{generation}.json').read_text(encoding='utf-8'))
        with self.store._writer():
            _atomic_write(self.path / 'current.json', _json(self._pointer(state, envelope['sha256'])))
            self._install(state, indexes=indexes)
