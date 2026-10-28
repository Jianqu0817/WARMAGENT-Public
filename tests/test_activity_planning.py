import asyncio
import json
import sqlite3

import pytest

from myatri.contracts import ModelResponse
from myatri.providers.gateway import validate_protocol
from warmagent.activities import ActivityCoordinator
from warmagent.activity_planning import PLAN_TOOL
from warmagent.tool_catalog import ToolCatalog
from test_activity_runner import FakeApp, finish_response, build, tool_call
from test_warm_runtime import incoming, make_bridge


def submit(coordinator, instruction='查找宋画留白资料', mode='next', ref='owner-message@1'):
    return coordinator.plans.submit(source_ref=ref, scope='owner-scope', owner_id='owner',
        source_text='把查找宋画资料排到下一步', mode=mode, instruction=instruction)


@pytest.mark.asyncio
async def test_owner_chat_saves_plan_before_reply_and_nonowner_has_no_tool(tmp_path):
    call = tool_call('plan-call', 'harness__activity_plan', {'mode': 'next', 'instruction': '整理宋画笔记'})
    bridge, _ = make_bridge(tmp_path, [
        ModelResponse(provider='fixture', model='fixture', content='', tool_calls=[call]),
        ModelResponse(provider='fixture', model='fixture', content='已排到下一步。')], autonomous=True)
    try:
        result = await bridge.receive(incoming('private', sender='owner', text='把整理宋画笔记排到下一步'))
        assert result['status'] == 'prepared'
        plan = bridge.activities.plans.view()[0]
        assert plan['status'] == 'pending' and plan['instruction'] == '整理宋画笔记'
        app = bridge._application(incoming('private', sender='owner'))
        record_id, revision = plan['source_ref'].rsplit('@', 1)
        raw = app.store.get_record(record_id, int(revision))
        handlers = await bridge._chat_handlers(raw)
        assert handlers['harness__activity_plan'][1]('next', '整理宋画笔记')['request_id'] == plan['request_id']
        source = dict(raw.payload['channel_source'], sender_id='member', sender_name='用户')
        other = raw.model_copy(update={'payload': {**raw.payload, 'channel_source': source}})
        assert 'harness__activity_plan' not in await bridge._chat_handlers(other)
    finally:
        await bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['next', 'redirect'])
async def test_plan_reaches_next_model_step_and_preserves_checkpoint(tmp_path, mode):
    app = FakeApp([finish_response('finish-new', 'continue', summary='正在整理宋画', next_step='读资料')])
    coordinator, _, _ = build(tmp_path, app, {})
    coordinator.checkpoint(status='active', goal='旧目标', next_step='旧下一步',
        protocol={'activity_id': 'old', 'messages': [], 'pending_calls': [], 'next_call_index': 0},
        receipts=[{'call_id': 'known', 'state': 'completed', 'result': {'ok': True}}])
    plan = submit(coordinator, mode=mode)
    assert (await coordinator.tick(601))['ran']
    state = coordinator.overview()
    assert state['goal'] == ('旧目标' if mode == 'next' else '查找宋画留白资料')
    assert state['next_step'] == '查找宋画留白资料'
    assert state['protocol']['owner_instruction']['previous_next_step'] == '旧下一步'
    assert state['receipts'][0]['call_id'] == 'known'
    assert '查找宋画留白资料' in json.dumps(app.provider.requests[0].messages, ensure_ascii=False)
    assert coordinator.plans.view()[0]['status'] == 'applied'
    with sqlite3.connect(coordinator.path) as db:
        previous = json.loads(db.execute('SELECT previous FROM activity_plans WHERE request_id=?',
            (plan['request_id'],)).fetchone()[0])
    assert previous['goal'] == '旧目标' and previous['next_step'] == '旧下一步'


@pytest.mark.asyncio
async def test_replaces_unexecuted_old_calls_without_replaying_effects(tmp_path):
    app = FakeApp([finish_response('new', 'continue', summary='新安排')])
    effects = []
    old_call = tool_call('old-call', 'old_effect', {})
    coordinator, runner, _ = build(tmp_path, app, {'old_effect': ({'type': 'function',
        'function': {'name': 'old_effect', 'description': 'fixture', 'parameters': {'type': 'object'}}},
        lambda: effects.append('executed'))})
    messages = await runner._initial_messages(app, None, 'fixture')
    messages.append({'role': 'assistant', 'content': '', 'tool_calls': [old_call]})
    coordinator.checkpoint(status='active', goal='旧目标', protocol={'activity_id': 'a',
        'messages': messages, 'activity_context_index': 3,
        'pending_calls': [old_call], 'next_call_index': 0})
    submit(coordinator)
    await coordinator.tick(601)
    assert not effects
    messages = coordinator.overview()['protocol']['messages']
    assert any(m.get('role') == 'tool' and 'superseded_by_owner_plan' in m['content'] for m in messages)
    validate_protocol(messages, complete=False)


@pytest.mark.asyncio
async def test_plan_survives_restart_rest_and_chat_but_does_not_override_manual_pause_or_unknown(tmp_path):
    calls = []
    async def step(activity, **kwargs):
        calls.append(activity)
        return {'action': 'continue', 'summary': 'fixture'}
    path = tmp_path / 'activities.sqlite3'
    coordinator = ActivityCoordinator(path, step, lambda: True, clock=lambda: 0)
    coordinator.checkpoint(status='resting', goal='目标')
    submit(coordinator)
    coordinator = ActivityCoordinator(path, step, lambda: True, clock=lambda: 0)
    coordinator.on_owner_message(now=10)
    assert not (await coordinator.tick(100))['ran']
    assert (await coordinator.tick(191))['ran']
    assert coordinator.plans.pending()  # Fake runner did not consume it.
    coordinator.pause()
    assert not (await coordinator.tick(1000))['ran']
    unknown = ActivityCoordinator(tmp_path / 'unknown.sqlite3', step, lambda: True, clock=lambda: 0)
    unknown.checkpoint(status='unknown', goal='效果待核对')
    submit(unknown)
    assert not (await unknown.tick(2000))['ran']


def test_latest_arrangement_wins_idempotently_and_tool_is_always_visible(tmp_path):
    c = ActivityCoordinator(tmp_path / 'activities.sqlite3', lambda *_: None, lambda: True)
    old = submit(c)
    new = submit(c, '改读经济学', mode='redirect', ref='next-message@1')
    assert submit(c)['status'] == 'superseded'
    assert c.plans.view()[0]['request_id'] == new['request_id']
    assert c.plans.view()[1]['request_id'] == old['request_id']
    handlers = {f'harness__test_{i}': (PLAN_TOOL, lambda: None) for i in range(10)}
    handlers['harness__activity_plan'] = (PLAN_TOOL, lambda: None)
    assert PLAN_TOOL in ToolCatalog(handlers).schemas()


@pytest.mark.asyncio
async def test_cancel_also_cancels_pending_owner_plan(tmp_path):
    calls = []
    c = ActivityCoordinator(tmp_path / 'activity.sqlite3', lambda *_: calls.append('ran'),
        lambda: True, clock=lambda: 0)
    submit(c)
    c.stop()
    assert not c.plans.pending() and c.plans.view()[0]['status'] == 'cancelled'
    assert not (await c.tick(1))['ran']
    assert not calls


@pytest.mark.asyncio
async def test_inserted_step_completion_restores_original_next_step(tmp_path):
    app = FakeApp([finish_response('done', 'complete', summary='插入事项已完成')])
    c, _, _ = build(tmp_path, app, {})
    c.checkpoint(status='active', goal='原目标', next_step='原下一步', protocol={'activity_id': 'original'})
    submit(c)
    await c.tick(601)  # Model plans a finish call.
    await c.tick(602)  # Execute only that internal call.
    state = c.overview()
    assert state['goal'] == '原目标' and state['next_step'] == '原下一步'
    assert state['status'] != 'completed'
    assert state['protocol']['owner_instruction']['status'] == 'completed'
    assert c.plans.view()[0]['status'] == 'completed'


@pytest.mark.asyncio
async def test_owner_plan_starts_from_empty_without_waiting_for_scan(tmp_path):
    app = FakeApp([finish_response('new', 'continue', summary='按安排做')])
    c, _, _ = build(tmp_path, app, {})
    submit(c)
    assert c.schedule_view(1)['reason'] == 'owner_plan_pending'
    assert (await c.tick(1))['ran']
    assert c.overview()['goal'] == '查找宋画留白资料'
    assert c.plans.view()[0]['status'] == 'applied'


@pytest.mark.asyncio
async def test_request_during_effect_waits_for_boundary_and_keeps_receipt(tmp_path):
    app = FakeApp([finish_response('new', 'continue', summary='遵照新安排')])
    started, released = asyncio.Event(), asyncio.Event()
    effects = []
    async def effect():
        started.set()
        await released.wait()
        effects.append('once')
        return {'ok': True}
    definition = {'type': 'function', 'function': {'name': 'effect', 'description': 'fixture',
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}}
    c, runner, _ = build(tmp_path, app, {'effect': (definition, effect)})
    call = tool_call('in-flight', 'effect', {})
    messages = await runner._initial_messages(app, None, 'fixture')
    messages.append({'role': 'assistant', 'content': '', 'tool_calls': [call]})
    c.checkpoint(status='active', goal='旧目标', protocol={'activity_id': 'a', 'messages': messages,
        'activity_context_index': 3, 'pending_calls': [call], 'next_call_index': 0})
    task = asyncio.create_task(c.tick(601))
    await asyncio.wait_for(started.wait(), 2)
    try:
        c.on_owner_message(now=602)
        submit(c, mode='redirect')
    finally:
        released.set()
    await task
    assert c.plans.pending() and effects == ['once']
    assert not (await c.tick(700))['ran']
    assert (await c.tick(783))['ran']
    assert c.overview()['goal'] == '查找宋画留白资料'
    assert effects == ['once']
    assert any(r['call_id'] == 'in-flight' for r in c.overview()['receipts'])
