"""Optional group speaking: no live model, transport or production data."""
import sqlite3
from types import SimpleNamespace

import pytest

from myatri.contracts import ModelResponse, digest
from myatri.providers.gateway import ProviderError
from warmagent.attention import name_call
from warmagent.bubbles import prepare_bubbles
from warmagent.chat_silence import NO_REPLY_NAME, NoReply
from warmagent.server import pending_deliveries
from warmagent.wechat import WeChatRunner
from test_reading_conversation_direct import FakeGateway, FakeHandler, conversation, tool_response
from test_warm_runtime import make_bridge, incoming


def silent():
    return ModelResponse(provider='fixture', model='fixture', content='', tool_calls=[{
        'id': 'silence-1', 'type': 'function', 'function': {
            'name': NO_REPLY_NAME, 'arguments': '{"reason":"not_addressed"}'}}])


def optional_source(**values):
    return {'channel_source': {'text': '刚才那个话题', 'trigger_kind': 'attention', **values}}


@pytest.mark.asyncio
async def test_no_reply_is_terminal_idempotent_and_never_an_assistant_message(tmp_path):
    gateway = FakeGateway(silent())
    turn = conversation(tmp_path, gateway, FakeHandler())
    answers = []
    turn.on_answer = answers.append
    result = await turn.respond('旁听资料', message_id='quiet', metadata=optional_source())
    assert result == NoReply('not_addressed')
    assert len(gateway.requests) == 1 and not answers
    assert any(t['function']['name'] == NO_REPLY_NAME for t in gateway.requests[0].tools)
    assert all(r.payload['role'] == 'user' for r in turn.store.records.values())
    assert turn.protocol_store.get_protocol('quiet')['status'] == 'completed'
    assert turn.protocol_store.get_protocol('quiet')['decision'] == 'no_reply'
    again = await turn.respond('旁听资料', message_id='quiet', metadata=optional_source())
    assert again == result and len(gateway.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('source', [
    {'trigger_kind': 'explicit'}, {'conversation_kind': 'private'},
    {'transport': 'local_cli'}, {'trigger_kind': 'batch_report'},
    {'trigger_kind': 'computer_report'}, {'trigger_kind': 'autonomous_activity'},
])
async def test_required_replies_do_not_offer_silence(tmp_path, source):
    gateway = FakeGateway(ModelResponse(provider='fixture', model='fixture', content='收到。'))
    turn = conversation(tmp_path, gateway, FakeHandler())
    await turn.respond('需要回复', message_id='required', metadata=optional_source(**source))
    assert all(t['function']['name'] != NO_REPLY_NAME for t in gateway.requests[0].tools)


@pytest.mark.asyncio
async def test_tool_results_cannot_be_hidden_by_silence(tmp_path):
    handler = FakeHandler()
    gateway = FakeGateway(tool_response(), silent())
    turn = conversation(tmp_path, gateway, handler)
    with pytest.raises(ProviderError, match='standalone decision before tools'):
        await turn.respond('旁听资料', message_id='must-report', metadata=optional_source())
    assert handler.calls
    assert all(t['function']['name'] != NO_REPLY_NAME for t in gateway.requests[1].tools)
    assert turn.protocol_store.get_protocol('must-report')['status'] == 'active'


@pytest.mark.asyncio
async def test_previous_turn_tools_do_not_remove_current_turn_silence(tmp_path):
    gateway = FakeGateway(silent())
    turn = conversation(tmp_path, gateway, FakeHandler())
    async def context(text, **_):
        call = tool_response('old-tool').tool_calls[0]
        return [{'role': 'user', 'content': '旧请求'},
            {'role': 'assistant', 'content': '', 'tool_calls': [call]},
            {'role': 'tool', 'tool_call_id': call['id'], 'content': '{"ok":true}'},
            {'role': 'assistant', 'content': '旧任务完成。'}, {'role': 'user', 'content': text}], 'snapshot'
    turn.context.abuild_context = context
    assert await turn.respond('旁听', message_id='new-turn', metadata=optional_source()) == NoReply('not_addressed')
    assert len(gateway.requests) == 1


@pytest.mark.asyncio
async def test_bridge_and_wechat_skip_without_send_retry_or_false_history(tmp_path):
    bridge, provider = make_bridge(tmp_path, [silent(), {'content': '我在。'}])
    bridge.random_probability = 1
    message = incoming(text='他们在商量碰面的地方', message_id='optional').model_copy(update={'mentioned': False})
    client = SimpleNamespace(group_id='room', send=lambda _: pytest.fail('silence was sent'))
    runner = WeChatRunner(bridge, client)
    runner.message = lambda _: message
    try:
        await runner.handle({})
        app = bridge.apps[message.scope]
        assert app.queue.get_protocol(message.key)['decision'] == 'no_reply'
        assert len(provider.requests) == 1
        assert not pending_deliveries(bridge.path, message.scope)
        assert not (await bridge.claim(message.key))['claimed']
        assert not bridge.short_memory.recent_assistant(message.scope)
        assert not any(r.payload.get('role') == 'assistant' for r in app.store.list_records('raw_message'))
        assert bridge.input_inbox.summary()['needs_attention'] == 0
        assert (await bridge.receive(message))['status'] == 'skipped'
        assert len(provider.requests) == 1
        # Recover the protocol -> outbox gap as after a crash, with no model replay.
        with sqlite3.connect(bridge.path) as db:
            db.execute("UPDATE deliveries SET status='received' WHERE id=?", (message.key,))
        assert (await bridge._receive_now(message))['status'] == 'skipped'
        assert len(provider.requests) == 1
        assert (await bridge.receive(incoming(text='@小温暖 你在吗', message_id='next')))['status'] == 'prepared'
        assert len(provider.requests) == 2
        assert all('group_no_reply' not in m.get('content', '') for m in provider.requests[1].messages
                   if m['role'] == 'assistant')
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_recent_problem_messages_are_filtered_before_model(tmp_path):
    bridge, provider = make_bridge(tmp_path)
    bridge.random_probability = 1
    bridge.config['attention_followup_enabled'] = True
    samples = ['@其他成员\u2005你人呢', '[强]', '😪😪', '笑死', '@用户\u2005小温暖是不是故意针对我啊[流泪]']
    try:
        for i, text in enumerate(samples):
            msg = incoming(text=text, message_id=f'replay-{i}').model_copy(update={'mentioned': False})
            assert (await bridge.receive(msg))['status'] == 'ignored'
        assert not provider.requests
        assert not name_call('每天晚上都要和小温暖调情')
        assert name_call('小温暖，你在吗')
        assert name_call('你觉得呢，小溫暖')
        with sqlite3.connect(bridge.path) as db:
            assert db.execute('SELECT COUNT(*) FROM attention_windows').fetchone()[0] == 0
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_attention_is_speaker_bound_decays_and_obeys_cooldown(tmp_path):
    bridge, provider = make_bridge(tmp_path, [{'content': '答。'}, {'content': '继续。'}])
    bridge.config.update(attention_followup_enabled=True, cooldown_seconds=60)
    bridge.attention_probability = .35
    owner = incoming(text='小温暖你在吗', message_id='wake')
    try:
        await bridge.receive(owner)
        with sqlite3.connect(bridge.path) as db:
            db.execute('UPDATE attention_windows SET remaining=4')
        # Pick a stable draw < .0875, so the probability gate passes even in the last round.
        def message(sender):
            for i in range(1000):
                candidate = incoming(sender=sender, text='刚才说的那个呢', message_id=f'{sender}-{i}').model_copy(update={'mentioned': False})
                if int(digest(['random-reply-v1', candidate.key])[:13],16)/16**13 < .0875:
                    return candidate
            raise AssertionError('fixture draw not found')
        other = await bridge.receive(message('other'))
        assert other['reason'] == 'not_a_trigger'
        own = await bridge.receive(message('member'))
        assert own['reason'] == 'cooldown'
        assert len(provider.requests) == 1
        # Direct addresses bypass cooldown and rebind the focus to the new speaker.
        await bridge.receive(incoming(sender='other', text='小温暖，你好', message_id='new-wake'))
        with sqlite3.connect(bridge.path) as db:
            assert db.execute('SELECT sender_id FROM attention_windows').fetchone()[0] == 'other'
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_existing_global_window_migrates_without_inventing_a_speaker(tmp_path):
    from warmagent.attention import followup
    (tmp_path / 'groups').mkdir()
    with sqlite3.connect(tmp_path / 'groups/deliveries.sqlite3') as db:
        db.execute('CREATE TABLE attention_windows(scope TEXT PRIMARY KEY,remaining INTEGER,expires REAL,wake_key TEXT)')
        db.execute("INSERT INTO attention_windows VALUES('old',4,1000,'legacy')")
    bridge, _ = make_bridge(tmp_path)
    try:
        with sqlite3.connect(bridge.path) as db:
            assert not followup(db, 'old', 'next', wake=False, now=1, sender_id='someone')['active']
            assert db.execute('SELECT remaining,sender_id FROM attention_windows').fetchone() == (4, '')
    finally:
        await bridge.close()


def test_one_bubble_default_preserves_explicit_semantic_split():
    policy = SimpleNamespace(prepare=lambda text: text)
    text = '这是一段完整的话。虽然有两句话，但没有主动分段，就保持一个气泡发送。'
    assert prepare_bubbles(text, policy)[1] == [text]
    assert prepare_bubbles('第一件事<|msg|>独立的第二件事', policy)[1] == ['第一件事', '独立的第二件事']
