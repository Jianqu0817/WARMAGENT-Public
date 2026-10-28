"""Browser transport contracts on temporary synthetic libraries only."""
import json
import re
import sqlite3
import uuid

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
import pytest

from warmagent.bridge import Incoming
from warmagent.local_chat import local_message
from warmagent.local_web import LOCAL_CHAT, create_app
from test_wechat import make_bridge


async def client_for(bridge):
    client = TestClient(TestServer(create_app(bridge)), cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    response = await client.get('/')
    html = await response.text()
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
    headers = {'Origin': str(client.make_url('/')).rstrip('/'), 'X-Local-CSRF': csrf}
    return client, headers


def fixture_bridge(tmp_path):
    bridge, provider = make_bridge(tmp_path)
    bridge.engine_config['embedding'] = {'backend': 'tfidf'}
    return bridge, provider


@pytest.mark.asyncio
async def test_local_browser_send_refresh_and_explicit_receipt(tmp_path):
    bridge, provider = fixture_bridge(tmp_path)
    client, headers = await client_for(bridge)
    try:
        state = await (await client.get('/api/state')).json()
        scope = state['selected_scope']
        body = {'scope': scope, 'text': '这是一条本机合成消息。', 'client_id': str(uuid.uuid4())}
        response = await client.post('/api/messages', json=body, headers=headers)
        assert response.status == 200
        result = await response.json()
        assert result['status'] == 'prepared'
        count = len(provider.requests)
        # Refreshing and re-submitting the same HTTP id must not call the model twice.
        response = await client.post('/api/messages', json=body, headers=headers)
        assert response.status == 200 and len(provider.requests) == count
        state = await (await client.get('/api/state')).json()
        parts = [row for row in state['messages'] if row.get('delivery_id') == result['id']]
        assert parts and all(row['ack_required'] for row in parts)
        with sqlite3.connect(bridge.path) as db:
            assert db.execute('SELECT status FROM deliveries WHERE id=?', (result['id'],)).fetchone()[0] == 'prepared'
        for row in sorted(parts, key=lambda item: item['part_index']):
            response = await client.post('/api/receipts', json={'scope': scope,
                'delivery_id': row['delivery_id'], 'part_index': row['part_index']}, headers=headers)
            assert response.status == 200
        state = await (await client.get('/api/state')).json()
        assert all(not row['ack_required'] for row in state['messages'] if row.get('delivery_id') == result['id'])
        assert all(row['status'] == 'sent' for row in state['messages'] if row.get('delivery_id') == result['id'])
        assert 'scheduler' in state
        assert not any('token' in name for name in state)
    finally:
        await client.close()
        await bridge.close()


@pytest.mark.asyncio
async def test_authentication_origin_and_sender_are_host_bound(tmp_path):
    bridge, _ = fixture_bridge(tmp_path)
    app = create_app(bridge)
    client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    try:
        assert (await client.get('/api/state')).status == 403
        html = await (await client.get('/')).text()
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
        origin = str(client.make_url('/')).rstrip('/')
        scope = app[LOCAL_CHAT].selected_scope
        body = {'scope': scope, 'text': '合成测试', 'client_id': str(uuid.uuid4())}
        assert (await client.post('/api/messages', json=body, headers={'Origin': origin})).status == 403
        assert (await client.post('/api/messages', json=body,
            headers={'Origin': 'https://example.invalid', 'X-Local-CSRF': csrf})).status == 403
        assert (await client.get('/api/state', headers={'Host': 'example.invalid'})).status == 403
        headers = {'Origin': origin, 'X-Local-CSRF': csrf}
        assert (await client.post('/api/select', json={'scope': 'a' * 32}, headers=headers)).status == 403
        body['sender_id'] = 'stranger'
        response = await client.post('/api/messages', json=body, headers=headers)
        result = await response.json()
        saved = bridge.input_inbox.get(result['id'])
        assert saved['payload']['sender_id'] == 'owner'
        assert saved['payload']['transport'] == 'local_cli'
    finally:
        await client.close()
        await bridge.close()


@pytest.mark.asyncio
async def test_pending_background_report_is_not_claimed_until_render_and_unknown_stays(tmp_path):
    bridge, _ = fixture_bridge(tmp_path)
    client, headers = await client_for(bridge)
    try:
        chat = client.server.app[LOCAL_CHAT]
        route = chat.route(chat.selected_scope)
        message = local_message(route, chat.settings, '活动合成报告')
        with sqlite3.connect(bridge.path) as db:
            for key, status, transport in [('activity-report', 'prepared', 'local_cli'),
                    ('uncertain-report', 'unknown', 'local_cli'), ('wechat-report', 'prepared', 'astrbot')]:
                envelope = message.model_dump()
                envelope['transport'] = transport
                db.execute('INSERT INTO deliveries(id,scope,fingerprint,status,reply_parts,updated,envelope,trigger_kind) '
                    'VALUES(?,?,?,?,?,1,?,?)', (key, route.scope, key, status, json.dumps(['第一段', '第二段']),
                    json.dumps(envelope), 'autonomy'))
        state = await (await client.get('/api/state')).json()
        assert not any(row['role'] == 'user' for row in state['messages'])
        assert len([row for row in state['messages'] if row.get('delivery_id') == 'activity-report' and row['ack_required']]) == 2
        assert all(not row.get('ack_required') for row in state['messages'] if row.get('delivery_id') != 'activity-report')
        for key in ('uncertain-report', 'wechat-report'):
            response = await client.post('/api/receipts', json={'scope': route.scope,
                'delivery_id': key, 'part_index': 0}, headers=headers)
            assert response.status in (403, 409)
        for part in (0, 0, 1):
            response = await client.post('/api/receipts', json={'scope': route.scope,
                'delivery_id': 'activity-report', 'part_index': part}, headers=headers)
            assert response.status == 200
        with sqlite3.connect(bridge.path) as db:
            assert db.execute("SELECT status FROM deliveries WHERE id='activity-report'").fetchone()[0] == 'sent'
            assert db.execute("SELECT status FROM deliveries WHERE id='uncertain-report'").fetchone()[0] == 'unknown'
    finally:
        await client.close()
        await bridge.close()


@pytest.mark.asyncio
async def test_default_existing_owner_context_and_switch_keep_same_library(tmp_path):
    bridge, _ = fixture_bridge(tmp_path)
    route = Incoming(platform_id='xiaowennuan-wechat', bot_id='bot', group_id='owner',
        sender_id='owner', sender_name='fixture', message_id='bootstrap',
        sent_at='2026-10-27T00:00:00+08:00', text='route only', conversation_kind='private', mentioned=False)
    bridge.config['allowed_groups'].append(route.route)
    bridge.register_route(route)
    path = bridge.root
    client, headers = await client_for(bridge)
    try:
        chat = client.server.app[LOCAL_CHAT]
        assert chat.selected_scope == route.scope
        state = await (await client.get('/api/state')).json()
        local_scope = next(row['scope'] for row in state['conversations'] if row['label'] == '本机私聊')
        assert (await client.post('/api/select', json={'scope': local_scope}, headers=headers)).status == 200
        assert bridge.active_local_scopes == {local_scope} and bridge.root == path
        # A stale browser tab must not send into a different conversation.
        response = await client.post('/api/messages', json={'scope': route.scope,
            'text': 'stale tab', 'client_id': str(uuid.uuid4())}, headers=headers)
        assert response.status == 409
    finally:
        await client.close()
        await bridge.close()
