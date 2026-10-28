import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import threading
import time
import tomllib
from types import SimpleNamespace

import pytest

from myatri.providers.gateway import FixtureProvider
from warmagent import local_chat
from warmagent.bridge import Bridge
from warmagent.local_chat import display_background_reports
from test_warm_runtime import incoming, make_bridge


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['private', 'group'])
async def test_local_autonomy_uses_selected_context_and_displays_reports_once(tmp_path, kind):
    bridge, provider = make_bridge(tmp_path, mode='live', autonomous=True, direct=True)
    route = incoming(kind, sender='owner')
    bridge.register_route(route)
    bridge.active_local_scopes.add(route.scope)
    bridge.direct.settings.update(enabled=True, owner_id='owner',
        permission_profile='full', autonomy_granted=True)
    try:
        local = bridge._owner_route()
        assert local.scope == route.scope and local.transport == 'local_cli'
        assert local.sender_id == 'owner'
        with sqlite3.connect(bridge.path) as db:
            db.execute("INSERT INTO deliveries(id,scope,fingerprint,status,updated,envelope) "
                "VALUES('wechat-pending',?,'fixture','prepared',0,?)",
                (route.scope, json.dumps({'transport': 'astrbot'})))
            db.executemany("INSERT INTO deliveries(id,scope,fingerprint,status,updated,envelope,trigger_kind) "
                "VALUES(?,?,'fixture','prepared',0,?,'autonomy')",
                [(f'wechat-old-{i}', route.scope, json.dumps({'transport': 'astrbot'})) for i in range(4)])
        assert not bridge._activity_available()['busy']
        calls = []
        async def step(activity, **kwargs):
            calls.append(activity)
            return {'action': 'create', 'goal': '本地自主活动', 'summary': '已推进'}
        bridge.activities.run_step = step
        assert (await bridge.activities.tick(time.time() + 601))['ran']
        assert len(calls) == 1 and not provider.requests
        await bridge._activity_notify('本地活动有新进展。')
        assert bridge._activity_available()['busy']
        lines = []
        await display_background_reports(bridge, route.scope, lines.append)
        await display_background_reports(bridge, route.scope, lines.append)
        assert lines == ['小温暖 > 本地活动有新进展。']
        assert not bridge._activity_available()['busy']
        with closing(sqlite3.connect(bridge.path)) as db:
            assert db.execute("SELECT status FROM deliveries WHERE id='wechat-pending'").fetchone()[0] == 'prepared'
        bridge.active_local_scopes.clear()
        assert bridge._activity_available()['busy']
    finally:
        await bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('ui', [False, True])
async def test_terminal_waiting_for_input_keeps_real_heartbeat_alive_without_wechat(tmp_path, monkeypatch, ui):
    # The terminal Bridge below uses fixtures, not the active production service.
    observed = threading.Event()
    instances = []
    project = Path(__file__).resolve().parents[1]
    root = tmp_path / 'project'
    (root / 'config/persona').mkdir(parents=True)
    (root / 'config/persona/persona.md').write_text(
        (project / 'config/persona/persona.md').read_text(encoding='utf-8'), encoding='utf-8')
    (root / 'var').mkdir()
    (root / 'var/token.txt').write_text('t' * 40)
    engine = tomllib.loads((project / 'config/engine.toml').read_text(encoding='utf-8'))
    settings = {'project_root': str(root), 'mode': 'live', 'owner_id': 'owner', 'owner_name': '用户',
        'persona_file': 'config/persona/persona.md', 'allowed_groups': [], 'shared_memory': True,
        'token_file': 'var/token.txt', 'port': 0,
        'autonomy': {'enabled': True}, 'proactive': {'enabled': True},
        'computer': {'enabled': True, 'direct_enabled': True, 'permission_profile': 'full', 'owner_id': 'owner',
            'autonomy_granted': True}}
    route = local_chat.new_local_private_route('owner')
    local_chat.allow_local_route(settings, route)
    def create(state_root, config, model_config, persona_file):
        bridge = Bridge(state_root, config, model_config, persona_file,
            provider_factory=lambda _: FixtureProvider([]))
        bridge.activities.interval = 0
        async def step(activity, **kwargs):
            assert bridge._owner_route().transport == 'local_cli'
            observed.set()
            return {'action': 'create', 'goal': '空闲心跳', 'summary': '已推进'}
        bridge.activities.run_step = step
        instances.append(bridge)
        return bridge
    def read(*_):
        assert observed.wait(3), 'CLI input blocked the autonomous heartbeat'
        return '/exit'
    monkeypatch.setattr(local_chat, 'Bridge', create)
    monkeypatch.setattr('builtins.input', read)
    class ExistingPage:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def settimeout(self, value): pass
        def connect_ex(self, address): return 0
    monkeypatch.setattr(local_chat, 'socket', SimpleNamespace(socket=ExistingPage))
    await asyncio.wait_for(local_chat.chat(root, tmp_path / 'groups', settings, engine, route, ui=ui), 5)
    assert instances[0].activity_observation['last_error'] is None
    assert settings['proactive']['enabled'] and settings['autonomy']['enabled']
