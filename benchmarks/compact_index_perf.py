"""Three bounded offline runs including build, verified reload and query costs."""
from __future__ import annotations

import argparse
import asyncio
import gc
import json
from pathlib import Path
import statistics
import tempfile
import time
import tracemalloc

import numpy as np
from threadpoolctl import threadpool_limits

from myatri.contracts import EmbeddingBatch
from myatri.storage import ArchiveStore
from warmagent.compact_index import CompactMemoryIndex
from warmagent.index_builder import GraphMemoryIndex
from .incremental_index_perf import records


class FixedLocalVectors:
    """Deterministic neural-shaped vectors; no model loading or external calls."""
    model, model_revision, space_id = 'fixture-dense', 'v1', 'fixture-512'
    dimension, normalization = 512, 'l2'

    def embed_sync(self, texts, space_id):
        from myatri.contracts import digest
        matrix = np.stack([np.random.default_rng(int(digest(text)[:16], 16)).normal(size=self.dimension) for text in texts])
        matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
        return EmbeddingBatch(vectors=matrix.tolist(), space_id=self.space_id, model=self.model,
            model_revision=self.model_revision, dimension=self.dimension, normalization='l2')

    async def embed(self, texts, space_id):
        return self.embed_sync(texts, space_id)


def run(count=120, repeats=3):
    result = []
    with tempfile.TemporaryDirectory(prefix='warm-compact-perf-') as directory, threadpool_limits(limits=1):
        for backend in ('tfidf', 'dense512'):
            for repeat in range(repeats):
                outputs = []
                for name, index_type in (('legacy', GraphMemoryIndex), ('compact', CompactMemoryIndex)):
                    store = ArchiveStore(Path(directory) / f'{backend}-{name}-{repeat}', 'perf', 'synthetic')
                    store.commit(records(count), 'base')
                    provider = FixedLocalVectors() if backend == 'dense512' else None
                    index = index_type(store, provider)
                    start = time.perf_counter()
                    asyncio.run(index.rebuild())
                    build_ms = (time.perf_counter() - start) * 1000
                    query = '第七层书架上的蓝色笔记'
                    outputs.append(index.query(query, limit=20))
                    size = sum(p.stat().st_size for p in (index.path / 'generations').glob(f'{index.generation}.*'))
                    del index
                    gc.collect()
                    start = time.perf_counter()
                    loaded = index_type(store, provider)
                    reload_ms = (time.perf_counter() - start) * 1000
                    gc.collect()
                    tracemalloc.start()
                    measured = index_type(store, provider)
                    _, peak = tracemalloc.get_traced_memory()
                    tracemalloc.stop()
                    del measured
                    start = time.perf_counter()
                    for _ in range(5):
                        loaded.query(query, limit=20)
                    query_ms = (time.perf_counter() - start) * 1000 / 5
                    result.append({'backend': backend, 'variant': name, 'repeat': repeat + 1,
                        'records': count * 2, 'build_ms': build_ms, 'reload_ms': reload_ms,
                        'query_ms': query_ms, 'generation_bytes': size, 'reload_python_peak_bytes': peak})
                    del loaded
                assert [hit['ref'] for hit in outputs[0]] == [hit['ref'] for hit in outputs[1]]
                np.testing.assert_allclose([hit['score'] for hit in outputs[0]],
                    [hit['score'] for hit in outputs[1]], atol=1e-13)
    means = []
    for backend in ('tfidf', 'dense512'):
        for variant in ('legacy', 'compact'):
            subset = [row for row in result if row['backend'] == backend and row['variant'] == variant]
            means.append({'backend': backend, 'variant': variant, **{key: statistics.mean(row[key] for row in subset)
                for key in ('build_ms', 'reload_ms', 'query_ms', 'generation_bytes', 'reload_python_peak_bytes')}})
    return {'runs': result, 'means': means, 'query_refs_equal': True,
        'notes': 'Warm filesystem; float64 exact retrieval; legacy uses the same FastMemoryGraph. Python peak is not process RSS. Dense512 is synthetic, not BGE inference.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--count', type=int, default=120)
    args = parser.parse_args()
    report = run(args.count)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report['means']))
