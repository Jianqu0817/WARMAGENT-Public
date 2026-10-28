from argparse import Namespace
import json
from pathlib import Path
import shutil
import sqlite3
import tomllib
from types import SimpleNamespace
from contextlib import nullcontext

import pytest

from warmagent.__main__ import parser
from warmagent import launch


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('qq_enabled,qq_valid', [(False, True), (True, True), (True, False)])
async def test_dual_assembly_uses_one_bridge_and_disabled_qq_opens_nothing(tmp_path, monkeypatch, qq_enabled, qq_valid):
    from aiohttp import web
    from warmagent import bridge as bridge_module, channel_runtime, onebot_client, server, wechat, wechat_client
    from warmagent.onebot_client import OneBotSettings
    made, ran = [], []
    config = {'allowed_groups': ['xiaowennuan-wechat|room', 'xiaowennuan-wechat|owner',
        'xiaowennuan-qq|group:123', 'xiaowennuan-qq|private:456'], 'owner_id': 'owner',
        'shared_memory': True, 'persona_file': 'config/persona/persona.md', 'token_file': 'var/token'}
    (tmp_path / 'var').mkdir()
    (tmp_path / 'var/token').write_text('fixture-token')
    native = SimpleNamespace(group_enabled=True, private_enabled=True, owner_id='owner')
    class Client:
        group_id, private_id, bot_id = 'room', 'owner', 'bot'
        async def bind(self):
            pass
        async def close(self):
            pass
    class Runner:
        def __init__(self, bridge, client, **kwargs):
            self.bridge, self.client, self.observe_only = bridge, client, kwargs['observe_only']
        async def run(self):
            ran.append('wechat')
        async def close(self):
            pass
    class API:
        def __init__(self, *a, **kw):
            pass
        async def setup(self):
            pass
        async def start(self):
            pass
        async def cleanup(self):
            pass
    def bridge_factory(state, settings, engine, persona):
        made.append(dict(settings))
        return SimpleNamespace(config=settings)
    async def dual_run(bridge, runners):
        assert runners['wechat'].bridge is runners['qq'].bridge is bridge
        assert runners['wechat'].observe_only and runners['qq'].observe_only
        ran.append('dual')
        await runners['qq'].close()
    monkeypatch.setattr(launch, 'read_settings', lambda _: (tmp_path, config, tmp_path / 'var/groups'))
    monkeypatch.setattr(launch, 'locked', lambda _: nullcontext())
    monkeypatch.setattr(launch, 'engine_for', lambda *a: {})
    monkeypatch.setattr(wechat_client.Settings, 'load', lambda *a, **kw: native)
    monkeypatch.setattr(wechat_client, 'NativeClient', lambda _: Client())
    monkeypatch.setattr(wechat, 'WeChatRunner', Runner)
    monkeypatch.setattr(bridge_module, 'Bridge', bridge_factory)
    monkeypatch.setattr(server, 'create_app', lambda *a: object())
    monkeypatch.setattr(web, 'AppRunner', API)
    monkeypatch.setattr(web, 'TCPSite', API)
    monkeypatch.setattr(channel_runtime, 'run_channels', dual_run)
    def qq_settings(_):
        if not qq_valid:
            raise ValueError('synthetic invalid QQ config')
        return OneBotSettings(enabled=qq_enabled, bot_id='999', owner_id='456',
            group_ids=['123'], private_ids=['456'])
    monkeypatch.setattr(onebot_client.OneBotSettings, 'load', qq_settings)
    original = onebot_client.OneBotClient
    def qq_client(settings):
        assert qq_enabled, 'disabled QQ constructed a client'
        return original(settings)
    monkeypatch.setattr(onebot_client, 'OneBotClient', qq_client)
    args = Namespace(command='dual', config=tmp_path / 'config/bridge.local.toml', ui=False,
        wechat_config=None, onebot_config=None, mode='offline', observe_only=True)
    await launch.dual(args)
    assert len(made) == 1 and ran == ['dual' if qq_enabled and qq_valid else 'wechat']
    if qq_enabled and qq_valid:
        assert made[0]['owner_report_route'] == 'xiaowennuan-wechat|owner'
        assert made[0]['owner_bindings'][0]['sender_id'] == '456'


def test_wechat_exit_error_is_persisted_without_live_start(tmp_path, monkeypatch):
    from warmagent.__main__ import main
    from warmagent.wechat_client import NotReady
    async def fail(args):
        raise NotReady('NATIVE_HTTP_UNAVAILABLE')
    monkeypatch.setattr(launch, 'wechat', fail)
    with pytest.raises(SystemExit, match='NATIVE_HTTP_UNAVAILABLE'):
        main(['wechat', '--config', str(tmp_path / 'config/bridge.local.toml'), '--live'])
    text = (tmp_path / 'var/logs/warmagent.log').read_text(encoding='utf-8')
    assert 'NATIVE_HTTP_UNAVAILABLE' in text
    assert 'WeChat service stopped' in text


def prepared(tmp_path):
    shutil.copytree(ROOT / "config", tmp_path / "config",
                    ignore=shutil.ignore_patterns("*.local.toml", "*.local.json"))
    args = Namespace(config=tmp_path / "config/bridge.local.toml", wechat_config=None)
    launch.prepare(args)
    return args


def test_modes_are_explicit():
    with pytest.raises(SystemExit):
        parser().parse_args(["wechat"])
    assert parser().parse_args(["chat", "--offline"]).mode == "offline"
    assert parser().parse_args(["wechat", "--live"]).mode == "live"
    assert parser().parse_args(['chat', '--live', '--ui']).ui
    assert parser().parse_args(['dual', '--offline', '--ui']).mode == 'offline'


def test_prepare_preserves_existing_files_and_check_is_offline(tmp_path, monkeypatch):
    args = prepared(tmp_path)
    files = [args.config, launch.wechat_file(args), launch.onebot_file(args), tmp_path / "var/bridge-token.txt"]
    original = [file.read_bytes() for file in files]
    launch.prepare(args)
    assert [file.read_bytes() for file in files] == original
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *a: pytest.fail("check attempted network"))
    launch.check(args)
    assert not (tmp_path / "var/groups").exists()


@pytest.mark.asyncio
async def test_first_offline_chat_creates_local_private_route_and_sends_one_reply(tmp_path, monkeypatch, capsys):
    args = prepared(tmp_path)
    args.scope, args.mode = None, "offline"
    before = args.config.read_bytes()
    lines = iter(["第一次本机私聊", "/exit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(lines))
    await launch.chat(args)
    output = capsys.readouterr().out
    assert "离线演示" in output
    assert args.config.read_bytes() == before
    database = tmp_path / "var/groups/deliveries.sqlite3"
    with sqlite3.connect(database) as db:
        routes = [json.loads(payload) for (payload,) in db.execute("SELECT payload FROM group_routes")]
        assert len(routes) == 1
        route = routes[0]
        assert (route["platform_id"], route["bot_id"], route["group_id"], route["sender_id"]) == (
            "xiaowennuan-local", "xiaowennuan-local-bot", "offline-owner", "offline-owner")
        assert route["conversation_kind"] == "private"
    wechat = tomllib.loads(launch.wechat_file(args).read_text(encoding="utf-8"))["wechatagent"]
    assert wechat["project_dir"] == "var/wechatagent-runtime/workspace"


@pytest.mark.asyncio
async def test_live_local_chat_requires_real_owner_before_engine(tmp_path):
    args = prepared(tmp_path)
    args.scope, args.mode = None, "live"
    with pytest.raises(ValueError, match="live.*owner_id"):
        await launch.chat(args)


@pytest.mark.asyncio
async def test_bind_saves_authenticated_ids_only(tmp_path, monkeypatch):
    args = prepared(tmp_path)
    from warmagent import wechat_client
    class Client:
        group_id, bot_id = "real@chatroom", "real_bot"
        private_id = "real_owner"
        settings = SimpleNamespace(group_enabled=True, private_enabled=True)
        def __init__(self, settings):
            pass
        async def bind(self):
            pass
        async def close(self):
            pass
    monkeypatch.setattr(wechat_client.Settings, "load", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(wechat_client, "NativeClient", Client)
    await launch.bind(args)
    bridge = tomllib.loads(args.config.read_text(encoding="utf-8"))["bridge"]
    wechat = tomllib.loads(launch.wechat_file(args).read_text(encoding="utf-8"))["wechatagent"]
    assert bridge["allowed_groups"] == ["xiaowennuan-wechat|real@chatroom", "xiaowennuan-wechat|real_owner"]
    assert (wechat["group_id"], wechat["bot_id"], wechat["private_id"]) == ("real@chatroom", "real_bot", "real_owner")
    assert not (tmp_path / "upstream").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("authentication_fails", [True, False])
async def test_wechat_authenticates_and_checks_allowlist_before_bridge(tmp_path, monkeypatch, authentication_fails):
    args = Namespace(config=tmp_path / "config/bridge.local.toml", wechat_config=None,
                     ui=False, mode="live", observe_only=False)
    monkeypatch.setattr(launch, "read_settings", lambda _: (tmp_path, {"allowed_groups": []}, tmp_path / "var/groups"))
    from warmagent import bridge, server, wechat_client
    events = []
    class Client:
        group_id = "real@chatroom"
        private_id = ""
        def __init__(self, settings):
            pass
        async def bind(self):
            events.append("authenticate")
            if authentication_fails:
                raise RuntimeError("authentication failed")
        async def close(self):
            events.append("close")
    from types import SimpleNamespace
    monkeypatch.setattr(wechat_client.Settings, "load", lambda *args, **kwargs: SimpleNamespace(
        group_enabled=True, private_enabled=False))
    monkeypatch.setattr(wechat_client, "NativeClient", Client)
    monkeypatch.setattr(bridge, "Bridge", lambda *a, **kw: pytest.fail("Bridge constructed before authentication"))
    monkeypatch.setattr(server, "create_app", lambda *a: pytest.fail("Bridge background tasks started"))
    with pytest.raises((RuntimeError, ValueError)):
        await launch.wechat(args)
    assert events == ["authenticate", "close"]
