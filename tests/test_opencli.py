"""Offline OpenCLI contracts: owner gate, task sessions, receipts and model continuation."""
import asyncio
from datetime import datetime, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest

from myatri.contracts import ModelResponse
from warmagent.opencli import OpenCLI, handlers
from test_direct_harness import FakeExecutor
from test_warm_runtime import incoming, make_bridge


def tool_call(call_id, name, args):
    return {'id': call_id, 'type': 'function', 'function': {'name': name,
            'arguments': json.dumps(args, ensure_ascii=False)}}


@pytest.mark.asyncio
async def test_cli_arguments_are_not_shell_commands_and_task_sessions_are_host_bound(tmp_path, monkeypatch):
    node = tmp_path / 'node.exe'
    node.touch()
    entry = tmp_path / 'cli.js'
    entry.touch()
    client = OpenCLI({'enabled': True, 'entrypoint': 'cli.js', 'profile': 'fixture-profile'}, tmp_path, str(node))
    calls, browser_opens = [], []
    async def browser():
        browser_opens.append(True)
    client.open_browser = browser
    class Process:
        returncode = 0
        async def communicate(self):
            return b'{"ok":true,"data":{"title":"fixture"}}', b''
    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setenv('OPENCLI_CDP_ENDPOINT', 'do-not-inherit')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'do-not-inherit')
    first = await client.run(['browser', 'open', 'https://example.com/?x=1&y=$(ignored)'], {'scope': 'a'})
    again = await client.run(['browser', 'state'], {'scope': 'a', 'turn_id': 'next'})
    other = await client.run(['browser', 'state'], {'scope': 'b'})
    activity = await client.run(['browser', 'state'], {'scope': 'a', 'activity_id': 'activity'})
    assert first['browser_session'] == again['browser_session']
    assert len({first['browser_session'], other['browser_session'], activity['browser_session']}) == 3
    command, kwargs = calls[0]
    assert 'https://example.com/?x=1&y=$(ignored)' in command
    assert command[:4] == (str(node), str(entry), '--profile', 'fixture-profile')
    assert command[-2:] == ('--session', first['browser_session'])
    assert 'shell' not in kwargs and 'DEEPSEEK_API_KEY' not in kwargs['env']
    assert 'OPENCLI_CDP_ENDPOINT' not in kwargs['env']
    await client.run(['list', 'search', '--site', 'zhihu'], {'scope': 'a'})
    await client.run(['browser', 'click', '--help', '-f', 'json'], {'scope': 'a'})
    assert len(browser_opens) == 4
    before = len(calls)
    assert (await client.run(['browser', 'state', '--session=other'], {}))['not_executed']
    assert len(calls) == before
    disabled = OpenCLI({}, tmp_path)
    assert (await disabled.run(['list'], {}))['error'] == 'opencli_disabled'


@pytest.mark.asyncio
async def test_timeout_is_recorded_unknown_and_same_call_does_not_execute_twice(tmp_path):
    bridge, _ = make_bridge(tmp_path, mode='live', direct=True)
    bridge.direct.settings.update(enabled=True, owner_id='owner', permission_profile='full')
    bridge.opencli.settings['enabled'] = True
    calls = []
    async def unknown(args, context):
        calls.append(args)
        raise asyncio.TimeoutError()
    bridge.opencli.run = unknown
    try:
        entries = bridge._browser_handlers({'scope': 'scope', 'turn_id': 'turn'}, authorized=True)
        run = entries['harness__browser_opencli'][1].execute_call
        first = await run('once', {'args': ['browser', 'click', '--text', 'fixture']})
        second = await run('once', {'args': ['browser', 'click', '--text', 'fixture']})
        assert first == second and first['error'] == 'outcome_unknown' and len(calls) == 1
        assert bridge._browser_handlers({'turn_id': 'denied'}, authorized=False) == {}
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_owner_model_discovers_opencli_and_receives_result_in_same_conversation(tmp_path):
    calls = [tool_call('discover', 'harness_discover_tools', {'names': ['harness__browser_opencli']}),
             tool_call('cli', 'harness__browser_opencli', {'args': ['list', 'search', '--site', 'zhihu']})]
    bridge, provider = make_bridge(tmp_path, [*[ModelResponse(provider='fixture', model='fixture',
        content='', tool_calls=[call]) for call in calls],
        ModelResponse(provider='fixture', model='fixture', content='知乎的搜索入口找到了。')], mode='live', direct=True)
    bridge.direct.executor = FakeExecutor()
    bridge.direct.settings.update(enabled=True, owner_id='owner', permission_profile='full', autonomy_granted=True)
    bridge.opencli.settings['enabled'] = True
    executed = []
    async def run(args, context):
        executed.append((args, context))
        return {'exit_code': 0, 'output': {'ok': True, 'data': {'commands': ['zhihu search']}}}
    bridge.opencli.run = run
    message = incoming('private', sender='owner', text='去知乎找找', message_id='cli-request')
    try:
        result = await bridge.receive(message)
        assert '找到了' in result['reply'] and len(provider.requests) == 3
        assert len(executed) == 1 and executed[0][1]['scope'] == message.scope
        assert 'zhihu search' in json.dumps(provider.requests[-1].messages, ensure_ascii=False)
        instructions = '\n'.join(m.get('content', '') for m in provider.requests[0].messages if m['role'] == 'system')
        assert '在本轮发现并调用合适工具' in instructions
        assert 'Humanizer' not in instructions
        raw = bridge.apps[message.scope].store.get_record(message.key)
        outsider = raw.model_copy(update={'payload': {'channel_source': {
            **raw.payload['channel_source'], 'sender_id': 'outsider', 'sender_name': '用户'}}})
        assert 'harness__browser_opencli' not in await bridge._chat_handlers(outsider)
        assert 'harness__browser_opencli' in await bridge._activity_handlers()
        with sqlite3.connect(bridge.direct.db_path) as db:
            assert db.execute("SELECT state FROM calls WHERE call_id='cli'").fetchone() == ('completed',)
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_autonomous_materials_prefer_requested_sites_without_extra_search_calls(tmp_path):
    bridge, _ = make_bridge(tmp_path, mode='live')
    message = incoming('private', sender='owner')
    bridge._application(message, start_workers=False)
    bridge.opencli.settings.update(enabled=True, preferred_domains=['zhihu.com', 'bilibili.com'])
    bridge.web_enabled = True
    bridge.proactive_config = {'web_scan_minutes': 0, 'bili_scan_minutes': 0}
    query = '关心的话题' * 30
    bridge._web_topic_query = lambda scope: query
    queries = []
    def search(**kwargs):
        queries.append(kwargs['query'])
        return {'results': [{'title': '保留站点', 'url': 'https://www.zhihu.com/question/1', 'snippet': '摘要'},
                            {'title': '奇怪转载', 'url': 'https://unrelated.example/article', 'snippet': '忽略'}]}
    bridge._web_tool = lambda root: (None, SimpleNamespace(search=search))
    try:
        bridge._scan_web_material(message.scope)
        assert queries[0].endswith(' site:zhihu.com') and len(queries[0]) <= 111
        found = await bridge._material_candidates(message.scope, datetime.now(timezone.utc), set())
        assert len(found) == 1
        assert '保留站点' in found[0].excerpt and '奇怪转载' not in found[0].excerpt
        bridge._scan_web_material(message.scope)
        assert queries[1].endswith(' site:bilibili.com') and len(queries) == 2
    finally:
        await bridge.close()


def test_handler_validates_without_running_cli_when_arguments_are_wrong(tmp_path):
    client = OpenCLI({'enabled': True}, tmp_path)
    async def execute(*args):
        raise AssertionError('invalid arguments must not execute')
    entry = handlers(client, execute, {})['harness__browser_opencli'][1]
    result = asyncio.run(entry.execute_call('bad', {'args': 'browser state'}))
    assert result['error'] == 'invalid_tool_arguments'
