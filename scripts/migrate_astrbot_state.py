"""Cold-copy the existing AstrBot memory into this project's shared library.

Keep immutable records and every runtime database. Copy only the current
rebuildable index generation; leave all historical generations in the source.
Never start a model, send WeChat messages, or copy model credentials.
"""
from contextlib import ExitStack, contextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tomllib

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / 'MYATRI-AstrBot'


@contextmanager
def source_stopped():
    import msvcrt
    with (SOURCE / 'var/service.lock').open('r+b') as stream:
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError('Close the old AstrBot group/local-chat service before migration.') from None
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    from warmagent.launch import replace_setting
    from warmagent.server import service_lock
    from setup_computer_control import replace_table
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    original = tomllib.loads((SOURCE / 'config/bridge.local.toml').read_text(encoding='utf-8-sig'))['bridge']
    source = (SOURCE / original['state_root']).resolve()
    target = ROOT / 'var/groups'
    if not source.is_relative_to(SOURCE) or not target.is_relative_to(ROOT):
        raise ValueError('Invalid migration boundary')
    if target.exists():
        raise RuntimeError('Target state already exists; inspect it instead of overwriting.')
    backup = ROOT / 'var/backups' / ('initialization-' + stamp)
    backup.mkdir(parents=True)
    for name in ('bridge.local.toml', 'wechatagent.local.toml', 'engine.toml'):
        shutil.copy2(ROOT / 'config' / name, backup / name)
    with ExitStack() as stack:
        stack.enter_context(service_lock(ROOT / 'var/service.lock'))
        stack.enter_context(source_stopped())
        scopes = [p.name for p in source.iterdir() if p.is_dir() and (p / 'runtime.sqlite3').is_file()]
        if len(scopes) != 1:
            raise RuntimeError('Expected exactly one source memory library; select explicitly before migrating.')
        scope = scopes[0]
        current = {}
        for pointer in source.glob('*/domains/*/indexes/current.json'):
            current[pointer.parent.resolve()] = json.loads(pointer.read_text())['generation'] + '.json'
        files, skipped, copied_bytes = [], 0, 0
        # Create directly under the target while both entry locks are held.
        # A failed run leaves a manifest for inspection and is never overwritten.
        target.mkdir(parents=True)
        for f in source.rglob('*'):
            if not f.is_file() or f.name.endswith(('-wal', '-shm', '.lock')):
                continue
            if f.parent.name == 'generations' and f.parent.parent.resolve() in current:
                if f.name != current[f.parent.parent.resolve()]:
                    skipped += 1
                    continue
            rel = f.relative_to(source)
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if f.suffix in {'.sqlite3', '.db'}:
                with sqlite3.connect(f.as_uri() + '?mode=ro', uri=True) as src, sqlite3.connect(dst) as db:
                    src.backup(db)
                    if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise RuntimeError('Copied SQLite failed quick_check: ' + str(rel))
            else:
                shutil.copy2(f, dst)
                if sha(f) != sha(dst):
                    raise RuntimeError('Copy hash mismatch: ' + str(rel))
            copied_bytes += dst.stat().st_size
            files.append({'path': rel.as_posix(), 'sha256': sha(dst)})
        with sqlite3.connect(target / 'deliveries.sqlite3') as db:
            pending = [row[0] for row in db.execute("SELECT id FROM deliveries WHERE status IN ('prepared','received','waiting_for_computer')")]
            db.execute("UPDATE deliveries SET status='superseded' WHERE status IN ('prepared','received','waiting_for_computer')")
            states = dict(db.execute('SELECT status,COUNT(*) FROM deliveries GROUP BY status'))
        # Retain old global window metadata for the original channel only.
        with sqlite3.connect(target / scope / 'runtime.sqlite3') as db:
            for key in ('context_window_boundary', 'context_token_anchor', 'group_context_boundary'):
                db.execute('INSERT OR IGNORE INTO schedule(agent,user,name,value) '
                    'SELECT agent,user,?,value FROM schedule WHERE name=?', (f'context:{scope}:{key}', key))
        file = ROOT / 'config/bridge.local.toml'
        text = file.read_text(encoding='utf-8-sig')
        keys = ('mode','owner_id','owner_name','allowed_groups','random_reply_probability',
            'attention_followup_enabled','attention_reply_probability','cooldown_seconds',
            'recent_context_messages','dialogue_turns','cold_retention_days','random_reply_ttl_seconds',
            'memory_review_enabled','capture_requests')
        for key in keys:
            if key in original:
                text = replace_setting(text, key, original[key])
        text = replace_setting(text, 'library_scope', scope)
        text = replace_setting(text, 'shared_memory', True)
        for table in ('web', 'proactive', 'recovery'):
            if table in original:
                text = replace_table(text, 'bridge.' + table, original[table])
        settings = tomllib.loads(text)['bridge']['computer']
        settings.update(enabled=True, direct_enabled=True, agent_enabled=False,
            autonomy_granted=True, owner_id=original['owner_id'], permission_profile='full',
            node_path=str(ROOT / 'var/harness/node/node.exe'), runtime_root=str(ROOT / 'var/harness/runtime'))
        text = replace_table(text, 'bridge.computer', settings)
        file.write_text(text, encoding='utf-8')
        wechat = tomllib.loads((SOURCE / 'config/wechatagent.local.toml').read_text(encoding='utf-8-sig'))['wechatagent']
        workspace = Path(wechat['project_dir'])
        if not workspace.is_absolute():
            workspace = SOURCE / workspace
        wechat.update(project_dir=str(workspace.resolve()), owner_id=original['owner_id'],
            group_enabled=True, private_enabled=True, private_id='')
        (ROOT / 'config/wechatagent.local.toml').write_text(replace_table('', 'wechatagent', wechat), encoding='utf-8')
        source_card = SOURCE / original['persona_file']
        target_card = ROOT / 'config/persona/persona.md'
        if sha(source_card) != sha(target_card):
            shutil.copy2(target_card, backup / 'persona.md')
            shutil.copy2(source_card, target_card)
        source_engine = tomllib.loads((SOURCE / original['engine_config']).read_text(encoding='utf-8-sig'))
        model_path = Path(source_engine.get('embedding', {}).get('path', 'var/models/bge-small-zh-v1.5'))
        if not model_path.is_absolute() and (SOURCE / model_path / 'manifest.json').is_file():
            src_model, dst_model = SOURCE / model_path, ROOT / model_path
            if not dst_model.exists():
                dst_model.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src_model, dst_model)
            for model_file in src_model.rglob('*'):
                if model_file.is_file() and sha(model_file) != sha(dst_model / model_file.relative_to(src_model)):
                    raise RuntimeError('Copied embedding model hash mismatch')
        report = {'source': str(source), 'target': str(target), 'library_scope': scope,
            'copied_files': len(files), 'copied_bytes': copied_bytes,
            'historical_index_files_left_in_source': skipped,
            'source_preserved': True, 'stale_outbox_superseded_in_target': pending,
            'delivery_states': states, 'source_file_snapshot': files,
            'configured_identity_from_source': True, 'sent_test_messages': 0}
        (backup / 'migration.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps({k:v for k,v in report.items() if k not in ('source_file_snapshot','stale_outbox_superseded_in_target')}, indent=2))
        print('Report:', backup / 'migration.json')


if __name__ == '__main__':
    main()
