"""Check tracked public files, optionally against a private external policy."""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_PARTS = {'.venv', 'var', 'notes', 'notebooksForWennuan', 'private', '__pycache__', '.model-desk'}


def git(*args, data=None):
    return subprocess.check_output(['git', *args], cwd=ROOT, input=data)


def scan(policy=None, history=False):
    terms = json.loads(Path(policy).read_text(encoding='utf-8'))['blocked_text'] if policy else []
    needles = set()
    for term in terms:
        if not isinstance(term, str) or not term:
            raise ValueError('Policy entries must be nonempty strings')
        needles.update(term.encode(encoding) for encoding in ('utf-8', 'utf-16le', 'utf-16be'))
        needles.add(json.dumps(term, ensure_ascii=True)[1:-1].encode())
    files = [p for p in git('ls-files', '-z').decode().split('\0') if p]
    violations = []
    for name in files:
        path = Path(name)
        if (set(path.parts) & PRIVATE_PARTS or '.local.' in name or path.suffix in {'.sqlite3', '.db', '.log'}
                or name == '.public-export-policy.json' or path.name.startswith('.env')):
            violations.append({'path': name, 'kind': 'private_path'})
        raw = (ROOT / path).read_bytes()
        if any(needle in raw or needle in name.encode() for needle in needles):
            violations.append({'path': name, 'kind': 'blocked_content'})
    objects = 0
    if history:
        identities = [row.split(b' ', 1)[0] for row in git('rev-list', '--objects', '--all').splitlines()]
        stream = io.BytesIO(git('cat-file', '--batch', data=b'\n'.join(identities) + b'\n'))
        for identity in identities:
            header = stream.readline().split()
            raw = stream.read(int(header[2]))
            stream.read(1)
            objects += 1
            if any(needle in raw for needle in needles):
                violations.append({'object': identity.decode(), 'kind': 'blocked_history'})
    return {'files': len(files), 'history_objects': objects, 'violations': violations}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', type=Path)
    parser.add_argument('--history', action='store_true')
    args = parser.parse_args()
    result = scan(args.policy, args.history)
    print(json.dumps(result, ensure_ascii=False))
    return bool(result['violations'])


if __name__ == '__main__':
    raise SystemExit(main())
