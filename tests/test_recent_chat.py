"""Synthetic group windows only: no model, device, real database or sends."""
import json
import sqlite3

import pytest

from test_warm_runtime import incoming, make_bridge
from myatri.contracts import ModelResponse
from warmagent.recent_chat import read_recent_group_messages


def _seed(bridge, *, count=24, scope=None, key='turn', kind='group', text='旁听消息'):
    scope = scope or incoming().scope
    messages = [{'message_id': f'm{i}', 'sender_id': f'member{i % 3}',
                 'sender_name': '合成成员', 'text': f'{text}{i}'} for i in range(count)]
    envelope = {'message_id': 'current', 'conversation_kind': kind,
                'recent_messages': [*messages, {'message_id': 'current', 'text': '读最近15条'}]}
    with sqlite3.connect(bridge.path) as db:
        db.execute("INSERT INTO deliveries(id,scope,fingerprint,status,envelope,updated) VALUES(?,?,?,'sent',?,0)",
                   (key, scope, key, json.dumps(envelope)))
    return scope


@pytest.mark.asyncio
async def test_recent_read_counts_messages_pages_and_stays_frozen(tmp_path):
    bridge, provider = make_bridge(tmp_path)
    try:
        scope = _seed(bridge)
        # Later intake must not change the window the original request referred to.
        with sqlite3.connect(bridge.path) as db:
            bridge._cache_message(db, scope, 'later', 'later', {'message_id': 'later', 'text': '后来才到'})
        page = read_recent_group_messages(bridge.path, scope, 'turn', count=15)
        assert [m['message_id'] for m in page['messages']] == [f'm{i}' for i in range(9, 24)]
        assert page['returned_count'] == 15 and page['has_more']
        earlier = read_recent_group_messages(bridge.path, scope, 'turn', count=15,
                                             before_message_id=page['next_before_message_id'])
        assert [m['message_id'] for m in earlier['messages']] == [f'm{i}' for i in range(9)]
        assert not earlier['has_more']
        assert not provider.requests
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_scope_private_and_unknown_cursor_are_unavailable(tmp_path):
    bridge, _ = make_bridge(tmp_path)
    try:
        scope = _seed(bridge)
        _seed(bridge, key='private', kind='private')
        for other_scope, turn, cursor in [('other-group', 'turn', None),
                                          (scope, 'private', None), (scope, 'turn', 'not-retained')]:
            result = read_recent_group_messages(bridge.path, other_scope, turn, before_message_id=cursor)
            assert result['available'] is False and 'messages' not in result
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_preview_is_bounded_and_explicitly_incomplete(tmp_path):
    bridge, _ = make_bridge(tmp_path)
    try:
        scope = _seed(bridge, count=50, text='长' * 3000)
        page = read_recent_group_messages(bridge.path, scope, 'turn', count=50)
        assert 0 < page['returned_count'] < 50 and page['has_more']
        assert len(json.dumps(page, ensure_ascii=False).encode('utf-8')) < 12000
        assert all(not m['text_complete'] and len(m['text']) == 500 for m in page['messages'])
        assert page['next_before_message_id']
        with pytest.raises(ValueError):
            read_recent_group_messages(bridge.path, scope, 'turn', count=True)
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_ordinary_group_member_gets_reader_without_computer_authority(tmp_path):
    bridge, _ = make_bridge(tmp_path)
    try:
        message = incoming(sender='member')
        scope = _seed(bridge, key=message.key)
        app = bridge._application(message, start_workers=False)
        raw = app.store.append_message('user', '读最近15条', message_id=message.key,
                                       channel_source=message.model_dump())
        handlers = await bridge._chat_handlers(raw)
        assert 'read_recent_group_messages' in handlers
        assert not any(name.startswith('harness__terminal') for name in handlers)
        reader = handlers['read_recent_group_messages'][1]
        assert reader(count=15)['returned_count'] == 15
        assert scope == message.scope
        private = message.model_copy(update={'conversation_kind': 'private', 'group_id': 'owner'})
        private_raw = app.store.append_message('user', 'private', message_id='private-tool-check',
                                               channel_source=private.model_dump())
        assert 'read_recent_group_messages' not in await bridge._chat_handlers(private_raw)
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_unmentioned_messages_reach_model_through_read_tool(tmp_path):
    call = {'id': 'recent-call', 'type': 'function', 'function': {
        'name': 'read_recent_group_messages', 'arguments': '{"count":15}'}}
    bridge, provider = make_bridge(tmp_path, [
        ModelResponse(provider='fixture', model='fixture', content='', tool_calls=[call]),
        ModelResponse(provider='fixture', model='fixture', content='刚才在讨论合成话题。')])
    try:
        for i in range(15):
            message = incoming(sender='member', message_id=f'ambient{i}', text=f'这是合成的群聊原文第{i}条')
            result = await bridge.receive(message.model_copy(update={'mentioned': False}))
            assert result['status'] == 'ignored'
        result = await bridge.receive(incoming(message_id='read-request', text='读取最近15条聊天'))
        assert result['status'] == 'prepared' and len(provider.requests) == 2
        assert any(t['function']['name'] == 'read_recent_group_messages' for t in provider.requests[0].tools)
        reply = next(m for m in provider.requests[1].messages if m.get('tool_call_id') == 'recent-call')
        data = json.loads(reply['content'])
        assert data['returned_count'] == 15
        assert [m['message_id'] for m in data['messages']] == [f'ambient{i}' for i in range(15)]
    finally:
        await bridge.close()
