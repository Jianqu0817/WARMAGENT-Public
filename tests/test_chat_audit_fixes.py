"""Chat audit regressions: synthetic inputs, fake models and no real sends."""
import asyncio
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from myatri.contracts import ModelResponse
from warmagent.proactive import Candidate
from warmagent.runtime_inbox import RuntimeInbox
from warmagent.server import pending_deliveries
from warmagent.tool_context import environment_message
from warmagent.tool_policy import image_capability_denial
from test_chat_batches import report
from test_group_restraint import silent
from test_reading_conversation_direct import FakeContext, FakeGateway, FakeHandler, TOOL_SCHEMA, conversation, tool_response
from test_warm_runtime import make_bridge, incoming


def test_only_newer_owner_inputs_interrupt_even_when_clock_moves_back(tmp_path):
    inbox = RuntimeInbox(tmp_path / 'inbox.sqlite3')
    inbox.enqueue('old', 'room', 'a', {'sender_id': 'owner'}, now=100)
    inbox.claim(key='old', now=100)
    inbox.enqueue('new', 'room', 'b', {'sender_id': 'owner'}, now=90)
    inbox.claim(key='new', now=100)
    assert inbox.owner_waiting('owner', 'room', exclude='old')
    assert not inbox.owner_waiting('owner', 'room', exclude='new')
    assert not inbox.owner_waiting('owner', 'other', exclude='old')
    assert not inbox.owner_waiting('other-user', 'room', exclude='old')
    # Legacy batch checkpoints without an inbox row still yield to pending owner input.
    assert inbox.owner_waiting('owner', 'room', exclude='legacy-checkpoint')


@pytest.mark.asyncio
async def test_back_to_back_private_inputs_keep_newest_request_executable(tmp_path):
    handler = FakeHandler()
    bridge, provider = make_bridge(tmp_path, [tool_response('not-executed'),
        report('blocked', '先接你后面的话，原进度保留。', ''), {'content': '接到你补充的消息了。'}])
    first = incoming('private', sender='owner', text='照片在notes/photo里', message_id='first')
    second = incoming('private', sender='owner', text='看看吧', message_id='second')
    app = bridge._application(first)
    app.conversation.context = FakeContext()
    app.conversation.context.budget.limit = 200000
    async def factory(_):
        return {'harness__write': (TOOL_SCHEMA, handler.call)}
    app.conversation.turn_tool_factory = factory
    entered, release = asyncio.Event(), asyncio.Event()
    original = provider.generate
    async def generate(request):
        if not provider.requests:
            entered.set()
            await release.wait()
        return await original(request)
    provider.generate = generate
    first_task = asyncio.create_task(bridge.receive(first))
    second_task = None
    try:
        async with asyncio.timeout(8):
            await entered.wait()
            second_task = asyncio.create_task(bridge.receive(second))
            while not bridge.input_inbox.get(second.key):
                await asyncio.sleep(.01)
            release.set()
            first_result, second_result = await asyncio.gather(first_task, second_task)
        assert first_result['status'] == second_result['status'] == 'prepared'
        assert app.queue.get_protocol(first.key)['status'] == 'batch_waiting'
        assert app.queue.get_protocol(second.key)['context_stats']['tool_batch']['state'] == 'completed'
        assert len(provider.requests) == 3 and not handler.calls
        assert provider.requests[1].output_schema
        assert provider.requests[2].output_schema is None
    finally:
        release.set()
        for task in (first_task, second_task):
            if task and not task.done():
                task.cancel()
            if task:
                await asyncio.gather(task, return_exceptions=True)
        await bridge.close()


IMAGE_SCHEMA = {'type': 'function', 'function': {'name': 'harness__read_image',
    'description': 'Read a local image', 'parameters': {'type': 'object', 'properties': {
        'file_path': {'type': 'string'}}, 'required': ['file_path'], 'additionalProperties': False}}}


def image_turn(tmp_path, *responses, enabled=True):
    handler = FakeHandler()
    gateway = FakeGateway(*responses)
    gateway.vision_root = tmp_path if enabled else None
    turn = conversation(tmp_path, gateway, handler)
    async def factory(_):
        return {'harness__read_image': (IMAGE_SCHEMA, handler.call)}
    turn.turn_tool_factory = factory
    return turn, gateway, handler


@pytest.mark.asyncio
async def test_false_visual_denial_gets_one_existing_budgeted_correction_and_tool_result(tmp_path):
    call = {'id': 'read-photo', 'type': 'function', 'function': {
        'name': 'harness__read_image', 'arguments': '{"file_path":"notes/photo/photo.png"}'}}
    turn, gateway, handler = image_turn(tmp_path,
        ModelResponse(provider='fixture', model='fixture', content='图片内容我目前读不了，只能读到文字。'),
        ModelResponse(provider='fixture', model='fixture', content='', tool_calls=[call]),
        ModelResponse(provider='fixture', model='fixture', content='已收到读图工具的结果。'))
    answer = await turn.respond('看看吧', message_id='image', metadata={
        'channel_source': {'text': '看看吧', 'conversation_kind': 'private'}})
    assert answer.body_markdown == '已收到读图工具的结果。'
    assert len(gateway.requests) == 3 and len(handler.calls) == 1
    assert turn.protocol_store.get_protocol('image')['context_stats']['capability_corrected']


@pytest.mark.asyncio
async def test_repeat_visual_denial_is_bounded_and_preserves_other_result_text(tmp_path):
    denial = ModelResponse(provider='fixture', model='fixture', content='目录里有两张图片。图片内容我读不了，只能读文字。')
    turn, gateway, _ = image_turn(tmp_path, denial, denial)
    answer = await turn.respond('看看吧', message_id='bounded-image')
    assert len(gateway.requests) == 2
    assert '目录里有两张图片。' in answer.body_markdown
    assert not image_capability_denial(answer.body_markdown)


@pytest.mark.asyncio
async def test_disabled_vision_and_specific_file_errors_are_not_capability_claims(tmp_path):
    denial = ModelResponse(provider='fixture', model='fixture', content='图片内容我读不了。')
    turn, gateway, _ = image_turn(tmp_path, denial, enabled=False)
    assert (await turn.respond('看看吧', message_id='no-vision')).body_markdown == denial.content
    assert len(gateway.requests) == 1
    assert not image_capability_denial('这张图片无法读取，文件不存在。')
    assert not image_capability_denial('我无法读取损坏的图片。')


@pytest.mark.asyncio
async def test_host_interruption_is_resumable_and_does_not_report_missing_vision(tmp_path):
    turn, gateway, _ = image_turn(tmp_path, report('blocked', '图片内容我目前读不了，只能读文字。', ''))
    turn.batch_should_yield = lambda _: 'owner_message'
    answer = await turn.respond('看看吧', message_id='interrupted-image')
    assert answer.payload['partial']
    assert not image_capability_denial(answer.body_markdown)
    assert len(gateway.requests) == 1 and gateway.requests[0].tools == []
    assert any('调度暂停' in m.get('content', '') for m in gateway.requests[0].messages)


def seed_topic(bridge, scope, texts):
    for index, text in enumerate(texts):
        message = {'message_id': str(index), 'sender_id': 'member', 'sender_name': '成员',
            'text': text, 'sent_at': datetime.now(timezone.utc).isoformat()}
        message['topic_id'] = bridge.topics.add(scope, message)
        bridge.short_memory.add(scope, message)


@pytest.mark.asyncio
async def test_web_curiosity_uses_complete_source_text_and_skips_local_file_chatter(tmp_path):
    bridge, _ = make_bridge(tmp_path)
    try:
        seed_topic(bridge, 'files', ['你的note文件夹下，photo里面是你形象的照片', '看看吧', '12_gaze_simple.png'])
        assert bridge._web_topic_query('files') is None
        text = '政治经济学里的复杂劳动如何折算成简单劳动？'
        seed_topic(bridge, 'learning', [text, text, text])
        assert bridge._web_topic_query('learning') == text
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_proactive_can_decline_once_with_no_message_and_durable_consumption(tmp_path):
    bridge, provider = make_bridge(tmp_path, [silent(), {'content': '这条材料确实有意思。'}])
    route = incoming('private', sender='owner')
    bridge.register_route(route)
    app = bridge._application(route)
    app.conversation.context = FakeContext()
    now = datetime(2026, 10, 26, 3, 35, tzinfo=timezone.utc)
    candidate = Candidate(candidate_id='boring-material', kind='web_curiosity', created_at=now,
        reason='新材料', excerpt='与照片无关的文件安全文章', context_hash='fixture')
    original_count = len(app.store.list_records('raw_message'))
    try:
        result = await bridge._proactive_generate(scope=route.scope, candidate=candidate, now=now, instruction='自主开口机会')
        assert result is None and len(provider.requests) == 1
        assert len(app.store.list_records('raw_message')) == original_count
        assert pending_deliveries(bridge.path, route.scope) == []
        with sqlite3.connect(bridge.path) as db:
            assert db.execute("SELECT status FROM deliveries WHERE trigger_kind='proactive'").fetchall() == [('skipped',)]
            assert db.execute('SELECT COUNT(*) FROM proactive_sources').fetchone()[0] == 1
        assert await bridge._proactive_generate(scope=route.scope, candidate=candidate, now=now, instruction='再试') is None
        assert len(provider.requests) == 1
        fresh = Candidate(candidate_id='useful-material', kind='web_curiosity', created_at=now,
            reason='新材料', excerpt='有意义的文章', context_hash='another')
        draft = await bridge._proactive_generate(scope=route.scope, candidate=fresh, now=now, instruction='自主开口机会')
        assert draft['text'] == '这条材料确实有意思。' and len(provider.requests) == 2
        request = provider.requests[0]
        assert request.tools[0]['function']['name'] == 'group_no_reply'
        assert not any(m['role'] == 'user' for m in request.messages)
        assert '2026-10-26T11:35:00+08:00' in request.messages[0]['content']
    finally:
        await bridge.close()


def test_environment_provides_beijing_time_without_changing_utc_observation():
    message = environment_message(now=datetime(2026, 10, 26, 3, 35, tzinfo=timezone.utc))
    data = json.loads(message['content'].split('：', 1)[1])
    assert data['observed_at'] == '2026-10-26T03:35:00+00:00'
    assert data['local_time'] == '2026-10-26T11:35:00+08:00'
    assert data['timezone'] == 'Asia/Shanghai'
