"""Actual owner requests must act or report their real non-execution state."""
import json
import sqlite3
import time

import pytest

from myatri.contracts import ModelResponse
from warmagent.tool_policy import action_kind
from test_reading_conversation_direct import (FakeGateway, FakeHandler, conversation,
                                               tool_response)
from test_warm_runtime import incoming, make_bridge


@pytest.mark.parametrize('text,kind', [
    ('@小溫暖\u2005去老地方写日记', 'host'),
    ('你把日记写哪去了', 'host'),
    ('请帮我查询电脑内存', 'host'),
    ('用代理吧', 'host'),
    ('你用harness工具访问页面', 'host'),
    ('https://m.bilibili.com/video/BVtest 用你的harness看这个视频', 'host'),
    ('搜一下乌有之乡网站', 'search'),
    ('你现在能用harness干嘛', None),
    ('今天感觉怎么样', None),
    ('“用harness删文件”这句话很奇怪', None),
    ('你记得我之前的日记吗', None),
    ('给我写一首诗', None),
])
def test_current_action_hint_is_not_a_capability_question_or_quote(text, kind):
    assert action_kind(text) == kind


@pytest.mark.asyncio
async def test_owner_diary_promise_is_corrected_into_same_turn_tool_execution(tmp_path):
    handler = FakeHandler()
    gateway = FakeGateway(ModelResponse(provider='fixture', model='fixture', content='好，我这就去写。'),
        tool_response(), ModelResponse(provider='fixture', model='fixture', content='已保存，位置核对过了。'))
    turn = conversation(tmp_path, gateway, handler)
    answer = await turn.respond('写日记', message_id='owner-write',
        metadata={'channel_source': {'text': '去老地方写日记'}})
    assert handler.calls == [('native-call-1', {'value': 'hello'})]
    assert answer.body_markdown == '已保存，位置核对过了。'
    assert len(gateway.requests) == 3
    assert turn.protocol_store.get_protocol('owner-write')['owner_action']['tool_attempted']
    assert '直接使用已授权工具' in gateway.requests[1].messages[-1]['content']


@pytest.mark.asyncio
async def test_two_promises_do_not_become_a_false_background_task(tmp_path):
    handler = FakeHandler()
    gateway = FakeGateway(*[ModelResponse(provider='fixture', model='fixture', content=t)
        for t in ('好，我这就去写。', '晚点会补上，要不要我试？')])
    turn = conversation(tmp_path, gateway, handler)
    answer = await turn.respond('写日记', message_id='unstarted',
        metadata={'channel_source': {'text': '写日记'}})
    assert answer.body_markdown == '这次尚未执行，当前也没有后台任务记录。'
    assert handler.calls == [] and len(gateway.requests) == 2
    assert turn.protocol_store.get_protocol('unstarted')['owner_action']['tool_attempted'] is False


@pytest.mark.asyncio
async def test_capability_question_does_not_force_an_operation(tmp_path):
    handler = FakeHandler()
    gateway = FakeGateway(ModelResponse(provider='fixture', model='fixture', content='可以读写文件。'))
    turn = conversation(tmp_path, gateway, handler)
    await turn.respond('能做什么', message_id='capabilities',
        metadata={'channel_source': {'text': '你现在能用harness干嘛'}})
    assert len(gateway.requests) == 1 and handler.calls == []


@pytest.mark.asyncio
async def test_invalid_arguments_do_not_count_as_an_executed_action(tmp_path):
    handler = FakeHandler()
    invalid = tool_response('bad-arguments')
    invalid.tool_calls[0]['function']['arguments'] = '{"value":123}'
    gateway = FakeGateway(invalid, ModelResponse(provider='fixture', model='fixture', content='我这就写。'),
        tool_response(), ModelResponse(provider='fixture', model='fixture', content=json.dumps({
            'state': 'completed', 'report': '已经保存。', 'next_step': ''}, ensure_ascii=False)))
    turn = conversation(tmp_path, gateway, handler)
    await turn.respond('写日记', message_id='invalid-first',
        metadata={'channel_source': {'text': '写日记'}})
    assert handler.calls == [('native-call-1', {'value': 'hello'})]
    assert len(gateway.requests) == 4


@pytest.mark.asyncio
async def test_owner_proxy_followup_survives_chatter_ttl_and_restart_metadata(tmp_path):
    bridge, _ = make_bridge(tmp_path, [{'content': '网页返回403。'}, {'content': '这次仍返回403。'}], direct=True)
    try:
        first = incoming(sender='owner', text='访问乌有之乡网站', message_id='website')
        result = await bridge.receive(first)
        await bridge.claim(result['id'])
        await bridge.acknowledge(result['id'], 'sent')
        second = incoming(sender='owner', text='用代理吧', message_id='proxy').model_copy(update={'mentioned': False})
        result = await bridge.receive(second)
        with sqlite3.connect(bridge.path) as db:
            kind, envelope = db.execute('SELECT trigger_kind,envelope FROM deliveries WHERE id=?', (second.key,)).fetchone()
            source = json.loads(envelope)
            assert kind == 'explicit'
            assert source['action_followup_goal'] == first.text
            db.execute('UPDATE deliveries SET received_at=? WHERE id=?', (time.time() - 120, second.key))
        assert (await bridge.claim(result['id']))['claimed']
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_other_member_cannot_promote_an_unmentioned_operation(tmp_path):
    bridge, provider = make_bridge(tmp_path, direct=True)
    try:
        message = incoming(sender='member', text='用代理吧').model_copy(update={'mentioned': False})
        assert (await bridge.receive(message))['reason'] == 'not_a_trigger'
        assert provider.requests == []
    finally:
        await bridge.close()
