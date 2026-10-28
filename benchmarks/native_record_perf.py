"""Three short paired reads over the existing synthetic M archive."""
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import statistics
import time

from myatri.storage import ArchiveStore
from warmagent.context_read_view import ContextReadView
from benchmarks.reference_read_view import ContextReadView as PythonReadView


ROOT = Path(__file__).resolve().parents[1]


def main():
    run = ROOT / 'reports/perf' / ('native-record-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    run.mkdir(parents=True)
    source = ROOT / 'var/benchmarks/perf-20261025-224806-smoke-n1-M/fixtures/M/archive'
    store = ArchiveStore(source, agent_id='warmagent', user_id='synthetic-shared')
    old, new = PythonReadView(store), ContextReadView(store)
    assert len(old.list_records()) == 2100
    identifiers = [row.record_id for row in old.list_records() if row.record_kind == 'event'][:100]
    values = []
    report = []
    for label, baseline, optimized in [
        ('list_records', old.list_records, new.list_records),
        ('snapshot_copy', old._load, new._load),
        ('get_100_refs', lambda: [old.get_record(identifier) for identifier in identifiers],
         lambda: [new.get_record(identifier) for identifier in identifiers]),
    ]:
        expected = baseline()
        assert optimized() == expected
        times = {'python': [], 'rust_serializer': []}
        for repeat in range(1, 4):
            order = [('python', baseline), ('rust_serializer', optimized)]
            if repeat == 2:
                order.reverse()
            for backend, fn in order:
                start = time.perf_counter_ns()
                result = fn()
                elapsed = (time.perf_counter_ns() - start) / 1e6
                assert result == expected
                values.append({'case': label, 'backend': backend, 'repeat': repeat,
                               'latest_records': 2100, 'wall_ms': elapsed})
                times[backend].append(elapsed)
                with (run / 'raw.csv').open('w', newline='', encoding='utf-8') as stream:
                    writer = csv.DictWriter(stream, fieldnames=values[0].keys())
                    writer.writeheader()
                    writer.writerows(values)
        means = {key: statistics.mean(rows) for key, rows in times.items()}
        ratio = means['rust_serializer'] / means['python']
        report.append({'case': label, 'python_ms': means['python'],
            'rust_serializer_ms': means['rust_serializer'], 'ratio': ratio,
            'under_half': ratio < .5, 'raw': times})
        print(label, means, 'ratio', round(ratio, 3), flush=True)
    (run / 'summary.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    files = ['src/warmagent/record_copy.py', 'src/warmagent/context_read_view.py',
             'benchmarks/reference_read_view.py']
    (run / 'environment.json').write_text(json.dumps({'source': str(source),
        'backend': 'existing pydantic-core Rust serializer + Python model_copy; no new Rust crate',
        'includes': 'entire read-view method including filtering, cloning and Python model copy',
        'excludes': 'authoritative validation and initial view construction, shared by both versions',
        'hashes': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files}},
        indent=2), encoding='utf-8')
    print('REPORT', run, flush=True)


if __name__ == '__main__':
    main()
