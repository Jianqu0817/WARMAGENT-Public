"""Isolated UI/API fixtures: never launch chat, models, installers or devices."""
import json
from pathlib import Path
from types import SimpleNamespace
import tomllib

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
import pytest

from warmagent.console_catalog import ACTIONS
from warmagent.console_settings import read_form, save_form
from warmagent.control_console import Console, create_app


@pytest.fixture
def project(tmp_path):
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/bridge.local.toml').write_bytes(b'[bridge]\r\nowner_name = "fixture" # keep me\r\nport=8766\r\nunknown=42\r\n[bridge.memory]\r\ncompact_index_enabled=true\r\n')
    (tmp_path / 'config/engine.toml').write_text('[chat]\nmodel="fixture"\n[budget]\ndaily_tokens=100\n', encoding='utf-8')
    (tmp_path / 'README.md').write_text('# Fixture\nRead only.', encoding='utf-8')
    (tmp_path / 'config/onebot.local.toml').write_text('[onebot]\naccess_token="SYNTHETIC_SECRET"\n', encoding='utf-8')
    (tmp_path / '.venv/Scripts').mkdir(parents=True)
    (tmp_path / '.venv/Scripts/python.exe').touch()
    return tmp_path


def test_form_preserves_other_keys_comments_and_secrets_and_backs_up(project):
    before = (project / 'config/bridge.local.toml').read_bytes()
    snapshot = read_form(project)
    assert 'SYNTHETIC_SECRET' not in json.dumps(snapshot)
    save_form(project, {'file': 'bridge', 'revision': snapshot['revisions']['bridge'],
        'changes': {'bridge:bridge:owner_name': '新的称呼', 'bridge:bridge:allowed_groups': ['fixture|room']}})
    text = (project / 'config/bridge.local.toml').read_text(encoding='utf-8')
    table = tomllib.loads(text)['bridge']
    assert table['owner_name'] == '新的称呼' and table['unknown'] == 42
    assert table['allowed_groups'] == ['fixture|room'] and '# keep me' in text
    assert next((project / 'var/backups/control-config').glob('*')).read_bytes() == before
    with pytest.raises(FileExistsError):
        save_form(project, {'file': 'bridge', 'revision': snapshot['revisions']['bridge'],
            'changes': {'bridge:bridge:owner_name': 'old'}})


def test_invalid_and_unsafe_downgrade_never_change_config(project):
    snapshot = read_form(project)
    path = project / 'config/bridge.local.toml'
    before = path.read_bytes()
    for changes in ({'bridge:bridge.memory:compact_index_enabled': False},
            {'bridge:bridge.autonomy:cost_quiet_hours': ['17:00-09:00']},
            {'bridge:bridge:token_file': 'any'}, {'bridge:bridge.autonomy:scan_interval_seconds': -1}):
        with pytest.raises(ValueError):
            save_form(project, {'file': 'bridge', 'revision': snapshot['revisions']['bridge'], 'changes': changes})
        assert path.read_bytes() == before


def test_complex_multiline_value_is_not_silently_damaged(project):
    path = project / 'config/bridge.local.toml'
    path.write_text('[bridge]\nallowed_groups = [\n "fixture|room"\n]\n', encoding='utf-8')
    before = path.read_bytes()
    snapshot = read_form(project)
    with pytest.raises(ValueError):
        save_form(project, {'file': 'bridge', 'revision': snapshot['revisions']['bridge'],
            'changes': {'bridge:bridge:allowed_groups': ['another|room']}})
    assert path.read_bytes() == before


def test_launch_uses_only_fixed_targets_and_prevents_duplicate_runtime(project, monkeypatch):
    calls = []
    def launch(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(pid=12345, poll=lambda: None)
    console = Console(project, popen=launch)
    monkeypatch.setattr(console, 'listening', lambda port: False)
    with pytest.raises(ValueError):
        console.start('web_live & echo injected')
    row = console.start('web_offline')
    assert calls[0][0][1:] == ['-u', '-m', 'warmagent', 'web', '--offline', '--ui', '--no-browser']
    assert calls[0][1]['shell'] is False and calls[0][1]['cwd'] == str(project)
    assert console.runs[row['id']]['action'] == 'web_offline'
    with pytest.raises(FileExistsError):
        console.start('wechat')
    with pytest.raises(FileExistsError):
        console.start('setup')
    assert len(calls) == 1
    with pytest.raises(ValueError):
        console.read_log('../../config/onebot.local.toml')


def test_all_existing_cmd_entries_have_a_visible_mapping():
    root = Path(__file__).resolve().parents[1]
    mapped = {row['cmd'].lower() for row in ACTIONS if row['cmd']} | {'start.cmd'}
    assert {path.name.lower() for path in root.glob('*.cmd')} <= mapped


@pytest.mark.asyncio
async def test_loopback_session_csrf_and_read_only_documents(project, monkeypatch):
    calls = []
    console = Console(project, popen=lambda *a, **kw: calls.append(a))
    monkeypatch.setattr(console, 'listening', lambda port: False)
    app = await create_app(project, controller=console)
    async with TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True)) as client:
        assert (await client.get('/api/settings')).status == 403
        page = await client.get('/')
        html = await page.text()
        csrf = html.split('name="csrf-token" content="')[1].split('"')[0]
        response = await client.get('/api/state')
        state = await response.json()
        assert response.status == 200 and not state['runs']
        assert (await client.get('/api/state', headers={'Host': 'outside.invalid'})).status == 403
        origin = str(client.make_url('/')).rstrip('/')
        assert (await client.post('/api/actions', json={'id': 'prepare'}, headers={'Origin': origin})).status == 403
        assert (await client.post('/api/actions', json={'id': 'prepare'}, headers={
            'Origin': 'https://outside.invalid', 'X-Console-CSRF': csrf})).status == 403
        assert (await client.post('/api/actions', json={'id': 'injected'}, headers={
            'Origin': origin, 'X-Console-CSRF': csrf})).status == 400
        assert (await client.get('/api/document', params={'id': '../secret.md'})).status == 400
        doc = await (await client.get('/api/document', params={'id': 'README.md'})).json()
        assert doc['text'] == '# Fixture\nRead only.'
        assert (await client.get('/api/logs/not-a-log')).status == 400
        log = await (await client.get('/api/logs/chat')).json()
        assert '尚未生成' in log['text']
        assert not calls
