"""Three offline paired archive-read trials against the copied source commit."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import tempfile
import time
import tracemalloc
import types

from myatri.contracts import Record
from myatri.storage.archive import ArchiveStore


BASELINE = 'benchmarks/baselines/archive_before.py'


def original_type():
    raw = (Path(__file__).resolve().parent / 'baselines/archive_before.py').read_bytes()
    module = types.ModuleType('archive_before_selective_copy')
    exec(compile(raw, '<copied-archive-baseline>', 'exec'), module.__dict__)
    return module.ArchiveStore, hashlib.sha256(raw).hexdigest()


def seed(store, count):
    for start in range(0, count, 100):
        rows = [Record(record_id=f'row_{i}', record_kind='cognitive_run' if i % 10 == 0 else 'raw_message',
            agent_id='perf', user_id='synthetic', author_kind='system',
            created_at='2026-10-28T00:00:00+00:00', body_markdown='合成资料' * 100,
            payload={'role': 'user', 'nested': {'numbers': list(range(24)), 'labels': ['合成', str(i)]}})
            for i in range(start, min(start + 100, count))]
        store.commit(rows, f'seed_{start}')


def measure(function, iterations):
    started = time.perf_counter()
    for _ in range(iterations):
        function()
    return (time.perf_counter() - started) * 1000 / iterations


def benchmark(count=1600, iterations=3):
    baseline_type, source_hash = original_type()
    with tempfile.TemporaryDirectory(prefix='warmagent-perf-archive-') as directory:
        optimized = ArchiveStore(Path(directory), 'perf', 'synthetic')
        seed(optimized, count)
        baseline = baseline_type(Path(directory), 'perf', 'synthetic')
        expected = baseline.get_record('row_0').model_dump()
        assert optimized.get_record('row_0').model_dump() == expected
        assert baseline.snapshot() == optimized.snapshot()
        assert [r.model_dump() for r in baseline.list_records('cognitive_run')] == [
            r.model_dump() for r in optimized.list_records('cognitive_run')]
        tasks = {
            'get_record': lambda s: s.get_record('row_0'),
            'snapshot': lambda s: s.snapshot(),
            'filtered_list_10_percent': lambda s: s.list_records('cognitive_run'),
            'chat_local_read_slice': lambda s: (s.snapshot(), s.get_record('row_0'),
                s.get_record('row_1'), s.get_record('row_2'), s.list_records('cognitive_run')),
            'pinned_local_read_phase': lambda s: read_phase(s),
        }
        rows = []
        for name, operation in tasks.items():
            operation(baseline)
            operation(optimized)
            values = {'baseline': [], 'optimized': []}
            for repeat in range(3):
                order = [('baseline', baseline), ('optimized', optimized)]
                if repeat % 2:
                    order.reverse()
                for variant, store in order:
                    values[variant].append(measure(lambda: operation(store), iterations))
            old, new = (statistics.mean(values[key]) for key in ('baseline', 'optimized'))
            rows.append({'operation': name, 'iterations_per_trial': iterations, 'trials': 3,
                'baseline_ms': values['baseline'], 'optimized_ms': values['optimized'],
                'baseline_mean_ms': old, 'optimized_mean_ms': new, 'ratio': new / old,
                'speedup': old / new})
        allocations = {}
        for name, store in [('baseline', baseline), ('optimized', optimized)]:
            tracemalloc.start()
            store.get_record('row_0')
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            allocations[name + '_get_record_peak_bytes'] = peak
        return {'fixture_records': count, 'fixture_manifests': (count + 99) // 100,
            'baseline_source': BASELINE, 'baseline_sha256': source_hash,
            'optimized_sha256': hashlib.sha256(Path('src/myatri/storage/archive.py').read_bytes()).hexdigest(),
            'contract': 'same archive, full fingerprint invalidation retained, selective copies remain independent',
            'scope': 'warm local reads; not model latency, cold startup, full memory consolidation or real WeChat',
            'results': rows, 'allocations': allocations}


def read_phase(store):
    reader = store.read_view() if isinstance(store, ArchiveStore) else store
    return reader.snapshot(), reader.get_record('row_0'), reader.get_record('row_1'), reader.get_record('row_2'), reader.list_records('cognitive_run')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--records', type=int, default=1600)
    parser.add_argument('--output', type=Path, default=Path('reports/perf/archive-lab.json'))
    args = parser.parse_args()
    report = benchmark(args.records)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
