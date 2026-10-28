import json
import pytest

from myatri.contracts import ModelResponse
from warmagent.harness_guide import GUIDE_TOOL, LocalGuide
from warmagent.tool_catalog import ToolCatalog
from warmagent.tool_policy import operation_seen
from test_activity_runner import tool_call, FakeApp, build, finish_response
from test_warm_runtime import incoming, make_bridge
from test_reading_conversation_direct import FakeGateway, FakeHandler, conversation, tool_response


def tool(name, description='fixture', handler=None):
    return ({'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}}, handler or (lambda: None))


def catalog(root):
    names = ['harness__read', 'harness__edit', 'harness__pwsh', 'harness__android_observe',
        'harness__android_text', 'harness__browser_state', 'harness__bili_humanism_read',
        'harness__document_read', 'harness__job_read', 'harness__windows_screenshot']
    handlers = {name: tool(name) for name in names}
    handlers['harness_guide'] = (GUIDE_TOOL, LocalGuide(root, mode='live'))
    handlers['recall'] = tool('recall')
    return ToolCatalog(handlers)


@pytest.mark.parametrize(('query', 'expected'), [
    ('找一下我的素材笔记', 'harness__read'), ('操作MuMu手机', 'harness__android_observe'),
    ('看看B站视频字幕', 'harness__bili_humanism_read'), ('读取PDF文档', 'harness__document_read'),
    ('截个屏幕截图', 'harness__windows_screenshot'), ('查询电脑内存', 'harness__pwsh')])
def test_chinese_tasks_find_only_registered_tools(tmp_path, query, expected):
    c = catalog(tmp_path)
    before = {t['function']['name'] for t in c.schemas()}
    assert expected not in before and 'harness_guide' in before
    result = c.discover(query=query)
    assert expected in {t['function']['name'] for t in result['tools']}
    assert set(c.expanded) <= set(c.native)
    assert expected in {t['function']['name'] for t in c.schemas()}
    assert len(c.handlers['harness_discover_tools'][0]['function']['description']) < 400


def test_guide_reads_current_resource_metadata_without_actions_or_credentials(tmp_path):
    notes = tmp_path / 'notes/素材小本.md'
    notes.parent.mkdir()
    notes.write_text('本地笔记正文不自动注入', encoding='utf-8')
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/secret.local.json').write_text('DO-NOT-READ-KEY')
    c = catalog(tmp_path)
    overview = c.guide()
    assert overview['resources'][0]['exists']
    assert overview['resources'][0]['bytes'] == notes.stat().st_size
    encoded = json.dumps(overview, ensure_ascii=False)
    assert '本地笔记正文' not in encoded and 'DO-NOT-READ-KEY' not in encoded
    guide = c.guide(query='查找素材')
    assert guide['topic'] == 'files' and guide['discover'] == ['harness__edit', 'harness__read']
    assert not c.expanded and c.state()['guide_queries'] == 2
    memory = c.guide(topic='memory')
    assert memory['ready_tools'] == ['recall'] and not memory['discover']
    for topic in ('overview', 'files', 'command', 'desktop', 'android', 'browser', 'video', 'document', 'memory', 'tasks'):
        assert len(json.dumps(c.guide(topic=topic), ensure_ascii=False).encode()) < 6000
    assert 'registered_not_probed' in encoded
    assert not operation_seen([{'role': 'assistant', 'tool_calls': [tool_call('g', 'harness_guide', {})]},
        {'role': 'tool', 'tool_call_id': 'g', 'content': encoded}], 'host')


@pytest.mark.asyncio
async def test_guide_available_to_owner_without_node_but_not_nickname_spoofer(tmp_path):
    bridge, provider = make_bridge(tmp_path, [ModelResponse(provider='fixture', model='fixture', content='在这里。')], direct=True)
    async def unavailable(*args, **kwargs):
        raise RuntimeError('fixture_node_unavailable')
    bridge.direct.handlers = unavailable
    try:
        message = incoming('private', sender='owner', text='你好')
        await bridge.receive(message)
        assert len(provider.requests) == 1
        app = bridge.apps[message.scope]
        raw = app.store.get_record(message.key)
        owner = await bridge._chat_handlers(raw)
        assert 'harness_guide' in owner
        other = raw.model_copy(update={'payload': {'channel_source': {
            **raw.payload['channel_source'], 'sender_id': 'other', 'sender_name': '用户'}}})
        assert 'harness_guide' not in await bridge._chat_handlers(other)
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_chat_guide_discovery_file_read_and_result(tmp_path):
    note = tmp_path / 'notes/素材小本.md'
    note.parent.mkdir()
    note.write_text('已有知识：宋画留白', encoding='utf-8')
    calls = [tool_call('g', 'harness_guide', {'query': '找素材笔记'}),
        tool_call('d', 'harness_discover_tools', {'category': 'files'}),
        tool_call('r', 'harness__read', {})]
    bridge, provider = make_bridge(tmp_path, [*[ModelResponse(provider='fixture', model='fixture',
        content='', tool_calls=[call]) for call in calls],
        ModelResponse(provider='fixture', model='fixture', content='素材小本里有宋画留白的笔记。')], direct=True)
    reads = []
    async def fake_handlers(raw, *, context):
        entries = {f'harness__fixture_{i}': tool(f'harness__fixture_{i}') for i in range(9)}
        async def read(**kwargs):
            raise AssertionError('must use the existing execution wrapper')
        async def execute(call_id, args):
            reads.append(call_id)
            return {'path': str(note), 'text': note.read_text(encoding='utf-8')}
        read.execute_call = execute
        entries['harness__read'] = tool('harness__read', '读取本地文件', read)
        return entries
    bridge.direct.handlers = fake_handlers
    try:
        message = incoming('private', sender='owner', text='查看我的素材笔记')
        result = await bridge.receive(message)
        assert result['status'] == 'prepared' and reads == ['r']
        first = provider.requests[0]
        assert 'harness__read' not in {t['function']['name'] for t in first.tools}
        assert 'harness__read' in {t['function']['name'] for t in provider.requests[2].tools}
        environment_message = next(m for m in first.messages if m.get('content', '').startswith('宿主环境快照'))
        environment = json.loads(environment_message['content'].split('：', 1)[1])
        assert environment['local_resources']['notes_directory'] == str(note.parent)
        final = json.dumps(provider.requests[-1].messages, ensure_ascii=False)
        assert '已有知识：宋画留白' in final
        stats = bridge.apps[message.scope].queue.get_protocol(message.key)['context_stats']['tool_catalog']
        assert stats['guide_queries'] == 1 and stats['discovery_calls'] == 1
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_activity_model_receives_navigation_and_can_query_same_guide(tmp_path):
    app = FakeApp([{'tool_calls': [tool_call('g', 'harness_guide', {'topic': 'files'})]},
        finish_response('done', 'continue', summary='已定位本地笔记')])
    guide = LocalGuide(tmp_path, mode='live')
    c, _, _ = build(tmp_path, app, {'harness_guide': (GUIDE_TOOL, guide)})
    c.checkpoint(status='active', goal='查找已有知识', protocol={'activity_id': 'guide-test'})
    await c.tick(601)
    await c.tick(602)
    await c.tick(603)
    assert len(app.provider.requests) == 2
    messages = app.provider.requests[-1].messages
    environment = next(m for m in messages if m.get('content', '').startswith('宿主环境快照'))
    navigation = json.loads(environment['content'].split('：', 1)[1])['local_resources']
    assert navigation['notes_directory'] == str(tmp_path / 'notes')
    assert '当前知识笔记' in json.dumps(messages, ensure_ascii=False)


@pytest.mark.asyncio
async def test_existing_single_correction_points_to_guide_then_actual_operation(tmp_path):
    gateway = FakeGateway(ModelResponse(provider='fixture', model='fixture', content='我无法写本地文件。'),
        ModelResponse(provider='fixture', model='fixture', content='',
            tool_calls=[tool_call('g', 'harness_guide', {'topic': 'files'})]),
        tool_response(), ModelResponse(provider='fixture', model='fixture', content=json.dumps({
            'state': 'completed', 'report': '已保存。', 'next_step': ''}, ensure_ascii=False)))
    handler = FakeHandler()
    turn = conversation(tmp_path, gateway, handler)
    turn.tool_handlers['harness_guide'] = (GUIDE_TOOL, LocalGuide(tmp_path, mode='live'))
    await turn.respond('写日记', message_id='guided-write', metadata={'channel_source': {'text': '写日记'}})
    assert len(gateway.requests) == 4 and len(handler.calls) == 1
    corrections = [m for m in gateway.requests[-1].messages if m.get('role') == 'system'
        and '本轮尚无对应操作工具调用' in m.get('content', '')]
    assert len(corrections) == 1 and 'harness_guide' in corrections[0]['content']
    protocol = turn.protocol_store.get_protocol('guided-write')
    assert protocol['owner_action']['tool_attempted']


def test_guide_reports_installed_metadata_not_a_copied_version(tmp_path):
    package = tmp_path / 'node_modules/@deepseek-ai/dsh/package.json'
    package.parent.mkdir(parents=True)
    package.write_text('{"version":"fixture-1"}')
    guide = LocalGuide(tmp_path, mode='live', runtime_root=tmp_path)
    ToolCatalog({'harness_guide': (GUIDE_TOOL, guide)})
    assert guide()['versions']['@deepseek-ai/dsh'] == 'fixture-1'
    package.write_text('{"version":"fixture-2"}')
    assert guide()['versions']['@deepseek-ai/dsh'] == 'fixture-2'
