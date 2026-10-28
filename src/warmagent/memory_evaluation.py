"""Offline annotated recall evaluation. Never loads channel or model credentials."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import statistics
import tempfile
import time

import numpy as np
from threadpoolctl import threadpool_limits

from myatri.contracts import Record, digest
from myatri.memory.index import MemoryIndex
from myatri.storage import ArchiveStore
from .compact_index import CompactMemoryIndex
from .group_memory import GroupRecall
from .identity import IdentityRegistry


def score_case(case, wide, actual, *, k=5):
    """Rank only primary candidates; attached original evidence is not a rank."""
    expected, forbidden = set(case.get('expected_refs', [])), set(case.get('forbidden_refs', []))
    groups = {ref: set(case.get('evidence_groups', {}).get(ref, [ref])) | {ref} for ref in expected}
    def primary(bundle):
        return [r.ref for r in bundle.records if not any(reason.startswith('original source for ')
            for reason in bundle.reasons.get(r.ref, []))]
    order = primary(wide)
    emitted = {r.ref for r in actual.records}
    diagnostics = []
    for ref in sorted(expected):
        equivalents = groups[ref]
        ranks = [order.index(item) for item in equivalents if item in order]
        if not ranks:
            diagnostics.append({'ref': ref, 'stage': 'unretrieved'})
        elif min(ranks) >= k:
            diagnostics.append({'ref': ref, 'stage': 'rank_below_k'})
        elif not equivalents & emitted:
            diagnostics.append({'ref': ref, 'stage': 'budget_pruned'})
        elif 'answer_citations' in case and not equivalents & set(case['answer_citations']):
            diagnostics.append({'ref': ref, 'stage': 'answer_unused'})
    gold_records = [r for r in actual.records if r.ref in expected]
    missing_sources = sorted({ref for r in gold_records for ref in r.source_refs if ref not in emitted})
    violations = sorted(forbidden & emitted)
    first = next((i + 1 for i, ref in enumerate(order) if ref in expected), None)
    metrics = {f'recall_at_{limit}': len(expected & set(order[:limit])) / len(expected) if expected else None
        for limit in (1, 3, 5)}
    metrics.update({f'evidence_recall_at_{limit}': sum(bool(group & set(order[:limit])) for group in groups.values()) / len(groups)
        if groups else None for limit in (1, 3, 5)})
    metrics['evidence_presented_recall'] = sum(bool(group & emitted) for group in groups.values()) / len(groups) if groups else None
    dcg = sum(1 / math.log2(rank + 2) for rank, ref in enumerate(order[:k]) if ref in expected)
    ideal = sum(1 / math.log2(rank + 2) for rank in range(min(k, len(expected))))
    return {'id': case['id'], 'category': case.get('category', 'unspecified'), **metrics,
        'mrr': 1 / first if first else 0.0 if expected else None,
        'ndcg_at_k': dcg / ideal if ideal else None,
        'presented_recall': len(expected & emitted) / len(expected) if expected else None,
        'wide_primary_order': order, 'presented_refs': sorted(emitted),
        'source_gaps': missing_sources, 'forbidden_returned': violations,
        'answer_evaluated': 'answer_citations' in case, 'diagnostics': diagnostics,
        'channels': {ref: wide.reasons.get(ref, []) for ref in expected},
        'limitations': actual.limitations}


def evaluate(dataset, root, *, index_type=CompactMemoryIndex, provider=None, repeats=1):
    if repeats not in (1, 2, 3):
        raise ValueError('repeats must be 1, 2 or 3')
    records = [Record.model_validate(row) for row in dataset['records']]
    store = ArchiveStore(Path(root), records[0].agent_id, records[0].user_id)
    snapshots = {}
    for phase in sorted({row.payload.get('evaluation_phase', 0) for row in records}):
        group = [row for row in records if row.payload.get('evaluation_phase', 0) == phase]
        expected = {row.record_id: row.revision - 1 for row in group}
        store.commit(group, f'eval-{phase}', expected=expected)
        snapshots[str(phase)] = store.snapshot()
    ids = IdentityRegistry(Path(root) / 'eval-identities.sqlite3', owner_id=dataset.get('owner_id', 'owner'),
        owner_name=dataset.get('owner_name', '星河'))
    for record in records:
        source = record.payload.get('channel_source')
        if source:
            scope = digest([source.get(key) for key in ('platform_id', 'bot_id', 'group_id')])[:32]
            ids.observe(scope, source, source_id=record.ref)
    index = index_type(store, provider)
    start = time.perf_counter()
    asyncio.run(index.rebuild())
    build_ms = (time.perf_counter() - start) * 1000
    cases = []
    for case in dataset['queries']:
        scope = digest(case.get('scope', ['fixture', 'bot', 'room']))[:32]
        recall = GroupRecall(store, index, ids, scope, scope_index_cache_enabled=True)
        recall.current_sender = case.get('sender_id', dataset.get('owner_id', 'owner'))
        recall.current_owner = recall.current_sender == dataset.get('owner_id', 'owner')
        recall.shared_visibility = True
        recall.private_channel = case.get('private', False)
        snapshot = snapshots[str(case['phase'])] if 'phase' in case else None
        wide = recall.recall(case['query'], case.get('filters'), snapshot, budget=10_000_000)
        elapsed = []
        for _ in range(repeats):
            start = time.perf_counter()
            actual = recall.recall(case['query'], case.get('filters'), snapshot, budget=case.get('budget', 4000))
            elapsed.append((time.perf_counter() - start) * 1000)
        result = score_case(case, wide, actual)
        result.update(latency_ms=elapsed, mean_ms=statistics.mean(elapsed))
        cases.append(result)
    numeric = ('recall_at_1', 'recall_at_3', 'recall_at_5', 'mrr', 'ndcg_at_k', 'presented_recall',
        'evidence_recall_at_1', 'evidence_recall_at_3', 'evidence_recall_at_5', 'evidence_presented_recall')
    summary = {key: statistics.mean(row[key] for row in cases if row[key] is not None)
        for key in numeric if any(row[key] is not None for row in cases)}
    latencies = sorted(value for row in cases for value in row['latency_ms'])
    summary.update(cases=len(cases), records=len(records), build_ms=build_ms,
        p50_ms=statistics.median(latencies), p95_ms=float(np.percentile(latencies, 95)),
        source_gaps=sum(len(row['source_gaps']) for row in cases),
        forbidden_returned=sum(len(row['forbidden_returned']) for row in cases),
        answer_cases=sum(row['answer_evaluated'] for row in cases))
    files = list((index.path / 'generations').glob(f'{index.generation}.*'))
    summary['generation_bytes'] = sum(path.stat().st_size for path in files)
    return {'schema_version': 1, 'dataset_id': dataset['id'], 'dataset_sha256': digest(dataset),
        'variant': index_type.__name__, 'provider': getattr(provider, 'model', 'local-tfidf'),
        'summary': summary, 'cases': cases,
        'measurement': 'Offline full-corpus GroupRecall; wide-budget order includes graph expansion. No answer model calls.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, choices=(1, 2, 3), default=1)
    parser.add_argument('--embedding-path', type=Path)
    args = parser.parse_args()
    dataset = json.loads(args.dataset.read_text(encoding='utf-8'))
    provider = None
    if args.embedding_path:
        from myatri.providers.local_onnx import LocalOnnxEmbeddingProvider
        from .bounded_embeddings import BoundedOnnxEmbeddingProvider
        provider = BoundedOnnxEmbeddingProvider(LocalOnnxEmbeddingProvider(args.embedding_path))
    reports = []
    with tempfile.TemporaryDirectory(prefix='warm-memory-eval-') as work, threadpool_limits(limits=1):
        for variant in (MemoryIndex, CompactMemoryIndex):
            reports.append(evaluate(dataset, Path(work) / variant.__name__, index_type=variant,
                provider=provider, repeats=args.repeats))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps([{'variant': row['variant'], **row['summary']} for row in reports], ensure_ascii=False))


if __name__ == '__main__':
    main()
