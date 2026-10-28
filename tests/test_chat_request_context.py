"""Synthetic prefix, telemetry, configuration and native-tool continuation checks."""
from copy import deepcopy
import json

import pytest

from myatri.contracts import ModelResponse
from warmagent.request_context import insert_current_context, request_measurement, record_request_usage
from warmagent.monitor import _context_view
from test_reading_conversation_direct import conversation, FakeGateway, FakeHandler, tool_response
from test_warm_runtime import make_bridge, incoming


def test_dynamic_data_keeps_stable_prefix_and_tool_pairs():
    original = [{'role': 'system', 'content': 'fixed persona'},
        {'role': 'user', 'content': 'previous'}, {'role': 'assistant', 'content': 'previous answer'},
        {'role': 'user', 'content': 'current'},
        {'role': 'assistant', 'tool_calls': [{'id': 'call'}]},
        {'role': 'tool', 'tool_call_id': 'call', 'content': 'receipt'}]
    first, second = deepcopy(original), deepcopy(original)
    insert_current_context(first, {'role': 'system', 'content': '宿主环境快照 time=1'})
    insert_current_context(second, {'role': 'system', 'content': '宿主环境快照 time=2'})
    _, encoded = request_measurement(first, [])
    measured, _ = request_measurement(second, [], encoded)
    assert first[:3] == second[:3] == original[:3]
    assert first[-2:] == original[-2:]
    assert first[-3]['content'] == 'current'
    old_first, old_second = deepcopy(original), deepcopy(original)
    old_first.insert(0, first[3])
    old_second.insert(0, second[3])
    _, old = request_measurement(old_first, [])
    old_stats, _ = request_measurement(old_second, [], old)
    assert measured['shared_prefix_bytes'] > old_stats['shared_prefix_bytes']


def test_reported_usage_and_local_bytes_stay_separate_and_private():
    measurement, encoded = request_measurement([
        {'role': 'system', 'content': 'private persona'},
        {'role': 'tool', 'tool_call_id': 'c', 'content': 'private output'}],
        [{'type': 'function', 'function': {'name': 'private_name'}}])
    stats = {'version': 1}
    record_request_usage(stats, measurement, {'prompt_tokens': 100, 'completion_tokens': 10,
        'prompt_tokens_details': {'cached_tokens': 80}})
    record_request_usage(stats, measurement, {'prompt_tokens': 120, 'completion_tokens': 5,
        'prompt_cache_hit_tokens': 90})
    assert stats['request_usage_totals'] == {'received_model_calls': 2, 'reported_usage_calls': 2,
        'cache_reported_calls': 2, 'prompt_tokens': 220,
        'completion_tokens': 15, 'prompt_cache_hit_tokens': 170}
    projected = _context_view(stats)
    assert projected['request_context']['components']['tool_schemas']['messages'] == 1
    assert projected['request_usage_totals']['prompt_cache_miss_tokens'] is None
    assert 'private' not in json.dumps(projected)
    assert measurement['serialized_bytes'] == len(encoded)
    assert measurement['shared_prefix_bytes'] is None


@pytest.mark.asyncio
async def test_native_tool_rounds_keep_rules_first_and_count_each_call(tmp_path):
    gateway = FakeGateway(tool_response(), ModelResponse(provider='fixture', model='fixture', content='完成。',
        usage={'prompt_tokens': 200, 'completion_tokens': 5, 'prompt_cache_hit_tokens': 100}))
    turn = conversation(tmp_path, gateway, FakeHandler())
    async def build(text, **kwargs):
        return [{'role': 'system', 'content': 'fixed persona'},
                {'role': 'user', 'content': text}], 'snapshot-1'
    turn.context.abuild_context = build
    await turn.respond('运行夹具', message_id='prefix-turn')
    for request in gateway.requests:
        assert request.messages[0]['content'] == 'fixed persona'
        assert not request.messages[0]['content'].startswith('宿主环境快照')
        latest_user = max(i for i, m in enumerate(request.messages) if m['role'] == 'user')
        assert request.messages[latest_user-1]['content'].startswith('宿主环境快照')
    result = turn.protocol_store.get_protocol('prefix-turn')['context_stats']
    assert result['request_usage_totals']['received_model_calls'] == 2
    assert result['request_usage_totals']['reported_usage_calls'] == 1
    assert result['request_usage_totals']['prompt_tokens'] == 200
    assert result['request_context']['components']['tool_results']['messages'] == 1
    assert result['request_context']['shared_prefix_bytes'] is not None


@pytest.mark.asyncio
async def test_savings_override_is_chat_local_and_can_be_disabled(tmp_path):
    bridge, _ = make_bridge(tmp_path)
    before = deepcopy(bridge.engine_config)
    bridge.config['chat_context_savings'] = {'enabled': True}
    try:
        app = bridge._application(incoming('private', sender='owner'), start_workers=False)
        context = app.conversation.context
        assert context.dialogue_turns == 20 and context.timeline_limit == 40
        assert context.recall_budget == 10000 and context.summary_budget_bytes == 12000
        assert context.imported_history_budget_bytes == 16000
        assert app.conversation.reasoning_effort == 'high'
        assert bridge.engine_config == before
        bridge.config['chat_context_savings']['enabled'] = False
        other = bridge._application(incoming('group', sender='owner'), start_workers=False)
        assert other.conversation.context.dialogue_turns == 80
        assert other.conversation.context.recall_budget == before['context']['recall_budget_chars']
    finally:
        await bridge.close()
