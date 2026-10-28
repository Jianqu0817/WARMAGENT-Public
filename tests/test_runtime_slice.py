"""B1 vertical slice: native receipt -> model -> fold -> read -> final -> ack."""

import json
from pathlib import Path

import pytest

from myatri.contracts import ModelResponse
from myatri.conversation.budget import TokenBudget
from myatri.providers.gateway import validate_protocol
from warmagent.direct_harness import DirectHarness
from warmagent.monitor import _context_view, _runtime_events_view
from warmagent.runtime_events import EventJournal
from warmagent.tool_catalog import ToolCatalog
from test_direct_harness import owner_raw, settings
from test_reading_conversation_direct import FakeGateway, FakeHandler, conversation


def call_response(call_id, name, args):
    return ModelResponse(provider='fixture', model='fixture', content='', tool_calls=[{
        'id': call_id, 'type': 'function', 'function': {
            'name': name, 'arguments': json.dumps(args)}}])


@pytest.mark.asyncio
async def test_real_harness_receipt_reaches_same_model_and_remains_readable(tmp_path):
    scenario = json.loads((Path(__file__).parent / 'fixtures/runtime_slice.json')
        .read_text(encoding='utf-8'))['native_tool_receipt_scenario']
    row = scenario['rounds'][0]
    body = row['receipt_seed'] * row['receipt_repeat_count']

    class Executor:
        effects = 0

        async def start(self):
            return [{'name': 'read_file', 'description': 'read fixture', 'parameters': {
                'type': 'object', 'properties': {'file_path': {'type': 'string'}},
                'required': ['file_path'], 'additionalProperties': False}}]

        async def execute(self, name, args, call_id):
            self.effects += 1
            assert args == {'file_path': 'synthetic/notes.txt'}
            return {'text': body}

        async def close(self):
            pass

    executor = Executor()
    harness = DirectHarness(settings(), tmp_path, 'owner-1', ['wechat|private-1'],
        'live', executor=executor)
    gateway = FakeGateway(
        call_response(row['call_id'], 'harness__read_file', {'filePath': 'synthetic/notes.txt'}),
        call_response('read-existing', 'harness_read_receipt', {'call_id': row['call_id'], 'limit': 2000}),
        ModelResponse(provider='fixture', model='fixture', content='已经读到结果。'))
    turn = conversation(tmp_path, gateway, FakeHandler())
    turn.context.budget = TokenBudget(limit=24000, output_reserve=512, safety=64)
    turn.context.group_scope = scenario['scope']
    turn.runtime_events = EventJournal(tmp_path / 'events.sqlite3')

    async def factory(raw):
        return await harness.handlers(owner_raw(), context={
            'turn_id': raw.record_id, 'scope': scenario['scope']})

    turn.turn_tool_factory = factory
    answer = await turn.respond('读取合成文件', message_id=row['turn_id'])
    assert answer.body_markdown == '已经读到结果。'
    assert executor.effects == 1
    second, third = gateway.requests[1:]
    first_seen = next(m for m in second.messages if m.get('role') == 'tool')
    assert json.loads(first_seen['content'])['result']['text'] == body
    folded = json.loads(next(m for m in third.messages if m.get('role') == 'tool')['content'])
    assert folded['receipt_ref']['turn'] == row['turn_id']
    assert len(folded['summary']) < len(body)
    page = json.loads(third.messages[-1]['content'])
    assert body in page['text']
    protocol = turn.protocol_store.get_protocol(row['turn_id'])
    assert json.loads(next(m['content'] for m in protocol['messages']
        if m.get('role') == 'tool'))['result']['text'] == body
    stats = protocol['context_stats']['tool_context']
    assert stats['folded_results'] == 1
    assert stats['items'][row['call_id']]['lifecycle'] == 'durable'
    assert stats['items'][row['call_id']]['observed_at'] is not None
    assert stats['projected_estimated_tokens'] < stats['original_estimated_tokens']
    for request in gateway.requests:
        validate_protocol(request.messages)
    event_view = _runtime_events_view(tmp_path / 'events.sqlite3')
    assert event_view['pending_tool_results'] == 0
    assert event_view['counts']['tool_result_observed'] == 1
    assert event_view['counts']['report_generated'] == 1
    assert event_view['counts']['delivery_sent'] == 0
    await harness.close()


def test_catalog_discovery_expands_only_authorized_schemas_and_resumes():
    handlers = {f'harness__fixture_{index}': ({'type': 'function', 'function': {
        'name': f'harness__fixture_{index}', 'description': 'fixture capability',
        'parameters': {'type': 'object'}}}, lambda: None) for index in range(10)}
    catalog = ToolCatalog(handlers)
    assert len(catalog.schemas()) == 1
    assert catalog.discover(names=['harness__unavailable'])['error'] == 'unknown_tool_names'
    assert not catalog.expanded
    catalog.discover(names=['harness__fixture_9'])
    assert len(catalog.schemas()) == 2
    assert len(ToolCatalog(handlers, catalog.state()['expanded']).schemas()) == 2
    assert ToolCatalog({}).schemas() == []


def test_monitor_only_exposes_numeric_context_projection():
    value = _context_view({'version': 1, 'tool_context': {
        'original_chars': 9000, 'projected_chars': 500, 'observed_call_ids': ['private-id'],
        'result_events': {'secret': 'body'}}, 'tool_catalog': {
        'loaded_tools': 4, 'available_tools': 50, 'expanded': ['private-tool']}})
    serialized = json.dumps(value)
    assert 'private-' not in serialized and 'secret' not in serialized
    assert value['tool_context']['original_chars'] == 9000


@pytest.mark.asyncio
async def test_activity_uses_same_consumption_projection(tmp_path):
    from test_activity_runner import FakeApp, build, finish_response, response

    body = '合成的活动工具结果。' * 140
    app = FakeApp([finish_response('create', 'create', goal='读取两份资料'),
        response('read-one', 'harness__read_fixture', {}),
        response('read-two', 'harness__read_fixture', {}),
        finish_response('finish', 'complete', summary='完成两次读取', notify_owner='已完成。')],
        token_limit=64000)  # Fit the autonomy gateway's full UTF-8 request estimate.
    effects = []

    async def handler():
        raise AssertionError('must use host call ID')

    async def execute_call(call_id, args):
        effects.append(call_id)
        return {'host_receipt_id': call_id, 'result': {'text': body}}

    handler.execute_call = execute_call
    schema = {'type': 'function', 'function': {'name': 'harness__read_fixture',
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}}
    coordinator, runner, notifications = build(tmp_path, app, {'harness__read_fixture': (schema, handler)})
    journal = EventJournal(tmp_path / 'events.sqlite3')
    runner.settings.update(event_journal=journal, scope_getter=lambda: 'owner-scope')
    for stamp in range(600, 608):
        await coordinator.tick(stamp)
    assert effects == ['read-one', 'read-two']
    first_seen = [m for m in app.provider.requests[2].messages if m.get('role') == 'tool'
        and m.get('tool_call_id') == 'read-one'][0]
    assert json.loads(first_seen['content'])['result']['text'] == body
    last_request = app.provider.requests[-1]
    folded = json.loads(next(m['content'] for m in last_request.messages
        if m.get('tool_call_id') == 'read-one'))
    assert 'summary' in folded and 'result' not in folded
    state = coordinator.overview()
    assert json.loads(next(m['content'] for m in state['protocol']['messages']
        if m.get('tool_call_id') == 'read-one'))['result']['text'] == body
    assert notifications == ['已完成。']
    assert journal.summary('owner-scope')['tool_result_available']['acknowledged'] == 2


@pytest.mark.asyncio
async def test_actual_outbox_ack_is_distinct_from_generated_report(tmp_path):
    from test_warm_runtime import make_bridge, incoming

    bridge, _ = make_bridge(tmp_path, [ModelResponse(provider='fixture', model='fixture', content='已收到。')])
    try:
        delivery = await bridge.receive(incoming('private', sender='owner', text='你好', message_id='event-chain'))
        view = _runtime_events_view(bridge.root / 'events.sqlite3')
        assert view['counts']['report_generated'] == 1
        assert view['counts']['delivery_prepared'] == 1
        assert view['counts']['delivery_sent'] == 0
        assert (await bridge.claim(delivery['id']))['claimed']
        await bridge.acknowledge(delivery['id'], 'unknown')
        view = _runtime_events_view(bridge.root / 'events.sqlite3')
        assert view['counts']['delivery_unknown'] == 1
        assert view['counts']['delivery_sent'] == 0
        assert not (await bridge.claim(delivery['id']))['claimed']
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_escaped_long_receipt_preview_stays_inside_model_byte_budget(tmp_path):
    from test_direct_harness import FakeExecutor

    harness = DirectHarness(settings(), tmp_path, 'owner-1', ['wechat|private-1'],
        'live', executor=FakeExecutor())
    value = harness._model_receipt({'host_receipt_id': 'escaped',
        'result': {'text': '\\"' * 4000}}, 'conversation', 'turn-fixture', 'escaped')
    assert value['result_complete'] is False
    assert len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) <= 5800
    await harness.close()
