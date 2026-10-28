"""Local-only full index merge process; no model API or channel startup."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from threadpoolctl import threadpool_limits

from myatri.memory.embeddings import LocalTfidfEmbeddingProvider
from myatri.providers.local_onnx import LocalOnnxEmbeddingProvider
from myatri.storage import ArchiveStore

from .bounded_embeddings import BoundedOnnxEmbeddingProvider
from .embedding_cache import cache_provider
from .index_builder import GraphMemoryIndex


class IndexReadStore(ArchiveStore):
    def recover(self):
        # The online process owns FTS/projection recovery. A derived-index
        # worker validates immutable archives without rewriting that database.
        self._load(force=True)


class CachedBuildIndex(GraphMemoryIndex):
    def _prepare_rebuild(self):
        prepared = super()._prepare_rebuild()
        for plan in prepared['plans']:
            # Each TF-IDF fit creates a new space. Persisting its entire dense
            # corpus again would duplicate the full generation without reuse.
            if isinstance(plan['provider'], LocalTfidfEmbeddingProvider):
                continue
            plan['provider'] = cache_provider(plan['provider'], self.path / 'incremental-vectors.sqlite3',
                plan['view'], self.cache_entries)
        return prepared


def build(request):
    store = IndexReadStore(Path(request['archive_root']), request['agent_id'], request['user_id'])
    spec = request['provider']
    if spec['backend'] == 'bge':
        provider = BoundedOnnxEmbeddingProvider(LocalOnnxEmbeddingProvider(Path(spec['path'])))
    elif spec['backend'] == 'tfidf':
        provider = None
    else:
        raise ValueError('Only local embedding providers are allowed in the index worker')
    index = CachedBuildIndex(store, provider, **request['params'])
    index.cache_entries = request['cache_entries']
    with threadpool_limits(limits=1):
        return asyncio.run(index.rebuild())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    args = parser.parse_args()
    build(json.loads(args.request.read_text(encoding='utf-8')))


if __name__ == '__main__':
    main()
