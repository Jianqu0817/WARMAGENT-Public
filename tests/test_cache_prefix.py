"""Synthetic cache-friendly projections, with fresh sources and unchanged tool protocols."""
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from myatri.contracts import ModelResponse
from warmagent.request_context import request_measurement
from warmagent.group_prompt import GROUP_RULES
from test_chat_context_selection import context_factory, _raw, _run  # noqa: F401, F811
from test_reading_conversation_direct import conversation, FakeGateway, FakeHandler, tool_response


def _seed(fixture, start, stop):
    context = fixture.context
    rows = []
    for i in range(start, stop):
        user = _raw(context.store, f'prefix-u{i}', f'第{i}轮的散步安排。' * 8)
        answer = _raw(context.store, f'prefix-a{i}', f'第{i}轮的回答。', role='assistant', reply_to=user.ref)
        stamp = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=i*2)
        user = user.model_copy(update={'created_at': stamp.isoformat()})
        answer = answer.model_copy(update={'created_at': (stamp+timedelta(seconds=1)).isoformat()})
        rows.extend((user, answer))
    context.store.commit(rows, f'seed-prefix-{start}-{stop}')
    with closing(sqlite3.connect(fixture.bridge.path)) as db:
        db.executemany("INSERT INTO deliveries(id,scope,fingerprint,status,updated) VALUES(?,?,?,'sent',0)",
            [(f'prefix-u{i}', context.group_scope, f'prefix-{i}') for i in range(start, stop)])
        db.commit()


def _history_ids(explanation):
    return [row['message_id'] for row in explanation['history_selection']]


def test_dynamic_recall_changes_after_confirmed_history(context_factory):  # noqa: F811
    fixture = context_factory
    fixture.context.cache_prefix_enabled = True
    fixture.context.dialogue_turns = 20
    _seed(fixture, 0, 12)
    fixture.recall_rows = [{'kind': 'event', 'ref': 'event-a@1', 'text': '昨天的线索'}]
    first, first_info = _run(fixture, text='昨天的线索')
    fixture.recall_rows = [{'kind': 'event', 'ref': 'event-b@2', 'text': '刚刚明确更正的线索'}]
    second, second_info = _run(fixture, text='换一个问题')
    assert first[:26] == second[:26]  # persona, channel rules and 12 confirmed pairs
    assert sum(m.get('content') == GROUP_RULES for m in second) == 1
    assert 'event-b@2' in json.dumps(second, ensure_ascii=False)
    assert 'event-a@1' not in json.dumps(second, ensure_ascii=False)
    assert _history_ids(first_info) == _history_ids(second_info)
    _, encoded = request_measurement(first, [])
    stable, _ = request_measurement(second, [], encoded)
    fixture.context.cache_prefix_enabled = False
    fixture.recall_rows = [{'kind': 'event', 'ref': 'event-a@1', 'text': '昨天的线索'}]
    legacy_first, _ = _run(fixture, text='昨天的线索')
    fixture.recall_rows = [{'kind': 'event', 'ref': 'event-b@2', 'text': '刚刚明确更正的线索'}]
    legacy_second, _ = _run(fixture, text='换一个问题')
    _, encoded = request_measurement(legacy_first, [])
    legacy, _ = request_measurement(legacy_second, [], encoded)
    assert stable['shared_prefix_bytes'] > legacy['shared_prefix_bytes'] + 1000


def test_history_appends_and_rotates_only_on_block_boundary(context_factory):  # noqa: F811
    fixture = context_factory
    context = fixture.context
    context.cache_prefix_enabled = True
    context.cache_history_block_turns = 4
    context.dialogue_turns = 20
    _seed(fixture, 0, 20)
    previous, info = _run(fixture)
    assert info['context_stats']['cache_prefix']['history_start'] == 0
    for count, expected_start in [(21, 4), (22, 4), (23, 4), (24, 4), (25, 8)]:
        _seed(fixture, count-1, count)
        messages, info = _run(fixture)
        actual = _history_ids(info)
        assert actual == [f'prefix-u{i}' for i in range(expected_start, count)]
        assert len(actual) <= 20
        if count in (22, 23, 24):
            previous_pairs = 2 + 2 * (count-1-expected_start)
            assert messages[:previous_pairs] == previous[:previous_pairs]
        previous = messages


def test_explicit_old_reply_is_dynamic_and_does_not_replace_recent_prefix(context_factory):  # noqa: F811
    fixture = context_factory
    fixture.context.cache_prefix_enabled = True
    fixture.context.dialogue_turns = 20
    _seed(fixture, 0, 25)
    ordinary, _ = _run(fixture)
    quoted, info = _run(fixture, reply_to_id='prefix-u0')
    assert quoted[:36] == ordinary[:36]  # persona, channel rules and 17 recent pairs
    assert 'prefix-u0' in _history_ids(info)
    assert len(_history_ids(info)) <= 20
    label = next(i for i, message in enumerate(quoted)
        if message.get('content', '').startswith('本轮明确引用的旧往返'))
    assert json.loads(quoted[label+1]['content'])['message_id'] == 'prefix-u0'
    assert json.loads(quoted[-1]['content'].split('\n', 1)[1])['message_id'] == 'current'


@pytest.mark.asyncio
async def test_cache_layout_native_continuation_uses_original_receipt(tmp_path):
    gateway = FakeGateway(tool_response(), ModelResponse(provider='fixture', model='fixture', content='完成。'))
    handler = FakeHandler()
    turn = conversation(tmp_path, gateway, handler)
    turn.context.cache_prefix_enabled = True
    source = [{'role': 'system', 'content': 'persona'},
        {'role': 'user', 'content': 'old question'}, {'role': 'assistant', 'content': 'old answer'},
        {'role': 'system', 'content': 'fresh state'}, {'role': 'user', 'content': 'current'}]
    async def build(*args, **kwargs):
        return deepcopy(source), 'snapshot-1'
    turn.context.abuild_context = build
    await turn.respond('current', message_id='cache-native')
    for request in gateway.requests:
        assert request.messages[:3] == source[:3]
    assert len(handler.calls) == 1
    result = next(m for m in gateway.requests[1].messages if m.get('role') == 'tool')
    assert result['tool_call_id'] == handler.calls[0][0]
