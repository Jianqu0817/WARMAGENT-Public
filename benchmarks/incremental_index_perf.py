"""Three short synthetic comparisons; never read production archives or APIs."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import statistics
import tempfile
import time

import numpy as np
from threadpoolctl import threadpool_limits

from myatri.contracts import Event, Record, SourceSpan
from myatri.memory.graph import MemoryGraph
from myatri.memory.index import MemoryIndex
from myatri.memory.vector_index import ExactVectorIndex
from myatri.retrieval import RecallService
from myatri.storage import ArchiveStore
from warmagent.incremental_index import IncrementalMemoryIndex
from warmagent.index_graph import FastMemoryGraph
from warmagent.context_read_view import ContextReadView


def records(count):
    rows = []
    for i in range(count):
        text = f'星河第{i}天整理书架，把第{i % 17}本蓝色笔记放到第{i % 9}层。'
        raw = Record(record_id=f'raw-{i}', record_kind='raw_message', agent_id='perf', user_id='synthetic',
            author_kind='human', body_markdown=text, payload={'role': 'user'})
        event = Record(record_id=f'event-{i}', record_kind='event', agent_id='perf', user_id='synthetic',
            author_kind='system', source_refs=[raw.ref], body_markdown=text,
            payload=Event(occurred_at=None, participants=['星河'], fact_summary=text,
                source_spans=[SourceSpan(message_ref=raw.ref, start=0, end=len(text), quote=text)],
                event_fingerprint=f'fixture-{i}', reality_scope='reported').model_dump())
        rows.extend((raw, event))
    return rows


async def measure_updates(workspace, count):
    seed = workspace / 'seed'
    store = ArchiveStore(seed, agent_id='perf', user_id='synthetic')
    store.commit(records(count), idempotency_key='base')
    await MemoryIndex(store).rebuild()
    results = []
    for repeat in range(3):
        for variant in ('full', 'incremental'):
            root = workspace / f'{variant}-{repeat}'
            shutil.copytree(seed, root)
            current = ArchiveStore(root, agent_id='perf', user_id='synthetic')
            index = MemoryIndex(current)
            if variant == 'incremental':
                index = IncrementalMemoryIndex.adapt(index, merge_delay_seconds=3600)
            new = Record(record_id='raw-new', record_kind='raw_message', agent_id='perf', user_id='synthetic',
                author_kind='human', body_markdown='星河把蓝色备用钥匙放在第七层书架。', payload={'role': 'user'})
            current.commit([new], idempotency_key='append')
            try:
                start = time.perf_counter()
                await index.update()
                update_ms = (time.perf_counter() - start) * 1000
                start = time.perf_counter()
                # Production GroupRecall pins this same validated view; avoid
                # measuring extra historical filesystem scans absent there.
                bundle = RecallService(ContextReadView(current), index).recall('蓝色备用钥匙', budget=4000)
                recall_ms = (time.perf_counter() - start) * 1000
                assert new.ref in {row.ref for row in bundle.records}
                results.append({'repeat': repeat + 1, 'variant': variant, 'update_ms': update_ms,
                    'recall_ms': recall_ms, 'update_and_visible_ms': update_ms + recall_ms,
                    'full_generation_current': index.watermark == current.snapshot()})
            finally:
                if isinstance(index, IncrementalMemoryIndex):
                    await index.close()
    return results


def measure_graph(count=300, dimension=64):
    matrix = np.random.default_rng(17).normal(size=(count, dimension))
    rows = [{'ref': f'e{i}@1', 'space_id': 'fixture', 'view': 'event', 'vector': vector.tolist()}
            for i, vector in enumerate(matrix)]
    index = ExactVectorIndex('fixture', dimension, rows)
    nodes = [row['ref'] for row in rows]
    results = []
    reference = None
    for variant, graph_type in (('full', MemoryGraph), ('blocked', FastMemoryGraph)):
        for repeat in range(4):
            graph = graph_type(nodes)
            start = time.perf_counter()
            graph.upsert_similarity_edges({'event': index})
            elapsed = (time.perf_counter() - start) * 1000
            keys = {(edge['from_ref'], edge['to_ref']) for edge in graph.edges}
            if reference is None:
                reference = keys
            assert keys == reference
            if repeat:
                results.append({'repeat': repeat, 'variant': variant, 'graph_ms': elapsed})
    return results


def summary(rows, field):
    return {variant: {'n': 3, 'mean_ms': statistics.mean(r[field] for r in rows if r['variant'] == variant),
                     'raw_ms': [r[field] for r in rows if r['variant'] == variant]}
            for variant in dict.fromkeys(r['variant'] for r in rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--records', type=int, default=240)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='warm-index-perf-') as directory, threadpool_limits(limits=1):
        updates = asyncio.run(measure_updates(Path(directory), args.records))
        graphs = measure_graph()
    sources = ['src/warmagent/incremental_index.py', 'src/warmagent/embedding_cache.py',
               'src/warmagent/index_graph.py', 'src/warmagent/index_builder.py', 'src/warmagent/index_worker.py']
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'records': args.records,
        'boundary': 'synthetic raw/event pairs, TF-IDF, verified read view; incremental full merge deferred, not eliminated',
        'updates': updates, 'graphs': graphs, 'update_summary': summary(updates, 'update_ms'),
        'visible_summary': summary(updates, 'update_and_visible_ms'), 'graph_summary': summary(graphs, 'graph_ms'),
        'source_sha256': {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in sources}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('update_summary', 'visible_summary', 'graph_summary')}, indent=2))


if __name__ == '__main__':
    main()
