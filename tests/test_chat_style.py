"""Offline assembly checks; fixture replies do not prove naturalness improved."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from myatri.contracts import ModelResponse
from myatri.persona.state import PersonaState
from warmagent.chat_style import CHAT_STYLE_RULES, apply_chat_style
from warmagent.proactive import Candidate
from test_warm_runtime import incoming, make_bridge
from test_reading_conversation_direct import conversation, FakeGateway, FakeHandler, tool_response
from test_chat_batches import report


def test_guidance_is_small_idempotent_and_rollback_keeps_user_text_and_receipts():
    messages = [{'role': 'system', 'content': 'existing persona and examples'},
        {'role': 'user', 'content': '自然聊天表达/这是用户引用的原话'},
        {'role': 'assistant', 'tool_calls': [{'id': 'c'}]},
        {'role': 'tool', 'tool_call_id': 'c', 'content': 'unconfirmed receipt'}]
    original = deepcopy(messages)
    styled = apply_chat_style(messages, True)
    assert styled[0] == messages[0]
    assert styled[1]['content'] == CHAT_STYLE_RULES
    assert apply_chat_style(styled, True) == styled
    assert apply_chat_style(styled, False) == original
    assert messages == original and styled[-2:] == original[-2:]
    assert len(CHAT_STYLE_RULES.encode()) < 1200


@pytest.mark.asyncio
@pytest.mark.parametrize(('kind', 'transport'), [('group', 'astrbot'), ('private', 'astrbot'), ('private', 'local_cli')])
async def test_style_reaches_group_private_local_without_persona_edits_or_extra_call(tmp_path, kind, transport):
    bridge, provider = make_bridge(tmp_path, [ModelResponse(provider='fixture', model='fixture', content='在呢。')])
    bridge.config['chat_style'] = {'enabled': True}
    bridge.config['cache_prefix'] = {'enabled': True}
    message = incoming(kind, sender='owner', text='在吗').model_copy(update={'transport': transport})
    app = bridge._application(message, start_workers=False)
    before = PersonaState(app.store).read_state()
    try:
        await bridge.receive(message)
        assert len(provider.requests) == 1
        request = provider.requests[0]
        assert sum(m.get('content') == CHAT_STYLE_RULES for m in request.messages) == 1
        assert request.messages[1]['content'] == CHAT_STYLE_RULES
        for entry in ('P07_language', 'P10_examples'):
            assert before.payload['entries'][entry] in request.messages[0]['content']
        after = PersonaState(app.store).read_state()
        assert after.ref == before.ref and after.body_markdown == before.body_markdown
        assert request.reasoning_effort == 'high'
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_style_survives_tool_batch_and_can_be_removed_when_resuming(tmp_path):
    gateway = FakeGateway(tool_response(), report(), report('completed', '已完成。', ''))
    turn = conversation(tmp_path, gateway, FakeHandler())
    turn.max_steps = 2
    turn.context.chat_style_enabled = True
    first = await turn.respond('完成这个任务', message_id='style-batch')
    assert first.payload['partial'] and gateway.requests[-1].output_schema
    assert all(sum(m.get('content') == CHAT_STYLE_RULES for m in r.messages) == 1 for r in gateway.requests)
    turn.context.chat_style_enabled = False
    await turn.respond('完成这个任务', message_id='style-batch', resume_batch=True)
    assert not any(m.get('content') == CHAT_STYLE_RULES for m in gateway.requests[-1].messages)
    assert any(m.get('role') == 'tool' for m in gateway.requests[-1].messages)


@pytest.mark.asyncio
async def test_proactive_generation_uses_same_guidance_without_new_human_turn(tmp_path):
    bridge, provider = make_bridge(tmp_path, [ModelResponse(provider='fixture', model='fixture', content='在看书呢。')])
    bridge.config['chat_style'] = {'enabled': True}
    message = incoming('private', sender='owner')
    bridge.register_route(message)
    app = bridge._application(message, start_workers=False)
    before = len(app.store.list_records('raw_message'))
    now = datetime.now(timezone.utc)
    candidate = Candidate(candidate_id='style-proactive', kind='group_revive', created_at=now,
        reason='fixture', context_hash='fixture')
    try:
        result = await bridge._proactive_generate(scope=message.scope, candidate=candidate, now=now, instruction='fixture')
        assert result and len(provider.requests) == 1
        assert sum(m.get('content') == CHAT_STYLE_RULES for m in provider.requests[0].messages) == 1
        assert len(app.store.list_records('raw_message')) == before
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_activity_style_preserves_fixed_context_slots_and_does_not_call_model(tmp_path):
    from test_activity_runner import FakeApp, build
    app = FakeApp([])
    coordinator, runner, _ = build(tmp_path, app, {})
    runner.settings['chat_style_enabled'] = True
    messages = await runner._initial_messages(app, None, 'activity prompt')
    assert messages[3]['content'].startswith('当前活动状态')
    assert messages[0]['content'].count(CHAT_STYLE_RULES) == 1
    runner.settings['chat_style_enabled'] = False
    disabled = await runner._initial_messages(app, None, 'activity prompt')
    assert CHAT_STYLE_RULES not in disabled[0]['content']
    assert not app.provider.requests
