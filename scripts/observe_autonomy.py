"""Bounded read-only autonomy observation; no model calls, controls or messages."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def connect(path):
    return closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1))


def snapshot(root, since):
    settings = tomllib.loads((root / 'config/bridge.local.toml').read_text(encoding='utf-8-sig'))['bridge']
    state = root / settings.get('state_root', 'var/groups')
    with connect(state / 'activities.sqlite3') as db:
        db.row_factory = sqlite3.Row
        row = dict(db.execute('SELECT * FROM idle_activity WHERE id=1').fetchone())
    protocol = json.loads(row['protocol'])
    activity = {key: row[key] for key in ('status', 'goal', 'summary', 'next_step',
        'last_check', 'last_chat', 'rest_until', 'updated', 'pause_requested', 'resume_requested')}
    activity.update(protocol_messages=len(protocol.get('messages', [])),
        pending_calls=len(protocol.get('pending_calls', [])),
        uncertain_calls=len(protocol.get('uncertain_calls', [])),
        receipts_count=len(json.loads(row['receipts'])))
    with connect(state / 'activities.sqlite3') as db:
        wake = db.execute("SELECT due_at FROM activity_wakeups WHERE status='scheduled' "
            'AND task_id=? ORDER BY due_at LIMIT 1', (protocol.get('activity_id', ''),)).fetchone()
    activity['next_explicit_wakeup'] = wake[0] if wake else None
    with connect(state / 'direct-harness.sqlite3') as db:
        tools = dict(db.execute("SELECT state,COUNT(*) FROM calls WHERE activity!='conversation' "
            'AND updated>=? GROUP BY state', (since,)))
    calls, total_tokens = 0, 0
    since_iso = datetime.fromtimestamp(since, timezone.utc).isoformat()
    for path in state.glob('*/runtime.sqlite3'):
        with connect(path) as db:
            count, tokens = db.execute("SELECT COUNT(*),SUM(json_extract(payload,'$.usage.total_tokens')) "
                "FROM usage WHERE json_extract(payload,'$.purpose')='autonomous_activity' "
                "AND json_extract(payload,'$.recorded_at')>=?", (since_iso,)).fetchone()
            calls += count
            total_tokens += tokens or 0
    notes = []
    for folder in (root / 'notes', root / 'var/harness/runtime/notes'):
        for path in folder.glob('*.md'):
            notes.append({'path': str(path.relative_to(root)), 'bytes': path.stat().st_size,
                'modified_at': path.stat().st_mtime, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return {'observed_at': datetime.now(timezone.utc).isoformat(), 'activity': activity,
        'new_activity_model_calls': calls, 'new_activity_tokens': total_tokens,
        'new_activity_tools': tools, 'notes': notes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=900)
    parser.add_argument('--interval', type=int, default=15)
    parser.add_argument('--since', type=float)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.seconds < 0 or args.interval < 1:
        parser.error('seconds must be nonnegative and interval positive')
    args.output.mkdir(parents=True, exist_ok=True)
    started = time.time()
    previous = None
    while True:
        try:
            row = snapshot(ROOT, args.since if args.since is not None else started)
            fingerprint = {k: v for k, v in row.items() if k != 'observed_at'}
            if fingerprint != previous:
                with (args.output / 'observations.jsonl').open('a', encoding='utf-8') as file:
                    file.write(json.dumps(row, ensure_ascii=False) + '\n')
                print(json.dumps({key: row[key] for key in ('observed_at', 'new_activity_model_calls',
                    'new_activity_tokens', 'new_activity_tools')} | {'status': row['activity']['status'],
                    'receipts': row['activity']['receipts_count'], 'rest_until': row['activity']['rest_until']},
                    ensure_ascii=False), flush=True)
                previous = fingerprint
            (args.output / 'latest.json').write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding='utf-8')
        except (OSError, sqlite3.Error) as error:
            print(json.dumps({'read_error': type(error).__name__}), flush=True)
        remaining = args.seconds - (time.time() - started)
        if remaining <= 0:
            break
        time.sleep(min(args.interval, remaining))


if __name__ == '__main__':
    main()
