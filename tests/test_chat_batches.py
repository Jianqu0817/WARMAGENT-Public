import asyncio
import json
from contextlib import closing
import sqlite3

import pytest

from myatri.contracts import ModelResponse
from myatri.providers.gateway import ProviderError
from warmagent.local_chat import display_background_reports
from warmagent.server import pending_deliveries
from test_reading_conversation_direct import FakeGateway, FakeHandler, FakeProtocolStore, conversation, tool_response, TOOL_SCHEMA
from test_warm_runtime import make_bridge, incoming


def report(state='continue', text='第一步已完成，下一批继续。', next_step='执行第二步'):
    return ModelResponse(provider='fixture', model='fixture', content=json.dumps({
        'state': state, 'report': text, 'next_step': next_step}, ensure_ascii=False))


@pytest.mark.asyncio
async def test_batch_report_is_within_limit_and_continuation_keeps_one_user_and_receipts(tmp_path):
    handler = FakeHandler()
    gateway = FakeGateway(tool_response('first'), report(), tool_response('second'),
        report('completed', '两步均已完成。', ''))
    turn = conversation(tmp_path, gateway, handler)
    turn.max_steps = 2
    first = await turn.respond('写日记', message_id='batched', metadata={'channel_source': {'text': '写日记'}})
    assert first.payload['partial'] and len(gateway.requests) == 2
    assert gateway.requests[1].tools == [] and gateway.requests[1].output_schema
    again = await turn.respond('写日记', message_id='batched', metadata={'channel_source': {'text': '写日记'}})
    assert first.ref == again.ref and len(gateway.requests) == 2
    final = await turn.respond('写日记', message_id='batched', metadata={'channel_source': {'text': '写日记'}}, resume_batch=True)
    assert final.body_markdown == '两步均已完成。'
    assert handler.calls == [('first', {'value': 'hello'}), ('second', {'value': 'hello'})]
    assert len(turn.store.users) == 1 and len(gateway.requests) == 4
    assert turn.protocol_store.get_protocol('batched')['status'] == 'completed'
    assert turn.protocol_store.get_protocol('batched')['context_stats']['tool_batch']['index'] == 2


@pytest.mark.asyncio
async def test_legacy_exhausted_cursor_only_generates_report_without_replaying_effect(tmp_path):
    store = FakeProtocolStore()
    done = tool_response('already-done').tool_calls[0]
    store.save_protocol('legacy', {'status': 'active', 'snapshot': 'old-snapshot', 'steps': 4,
        'messages': [{'role': 'user', 'content': '任务'}, {'role': 'assistant', 'content': '', 'tool_calls': [done]},
            {'role': 'tool', 'tool_call_id': 'already-done', 'content': '{"ok":true}'}], 'context_stats': {}})
    handler = FakeHandler()
    gateway = FakeGateway(report())
    turn = conversation(tmp_path, gateway, handler, protocol=store)
    answer = await turn.respond('任务', message_id='legacy')
    assert answer.payload['partial'] and len(gateway.requests) == 1 and not handler.calls
    assert gateway.requests[0].tools == []
    assert store.get_protocol('legacy')['steps'] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize('reported_state', ['continue', 'completed'])
async def test_unknown_result_blocks_automatic_batch_continuation(tmp_path, reported_state):
    handler = FakeHandler()
    original = handler.call.execute_call
    async def unknown(call_id, args):
        await original(call_id, args)
        return {'state': 'unknown', 'error': 'outcome_unknown'}
    handler.call.execute_call = unknown
    gateway = FakeGateway(tool_response(), report(reported_state))
    turn = conversation(tmp_path, gateway, handler)
    turn.max_steps = 2
    answer = await turn.respond('任务', message_id='unknown-task')
    assert '停止自动续跑' in answer.body_markdown
    assert turn.protocol_store.get_protocol('unknown-task')['status'] == 'completed'
    assert turn.protocol_store.get_protocol('unknown-task')['context_stats']['tool_batch']['state'] == 'blocked'


@pytest.mark.asyncio
async def test_owner_interrupt_before_effect_reports_and_preserves_resumable_checkpoint(tmp_path):
    handler = FakeHandler()
    gateway = FakeGateway(tool_response('not-started'), report('blocked', '已暂停，原进度保留。', ''))
    turn = conversation(tmp_path, gateway, handler)
    interrupt = []
    gateway.on_generate = lambda count, _: interrupt.append('paused') if count == 1 else None
    turn.batch_should_yield = lambda _: interrupt[-1] if interrupt else ''
    answer = await turn.respond('写日记', message_id='pause-during-model')
    assert answer.payload['partial'] and len(gateway.requests) == 2 and not handler.calls
    protocol = turn.protocol_store.get_protocol('pause-during-model')
    skipped = next(m for m in protocol['messages'] if m.get('tool_call_id') == 'not-started')
    assert json.loads(skipped['content'])['not_executed'] is True
    assert protocol['status'] == 'batch_waiting'


async def prepared_bridge(tmp_path, *, local=False):
    bridge, provider = make_bridge(tmp_path, [tool_response('first'), report(), tool_response('second'),
        report('completed', '两步均已完成。', '')], direct=True)
    bridge.engine_config['chat']['max_tool_steps'] = 2
    effects = []
    async def handlers(raw, *, context):
        async def invoke(**kwargs):
            raise AssertionError('use execute_call')
        async def execute(call_id, args):
            effects.append(call_id)
            return {'ok': True}
        invoke.execute_call = execute
        return {'harness__write': (TOOL_SCHEMA, invoke)}
    bridge.direct.handlers = handlers
    message = incoming('private', sender='owner', text='帮我完成两步写入')
    if local:
        message = message.model_copy(update={'transport': 'local_cli'})
        bridge.active_local_scopes.add(message.scope)
    first = await bridge.receive(message)
    await bridge.claim(first['id'])
    await bridge.acknowledge(first['id'], 'sent')
    return bridge, provider, message, effects


@pytest.mark.asyncio
@pytest.mark.parametrize('local', [False, True])
async def test_worker_resumes_and_reports_through_original_channel_once(tmp_path, local):
    bridge, provider, message, effects = await prepared_bridge(tmp_path, local=local)
    task = asyncio.create_task(bridge._batch_loop())
    try:
        async with asyncio.timeout(8):
            while bridge.chat_batches.state(message.key) != 'completed':
                await asyncio.sleep(.01)
        assert effects == ['first', 'second'] and len(provider.requests) == 4
        deliveries = pending_deliveries(bridge.path, message.scope)
        assert len(deliveries) == 1 and deliveries[0]['id'].startswith('batch_report_')
        assert deliveries[0]['reply_parts'] == ['两步均已完成。']
        if local:
            lines = []
            await display_background_reports(bridge, message.scope, lines.append)
            await display_background_reports(bridge, message.scope, lines.append)
            assert lines == ['小温暖 > 两步均已完成。']
        app = bridge.apps[message.scope]
        assert len([r for r in app.store.list_records('raw_message') if r.payload.get('role') == 'user']) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bridge.close()


@pytest.mark.asyncio
async def test_pause_cancel_recovery_and_model_error_are_visible(tmp_path):
    bridge, provider, message, effects = await prepared_bridge(tmp_path)
    try:
        bridge.chat_batches.control(message.scope, 'pause')
        assert bridge.chat_batches.claim(local=False, scopes=set()) is None
        bridge.chat_batches.control(message.scope, 'resume')
        assert bridge.chat_batches.claim(local=True, scopes={message.scope}) is None
        row = bridge.chat_batches.claim(local=False, scopes=set())
        assert row and bridge.chat_batches.state(message.key) == 'running'
        bridge.chat_batches.recover()
        assert bridge.chat_batches.state(message.key) == 'pending'
        async def failed(request):
            raise ProviderError('fixture model unavailable')
        bridge.apps[message.scope].conversation.gateway.generate = failed
        task = asyncio.create_task(bridge._batch_loop())
        try:
            async with asyncio.timeout(8):
                while bridge.chat_batches.state(message.key) != 'blocked':
                    await asyncio.sleep(.01)
            errors = pending_deliveries(bridge.path, message.scope)
            assert len(errors) == 1 and '续跑受阻' in errors[0]['reply_parts'][0]
            assert effects == ['first']
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        bridge.chat_batches.control(message.scope, 'resume')
        assert bridge.chat_batches.state(message.key) == 'pending'
        bridge.chat_batches.control(message.scope, 'cancel')
        bridge.chat_batches.queue(message.key, message.scope, 1)
        assert bridge.chat_batches.state(message.key) == 'cancelled'
        with closing(sqlite3.connect(bridge.path)) as db:
            assert db.execute('SELECT reply FROM deliveries WHERE id=?', (message.key,)).fetchone()[0]
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_task_control_preempts_focus_only_for_stable_owner(tmp_path):
    bridge, _ = make_bridge(tmp_path)
    try:
        bridge.start()
        assert bridge.batch_task is None  # Offline mode must not consume live task checkpoints.
        assert bridge._urgent_input(incoming(sender='owner', text='/task pause'))
        assert not bridge._urgent_input(incoming(sender='member', text='/task pause'))
    finally:
        await bridge.close()
