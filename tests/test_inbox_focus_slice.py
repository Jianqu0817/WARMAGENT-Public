"""Persisted input -> focus release -> same main model -> confirmed channel outbox."""

import sqlite3
from types import SimpleNamespace

import pytest

from myatri.contracts import ModelResponse
from warmagent.server import ActivityControl
from warmagent.wechat import WeChatRunner
from test_warm_runtime import make_bridge, incoming


def reply(text='收到留言。'):
    return ModelResponse(provider='fixture', model='fixture', content=text)


@pytest.mark.asyncio
async def test_focus_message_is_durable_then_delivered_without_proactive_enabled(tmp_path):
    bridge, provider = make_bridge(tmp_path, [reply()])
    message = incoming('private', sender='owner', text='做完再陪我聊', message_id='focus-held')
    sent = []

    async def send(text, target=None):
        sent.append((text, target))

    try:
        bridge.activities.checkpoint(goal='整理合成资料', summary='正在整理',
            protocol={'activity_id': 'activity-focus-slice'}, status='active')
        bridge.activities.set_focus('focus_first', duration_seconds=300)
        queued = await bridge.receive(message)
        assert queued['status'] == 'queued' and queued['reason'] == 'focus'
        assert provider.requests == []
        assert bridge.input_inbox.get(message.key)['payload']['text'] == message.text
        assert (await bridge.receive(message))['status'] == 'queued'
        assert bridge.input_inbox.summary()['pending'] == 1
        assert await bridge._process_next_input() is None
        bridge.control_activity('set_focus', policy='chat_first')
        await bridge._process_next_input()
        assert len(provider.requests) == 1
        assert bridge.input_inbox.get(message.key)['state'] == 'completed'
        runner = WeChatRunner(bridge, SimpleNamespace(group_id='owner', send=send))
        runner.scope_targets[message.scope] = 'owner'
        await runner.poll()
        await runner.poll()
        assert len(sent) == 1
        with sqlite3.connect(bridge.path) as db:
            assert db.execute('SELECT status FROM deliveries WHERE id=?', (message.key,)).fetchone()[0] == 'sent'
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_owner_urgent_pause_preempts_focus_and_non_owner_cannot(tmp_path):
    bridge, provider = make_bridge(tmp_path, [reply('已暂停。')])
    try:
        bridge.activities.checkpoint(goal='合成活动', protocol={'activity_id': 'urgent-task'}, status='active')
        bridge.activities.set_focus('focus_first', duration_seconds=300)
        outsider = incoming('private', sender='stranger', text='暂停任务', message_id='bad-principal')
        assert (await bridge.receive(outsider))['reason'] == 'private_owner_required'
        assert bridge.input_inbox.get(outsider.key) is None
        assert bridge.activities.overview()['status'] == 'active'
        response = await bridge.receive(incoming('private', sender='owner', text='暂停任务', message_id='urgent-pause'))
        assert response['status'] == 'prepared'
        state = bridge.activities.overview()
        assert state['status'] == 'paused' and state['last_chat'] is None
        assert len(provider.requests) == 1
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_restart_recovers_cross_database_handoff_without_regenerating(tmp_path):
    bridge, provider = make_bridge(tmp_path, [reply()])
    message = incoming('private', sender='owner', message_id='recover-prepared')
    prepared = await bridge.receive(message)
    assert prepared['status'] == 'prepared' and len(provider.requests) == 1
    with sqlite3.connect(bridge.input_inbox.path) as db:
        # Simulate crash after the outbox committed, before the inbox committed completion.
        db.execute("UPDATE runtime_inputs SET state='processing',lease_until=0,result=NULL WHERE key=?", (message.key,))
    await bridge.close()
    restored, fresh_provider = make_bridge(tmp_path)
    try:
        recovered = await restored._process_next_input()
        assert recovered['status'] == 'prepared'
        assert fresh_provider.requests == []
        assert restored.input_inbox.get(message.key)['state'] == 'completed'
        assert (await restored.claim(message.key))['claimed']
        await restored.acknowledge(message.key, 'unknown')
        assert (await restored.receive(message))['status'] == 'unknown'
        assert not (await restored.claim(message.key))['claimed']
        assert fresh_provider.requests == []
    finally:
        await restored.close()


def test_activity_controls_accept_only_action_specific_bounded_fields():
    assert ActivityControl(action='set_focus', policy='adaptive', duration_seconds=60).policy == 'adaptive'
    for payload in ({'action': 'pause', 'policy': 'focus_first'}, {'action': 'set_focus'},
                    {'action': 'set_focus', 'policy': 'focus_first', 'duration_seconds': float('inf')},
                    {'action': 'set_focus', 'policy': 'focus_first', 'duration_seconds': 3601}):
        with pytest.raises(ValueError):
            ActivityControl.model_validate(payload)
