"""Offline synthetic QQ/dual tests. Only ephemeral local WS; no NapCat/login/model/send."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from aiohttp import ClientSession, WSServerHandshakeError, web
from aiohttp.test_utils import TestServer
import pytest

from myatri.app import load_config
from myatri.contracts import digest
from myatri.providers import FixtureProvider
from warmagent.bridge import Bridge, Incoming
from warmagent.channel_runtime import run_channels
from warmagent.onebot import OneBotRunner
from warmagent.onebot_client import OneBotClient, OneBotSettings, parse_event
from warmagent.wechat import WeChatRunner
from warmagent.wechat_client import NotReady
from test_wechat import FakeClient
from test_direct_harness import FakeExecutor
from test_group_restraint import silent

ROOT = Path(__file__).resolve().parents[1]


def settings(**kw):
    return OneBotSettings(**({'enabled': True, 'bot_id': '999', 'owner_id': '456',
        'group_ids': ['123'], 'private_ids': ['456'], 'access_token': 'synthetic-test-token'} | kw))


def event(**kw):
    return {'post_type': 'message', 'message_type': 'group', 'self_id': 999,
        'user_id': 456, 'group_id': 123, 'message_id': 1,
        'time': int(datetime.now(timezone.utc).timestamp()), 'sender': {'nickname': 'fixture'},
        'message': [{'type': 'at', 'data': {'qq': '999'}}, {'type': 'text', 'data': {'text': '你好'}}]} | kw


def make_bridge(tmp_path):
    config = {'project_root': str(tmp_path), 'mode': 'offline', 'owner_id': 'wx-owner',
        'owner_name': 'fixture-owner', 'shared_memory': True, 'library_scope': 'a' * 32,
        'owner_bindings': [{'platform_id': 'xiaowennuan-qq', 'bot_id': '999', 'sender_id': '456'}],
        'owner_report_route': 'xiaowennuan-wechat|wx-owner',
        'allowed_groups': ['xiaowennuan-wechat|room@chatroom', 'xiaowennuan-wechat|wx-owner',
            'xiaowennuan-qq|group:123', 'xiaowennuan-qq|group:456', 'xiaowennuan-qq|private:456'],
        'random_reply_probability': 0, 'quality_guard_enabled': False, 'proactive': {'enabled': False}}
    provider = FixtureProvider([{'content': '第一段回复。\n第二段回复。'}] * 16)
    return Bridge(tmp_path / 'groups', config, load_config(ROOT / 'config/engine.toml'),
        ROOT / 'config/persona/persona.md', provider_factory=lambda _: provider)


class FakeQQ:
    group_id = ''

    def __init__(self, fail_part=None):
        self.settings = settings()
        self.bot_id = '999'
        self.sent, self.sent_targets = [], []
        self.fail_part = fail_part
        self.closed = False
        self.connected = True
        self.started = asyncio.Event()

    async def start(self):
        self.started.set()

    async def events(self):
        await asyncio.Event().wait()
        yield None

    async def send(self, text, target_id):
        self.sent.append(text)
        self.sent_targets.append(target_id)
        if len(self.sent) == self.fail_part:
            raise NotReady('SEND_RECEIPT_UNKNOWN')
        return str(900 + len(self.sent))

    async def close(self):
        self.closed = True


@pytest.mark.parametrize('change', [
    {'post_type': 'message_sent'}, {'post_type': 'meta_event'}, {'post_type': 'notice'},
    {'user_id': 999}, {'self_id': 888}, {'message': [{'type': 'image', 'data': {'file': 'x'}}]}])
def test_ignore_nonhuman_or_nontext_events(change):
    assert parse_event(event(**change), '999') is None


def test_cq_escape_mentions_reply_and_array_equivalence():
    parsed = parse_event(event(message='[CQ:reply,id=12][CQ:at,qq=111][CQ:at,qq=all]a&#91;b&#93;&amp;'), '999')
    assert parsed['text'] == 'a[b]&' and parsed['reply_to_id'] == '12'
    assert not parsed['mentioned'] and parsed['mentioned_ids'] == ['111', 'all']
    assert parse_event(event(message='&#91;CQ:at,qq=999&#93;你好'), '999')['mentioned'] is False
    assert parse_event(event(message='[CQ:at,qq=999]你好'), '999')['mentioned'] is True
    array = parse_event(event(), '999')
    string = parse_event(event(message='[CQ:at,qq=999]你好'), '999')
    assert array == string


def test_settings_default_off_and_private_owner_only(tmp_path):
    path = tmp_path / 'onebot.local.toml'
    assert not OneBotSettings.load(path).enabled
    path.write_text('[onebot]\nenabled=true\nbot_id="999"\nowner_id="456"\n'
        'private_ids=["789"]\naccess_token="synthetic-test-token"\n', encoding='utf-8')
    with pytest.raises(ValueError, match='owner-only'):
        OneBotSettings.load(path)


@pytest.mark.asyncio
async def test_owner_binding_keeps_raw_id_and_denies_unbound_private(tmp_path):
    bridge = make_bridge(tmp_path)
    try:
        runner = OneBotRunner(bridge, FakeQQ())
        message = runner.message(event(message_type='private'))
        assert message.sender_id == '456' and bridge._is_owner(message)
        assert bridge._urgent_input(message.model_copy(update={'text': '立即暂停任务'}))
        raw = SimpleNamespace(author_kind='human', payload={'channel_source': message.model_dump()})
        bridge.direct.settings['owner_id'] = 'wx-owner'
        assert bridge.direct._owner_authorized(raw)
        spoof = message.model_copy(update={'sender_id': '789', 'sender_name': 'fixture-owner'})
        assert not bridge._is_owner(spoof)
        raw.payload['channel_source'] = spoof.model_dump()
        assert not bridge.direct._owner_authorized(raw)
        assert (await bridge.receive(spoof))['reason'] == 'private_owner_required'
        assert not bridge._is_owner(message.model_copy(update={'bot_id': '998'}))
        await runner.handle(event(message_type='private'))
        app = bridge._application(message, start_workers=False)
        originals = [r for r in app.store.list_records('raw_message') if r.payload.get('role') == 'user']
        assert json.loads(originals[0].body_markdown)['sender_id'] == '456'
        assert bridge.identities.context(message.scope, '456', source=message.model_dump())['current_member']['is_owner']
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_same_number_group_and_private_do_not_share_scope_or_key(tmp_path):
    bridge = make_bridge(tmp_path)
    client = FakeQQ()
    client.settings.group_ids.append('456')
    try:
        runner = OneBotRunner(bridge, client)
        group = runner.message(event(group_id=456))
        private = runner.message(event(message_type='private'))
        assert group.scope != private.scope and group.key != private.key and group.route != private.route
        assert group.group_id == 'group:456' and private.group_id == 'private:456'
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_bound_qq_owner_can_use_fake_harness_and_stranger_cannot(tmp_path):
    bridge = make_bridge(tmp_path)
    harness = bridge.direct
    executor = FakeExecutor()
    harness.executor = executor
    harness.mode = 'live'
    harness.settings.update(enabled=True, owner_id='wx-owner', permission_profile='full')
    message = OneBotRunner(bridge, FakeQQ()).message(event())
    raw = {'author_kind': 'human', 'channel_source': message.model_dump()}
    try:
        tools = await harness.handlers(raw, context={'turn_id': 'synthetic-qq-turn', 'scope': message.scope})
        invoke = tools['harness__dsh-tool-search'][1].execute_call
        await invoke('synthetic-call', {'query': 'fixture'})
        assert len(executor.calls) == 1
        denied = {**raw, 'channel_source': {**raw['channel_source'], 'sender_id': '789'}}
        assert not await harness.handlers(denied, context={'turn_id': 'other', 'scope': message.scope})
    finally:
        await bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('qq_first', [True, False])
async def test_owner_report_route_is_explicit_and_survives_restart(tmp_path, qq_first):
    bridge = make_bridge(tmp_path)
    qq = OneBotRunner(bridge, FakeQQ()).message(event(message_type='private'))
    wx = Incoming(platform_id='xiaowennuan-wechat', bot_id='wx-bot', group_id='wx-owner',
        sender_id='wx-owner', message_id='bind', sent_at=qq.sent_at, text='绑定',
        mentioned=False, conversation_kind='private')
    for route in ([qq, wx] if qq_first else [wx, qq]):
        bridge.register_route(route)
    try:
        assert bridge._owner_route().platform_id == 'xiaowennuan-wechat'
    finally:
        await bridge.close()
    reloaded = make_bridge(tmp_path)
    try:
        assert reloaded._owner_route().platform_id == 'xiaowennuan-wechat'
    finally:
        await reloaded.close()


@pytest.mark.asyncio
async def test_parts_unknown_are_persistent_and_never_resent(tmp_path):
    bridge = make_bridge(tmp_path)
    client = FakeQQ(fail_part=2)
    runner = OneBotRunner(bridge, client)
    payload = event()
    message = runner.message(payload)
    runner.scope_targets[message.scope] = message.group_id
    try:
        await runner.handle(payload)
        with sqlite3.connect(bridge.path) as db:
            assert db.execute('SELECT status,sent_parts FROM deliveries WHERE id=?', (message.key,)).fetchone() == ('unknown', 1)
            assert db.execute('SELECT message_id FROM delivery_receipts').fetchall() == [('901',)]
        await runner.handle(payload)
        await runner.poll()
        assert len(client.sent) == 2
    finally:
        await runner.close()
        await bridge.close()


@pytest.mark.asyncio
async def test_disconnected_qq_does_not_claim_definitely_unsent_reply(tmp_path):
    bridge = make_bridge(tmp_path)
    client = FakeQQ()
    runner = OneBotRunner(bridge, client)
    message = runner.message(event())
    runner.scope_targets[message.scope] = message.group_id
    try:
        delivery = await bridge.receive(message)
        client.connected = False
        await runner.deliver(delivery, target_id=message.group_id)
        with sqlite3.connect(bridge.path) as db:
            assert db.execute('SELECT status FROM deliveries WHERE id=?', (message.key,)).fetchone()[0] == 'prepared'
        client.connected = True
        await runner.poll()
        assert len(client.sent) == 2
    finally:
        await runner.close()
        await bridge.close()


@pytest.mark.asyncio
async def test_disabled_channel_keeps_backlog_without_claiming_or_blocking_activity(tmp_path):
    bridge = make_bridge(tmp_path)
    qq = OneBotRunner(bridge, FakeQQ()).message(event())
    try:
        bridge.input_inbox.enqueue(qq.key, qq.scope, digest(qq.model_dump()), qq.model_dump())
        assert bridge.input_inbox.claim(platforms=['xiaowennuan-wechat']) is None
        assert bridge.input_inbox.claim(platforms=['xiaowennuan-qq'])['key'] == qq.key
        # A separately received turn creates a real pending outbox entry.
        other = qq.model_copy(update={'message_id': 'other'})
        await bridge.receive(other)
        bridge.chat_batches.queue(other.key, other.scope, 0)
        assert bridge.chat_batches.claim(local=False, scopes=set(), platforms=['xiaowennuan-wechat']) is None
        bridge.config.update(mode='live', active_platforms=['xiaowennuan-wechat'])
        bridge.autonomy_config['enabled'] = True
        assert not bridge._activity_available()['busy']
        bridge.config['active_platforms'].append('xiaowennuan-qq')
        assert bridge._activity_available()['busy']
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_known_qq_reply_wakes_bot_without_at(tmp_path):
    bridge = make_bridge(tmp_path)
    runner = OneBotRunner(bridge, FakeQQ())
    try:
        await runner.handle(event())
        await runner.handle(event(message_id=2, message=[{'type': 'reply', 'data': {'id': 901}},
            {'type': 'text', 'data': {'text': '接着说'}}]))
        assert len(runner.client.sent) == 4
    finally:
        await runner.close()
        await bridge.close()


@pytest.mark.asyncio
async def test_optional_qq_group_can_finish_without_reply(tmp_path):
    bridge = make_bridge(tmp_path)
    bridge.random_probability = 1
    runner = OneBotRunner(bridge, FakeQQ())
    payload = event(user_id=789, message=[{'type': 'text', 'data': {'text': '他们在商量周末碰面的地方'}}])
    message = runner.message(payload)
    app = bridge._application(message, start_workers=False)
    async def generate(_):
        return silent()
    app.gateway.provider.generate = generate
    try:
        await runner.handle(payload)
        assert not runner.client.sent
        with sqlite3.connect(bridge.path) as db:
            assert db.execute('SELECT status FROM deliveries WHERE id=?', (message.key,)).fetchone()[0] == 'skipped'
        assert not bridge.short_memory.recent_assistant(message.scope)
    finally:
        await runner.close()
        await bridge.close()


@pytest.mark.asyncio
async def test_parallel_channels_share_library_but_poll_own_scope(tmp_path):
    bridge = make_bridge(tmp_path)
    qq_client, wx_client = FakeQQ(), FakeClient()
    qq_runner, wx_runner = OneBotRunner(bridge, qq_client), WeChatRunner(bridge, wx_client)
    qq = qq_runner.message(event())
    wx = Incoming(platform_id='xiaowennuan-wechat', bot_id='bot', group_id='room@chatroom',
        sender_id='wx-owner', message_id='m1', sent_at=qq.sent_at, text='你好', mentioned=True)
    qq_runner.scope_targets[qq.scope] = qq.group_id
    wx_runner.scope_targets[wx.scope] = wx.group_id
    try:
        results = await asyncio.gather(bridge.receive(qq), bridge.receive(wx))
        assert all(result['status'] == 'prepared' for result in results)
        assert bridge._application(qq).store is bridge._application(wx).store
        await asyncio.gather(qq_runner.poll(), wx_runner.poll())
        assert qq_client.sent_targets == ['group:123'] * 2
        assert wx_client.sent_targets == ['room@chatroom'] * 2
    finally:
        await asyncio.gather(qq_runner.close(), wx_runner.close())
        await bridge.close()


@pytest.mark.asyncio
async def test_bootstrap_and_disabled_runner(tmp_path):
    bridge = make_bridge(tmp_path)
    client = FakeQQ()
    runner = OneBotRunner(bridge, client)
    client.settings.enabled = False
    await runner.run()
    assert not client.started.is_set() and not runner.scope_targets
    client.settings.enabled = True
    task = asyncio.create_task(runner.run())
    try:
        await asyncio.wait_for(client.started.wait(), 2)
        assert set(runner.scope_targets.values()) == {'group:123', 'private:456'}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runner.close()
        await bridge.close()


def ws_headers(**updates):
    return {'Authorization': 'Bearer synthetic-test-token', 'X-Self-ID': '999',
        'X-Client-Role': 'Universal'} | updates


@pytest.mark.asyncio
async def test_reverse_ws_auth_and_out_of_order_receipts():
    client = OneBotClient(settings())
    app = web.Application()
    app.router.add_get('/ws', client._connect)
    async with TestServer(app) as server, ClientSession() as session:
        for headers, code in [(ws_headers(Authorization='Bearer wrong'), 401),
                (ws_headers(**{'X-Self-ID': '888'}), 403),
                (ws_headers(**{'X-Client-Role': 'Event'}), 403)]:
            with pytest.raises(WSServerHandshakeError) as error:
                await session.ws_connect(server.make_url('/ws'), headers=headers)
            assert error.value.status == code
        async with session.ws_connect(server.make_url('/ws'), headers=ws_headers()) as ws:
            with pytest.raises(WSServerHandshakeError) as error:
                await session.ws_connect(server.make_url('/ws'), headers=ws_headers())
            assert error.value.status == 409
            first = asyncio.create_task(client.send('[CQ:at,qq=all]', 'group:123'))
            r1 = await ws.receive_json(timeout=2)
            second = asyncio.create_task(client.send('hi', 'private:456'))
            r2 = await ws.receive_json(timeout=2)
            assert r1['params']['message'] == [{'type': 'text', 'data': {'text': '[CQ:at,qq=all]'}}]
            await ws.send_json(event())
            await ws.send_json({'echo': r2['echo'], 'status': 'ok', 'retcode': 0, 'data': {'message_id': 22}})
            await ws.send_json({'echo': r1['echo'], 'status': 'ok', 'retcode': 0, 'data': {'message_id': 11}})
            assert await asyncio.wait_for(second, 2) == '22'
            assert await asyncio.wait_for(first, 2) == '11'
            assert (await asyncio.wait_for(client.queue.get(), 2))['message_id'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['timeout', 'disconnect', 'rejected', 'missing_id'])
async def test_ws_uncertain_and_rejected_send_never_replays(outcome):
    client = OneBotClient(settings(receipt_timeout=0.05 if outcome == 'timeout' else 1))
    app = web.Application()
    app.router.add_get('/ws', client._connect)
    async with TestServer(app) as server, ClientSession() as session:
        with pytest.raises(NotReady, match='NOT_CONNECTED'):
            await client.send('x', 'group:123')
        async with session.ws_connect(server.make_url('/ws'), headers=ws_headers()) as ws:
            send = asyncio.create_task(client.send('x', 'group:123'))
            request = await ws.receive_json(timeout=2)
            if outcome == 'disconnect':
                await ws.close()
            elif outcome in {'rejected', 'missing_id'}:
                await ws.send_json({'echo': request['echo'], 'status': 'failed' if outcome == 'rejected' else 'ok',
                    'retcode': 100 if outcome == 'rejected' else 0, 'data': {}})
            reason = 'ONEBOT_SEND_REJECTED' if outcome == 'rejected' else 'SEND_RECEIPT_UNKNOWN'
            with pytest.raises(NotReady, match=reason):
                await asyncio.wait_for(send, 2)
            assert not client.pending
            if outcome == 'timeout':
                client.settings.receipt_timeout = 1
                late = {'echo': request['echo'], 'status': 'ok', 'retcode': 0, 'data': {'message_id': 9}}
                await ws.send_json(late)
                followup = asyncio.create_task(client.send('next', 'group:123'))
                fresh = await ws.receive_json(timeout=2)
                assert fresh['echo'] != request['echo']
                await ws.send_json({**late, 'echo': fresh['echo'], 'data': {'message_id': 10}})
                assert await followup == '10'


@pytest.mark.asyncio
async def test_queue_overflow_is_bounded_and_explicit():
    client = OneBotClient(settings(queue_size=1))
    app = web.Application()
    app.router.add_get('/ws', client._connect)
    async with TestServer(app) as server, ClientSession() as session:
        async with session.ws_connect(server.make_url('/ws'), headers=ws_headers()) as ws:
            await ws.send_json(event())
            await ws.send_json(event(message_id=2))
            await ws.receive(timeout=2)
            assert client.queue.qsize() == 1 and client.overflow_count == 1


@pytest.mark.asyncio
async def test_qq_failure_and_cleanup_failure_do_not_stop_wechat():
    ready, failed = asyncio.Event(), asyncio.Event()
    class Runner:
        def __init__(self, broken=False):
            self.broken, self.closed = broken, False
        async def run(self):
            if self.broken:
                failed.set()
                raise OSError('synthetic port unavailable')
            ready.set()
            await asyncio.Event().wait()
        async def reset(self):
            if self.broken:
                raise OSError('synthetic cleanup failure')
        async def close(self):
            self.closed = True
    bridge, wx, qq = SimpleNamespace(), Runner(), Runner(True)
    task = asyncio.create_task(run_channels(bridge, {'wechat': wx, 'qq': qq}, retry_seconds=10))
    try:
        await asyncio.wait_for(asyncio.gather(ready.wait(), failed.wait()), 2)
        assert not task.done() and not wx.closed and bridge.channel_status['qq']['state'] == 'degraded'
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert wx.closed and qq.closed
