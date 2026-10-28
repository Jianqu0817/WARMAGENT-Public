"""Local synthetic slices for job events, workspace edits and browser effect ledger."""
import json
import sqlite3

from aiohttp.test_utils import TestClient, TestServer
import pytest

from myatri.contracts import ModelResponse
from myatri.conversation.budget import TokenBudget
from warmagent.server import create_app
from warmagent.tool_context import ToolContext
from warmagent.workspace_view import WorkspaceView
from test_direct_harness import FakeExecutor, settings
from warmagent.direct_harness import DirectHarness
from test_warm_runtime import incoming, make_bridge


@pytest.mark.asyncio
async def test_job_completion_is_seen_by_original_persona_once_then_goes_to_outbox(tmp_path):
    response = ModelResponse(provider='fixture', model='fixture', content='后台核对完了，结果是八条。')
    bridge, provider = make_bridge(tmp_path, [response])
    route = incoming('private', sender='owner', message_id='job-report-route')
    bridge.register_route(route)
    event = {'event_id': 'job-fixture', 'job_id': 'job-fixture', 'scope': route.scope,
        'task': 'conversation', 'turn': 'original-turn', 'session_id': 'original-session',
        'status': 'completed', 'output_preview': '实际工具返回8条，原动作已完成。', 'output_ref': {}}
    try:
        bridge.direct.background_jobs.record(route.scope, 'conversation', event['job_id'],
            turn=event['turn'], session_id=event['session_id'])
        bridge.direct._handle_job_event(event)
        bridge.direct._handle_job_event(event)
        await bridge._process_background_events()
        await bridge._process_background_events()
        assert len(provider.requests) == 1
        assert any(event['output_preview'] in str(m['content']) for m in provider.requests[0].messages)
        app = bridge.apps[route.scope]
        raw = [r for r in app.store.list_records('raw_message') if r.payload.get('host_event')]
        assert len(raw) == 1 and raw[0].author_kind == 'system'
        assert all(r.ref != raw[0].ref for r in app.maintenance.pending()[0])
        with sqlite3.connect(bridge.path) as db:
            rows = db.execute("SELECT id,status,trigger_kind FROM deliveries WHERE id LIKE 'job_report_%'").fetchall()
        assert len(rows) == 1 and rows[0][1:] == ('prepared', 'computer_report')
        assert (await bridge.claim(rows[0][0]))['claimed']
        await bridge.acknowledge(rows[0][0], 'unknown')
        await bridge._process_background_events()
        assert len(provider.requests) == 1
        assert not (await bridge.claim(rows[0][0]))['claimed']
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_activity_await_parks_then_completion_restores_same_protocol(tmp_path):
    bridge, _ = make_bridge(tmp_path)
    route = incoming('private', sender='owner')
    bridge.register_route(route)
    task = 'activity-waiting-fixture'
    protocol = {'activity_id': task, 'messages': [], 'pending_calls': []}
    try:
        bridge.activities.checkpoint(goal='原目标', summary='原进度', protocol=protocol, status='active')
        bridge.direct.background_jobs.record(route.scope, task, 'job-task', turn=task, session_id='task-session')
        result = bridge._await_job(protocol, {'job_id': 'job-task', 'timeout_seconds': 60,
            'resume_note': '原目标的下一步'}, 'await-call')
        protocol['event_wait'] = result['event_wait']
        bridge.activities.checkpoint(protocol=protocol, status='waiting_external')
        assert bridge.task_slots.park_current(route.scope)
        assert bridge.activities.overview() is None
        event = {'event_id': 'job-task', 'job_id': 'job-task', 'scope': route.scope,
            'task': task, 'turn': task, 'session_id': 'task-session', 'status': 'completed',
            'output_preview': '原任务工具已完成'}
        bridge.direct._handle_job_event(event)
        await bridge._process_background_events()
        assert bridge.task_slots.activate_next(quiet_period=0, now=bridge.activities.clock())
        restored = bridge.activities.overview()
        assert restored['goal'] == '原目标'
        assert restored['protocol']['activity_id'] == task
        assert restored['protocol']['external_event']['job_id'] == 'job-task'
        assert bridge.direct.background_jobs.list_pending() == []
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_browser_adapter_uses_existing_effect_ledger_and_unknown_is_not_replayed(tmp_path):
    harness = DirectHarness(settings(), tmp_path, 'owner-1', [], 'live', executor=FakeExecutor())
    calls = []
    context = {'turn_id': 'browser-turn', 'scope': 'owner-scope'}

    async def perform():
        calls.append('clicked')
        return {'verified': True}

    first = await harness.execute_local('browser-click', {'selector': '#go'}, context, 'click-id', perform, authorized=True)
    again = await harness.execute_local('browser-click', {'selector': '#go'}, context, 'click-id', perform, authorized=True)
    assert first == again and calls == ['clicked']
    assert (await harness.execute_local('browser-click', {}, context, 'no-auth', perform, authorized=False))['error']

    async def uncertain():
        calls.append('uncertain')
        raise RuntimeError('readback lost')

    for _ in range(2):
        result = await harness.execute_local('browser-click', {}, context, 'unknown-id', uncertain, authorized=True)
        assert result['error'] == 'outcome_unknown'
    assert calls == ['clicked', 'uncertain']
    await harness.close()


def test_pinned_working_context_keeps_original_while_other_consumed_results_fold(tmp_path):
    view = WorkspaceView(tmp_path / 'views.sqlite3')
    context = ToolContext({}, scope='scope', turn='turn', view=view)
    result = {'host_receipt_id': 'call', 'result': {'text': '合成正文' * 400}}
    context.result('call', 'fixture', result)
    messages = [{'role': 'tool', 'tool_call_id': 'call', 'content': json.dumps(result, ensure_ascii=False)}]
    context.consume(messages)
    budget = TokenBudget(limit=16000)
    assert 'summary' in json.loads(context.project(messages, [], budget)[0]['content'])
    view.pin('scope', 'chat', 'turn:call', 0)
    assert 'result' in json.loads(context.project(messages, [], budget)[0]['content'])
    assert 'result' in json.loads(messages[0]['content'])
    view.fork('scope', 'chat', 'branch', 1)
    view.note('scope', 'branch', 'current_goal', '探索新分支', 0)
    view.select('scope', 'chat', 'branch')
    projected = context.project(messages, [], budget)
    assert '探索新分支' in projected[0]['content']
    assert view.describe('scope', 'chat')['current_goal'] == ''
    with pytest.raises(KeyError):
        view.select('another-scope', 'chat', 'branch')


@pytest.mark.asyncio
async def test_workspace_api_is_authenticated_cas_and_does_not_change_receipt(tmp_path):
    bridge, _ = make_bridge(tmp_path)
    bridge.workspace_view.register_context('scope', 'chat', 'turn:call', observed=True, summary='已读结果')
    token = 'workspace-token-' * 4
    client = TestClient(TestServer(create_app(bridge, token)))
    await client.start_server()
    try:
        assert (await client.get('/v1/admin/workspaces')).status == 401
        headers = {'Authorization': 'Bearer ' + token}
        response = await client.post('/v1/admin/workspaces', headers=headers, json={
            'scope': 'scope', 'workspace': 'chat', 'action': 'pin', 'ref': 'turn:call', 'revision': 0})
        assert response.status == 200
        view = (await response.json())['view']
        assert view['items'][0]['pinned'] == 1 and view['revision'] == 1
        response = await client.post('/v1/admin/workspaces', headers=headers, json={
            'scope': 'scope', 'workspace': 'chat', 'action': 'unpin', 'ref': 'turn:call', 'revision': 0})
        assert response.status == 409
        assert bridge.workspace_view.list('scope', 'chat')['items'][0]['summary'] == '已读结果'
    finally:
        await client.close()
        await bridge.close()


@pytest.mark.asyncio
async def test_failed_background_reports_have_bounded_automatic_attempts(tmp_path):
    from myatri.providers import ProviderError
    bridge, _ = make_bridge(tmp_path)
    route = incoming('private', sender='owner')
    bridge.register_route(route)
    event = {'event_id': 'job-retry', 'job_id': 'job-retry', 'scope': route.scope,
        'task': 'conversation', 'turn': 'original', 'session_id': 'session', 'status': 'completed'}
    bridge.direct.background_jobs.record(route.scope, 'conversation', 'job-retry', turn='original', session_id='session')
    bridge.direct._handle_job_event(event)
    app = bridge._application(route)
    calls = []
    async def fail(*args, **kwargs):
        calls.append('report')
        raise ProviderError('synthetic provider unavailable')
    app.conversation.respond = fail
    try:
        for _ in range(4):
            await bridge._process_background_events()
            with sqlite3.connect(bridge.direct.background_jobs.path) as db:
                db.execute('UPDATE background_events SET next_attempt=0')
        assert calls == ['report'] * 3
        assert bridge.direct.background_jobs.list_pending() == []
        assert bridge.direct.background_jobs.retry_report(route.scope, 'job-retry')['accepted']
        await bridge._process_background_events()
        assert len(calls) == 4
        assert bridge.direct.background_jobs.get(route.scope, 'job-retry')['status'] == 'completed'
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_manual_global_pause_survives_empty_current_slot(tmp_path):
    bridge, _ = make_bridge(tmp_path)
    try:
        assert bridge.activities.overview() is None
        bridge.control_activity('pause')
        assert bridge.task_slots.dispatch_paused()
        bridge.control_activity('resume')
        assert not bridge.task_slots.dispatch_paused()
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_monitor_workspace_route_requires_session_and_accepts_valid_control(tmp_path, monkeypatch):
    from warmagent import monitor
    calls = []
    async def proxy(project, payload=None, **options):
        calls.append((payload, options))
        return {'ok': True, 'view': {}}, 200
    monkeypatch.setattr(monitor, 'runtime_settings_proxy', proxy)
    client = TestClient(TestServer(monitor.create_monitor_app(tmp_path)))
    await client.start_server()
    try:
        assert (await client.post('/api/workspaces', json={'scope': 'x', 'workspace': 'chat'})).status == 403
        await client.get('/')
        origin = str(client.make_url('/')).rstrip('/')
        response = await client.post('/api/workspaces', json={'scope': 'x', 'workspace': 'chat', 'action': 'list'},
            headers={'Origin': origin, 'X-Monitor-Control': '1'})
        assert response.status == 200
        assert calls[-1][1] == {'workspace': True}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_local_cli_background_result_uses_terminal_and_is_not_displayed_twice(tmp_path):
    from warmagent.local_chat import display_background_reports
    bridge, _ = make_bridge(tmp_path, [ModelResponse(provider='fixture', model='fixture', content='本机后台已完成。')])
    route = incoming('private', sender='owner')
    bridge.register_route(route)
    bridge.active_local_scopes.add(route.scope)
    event = {'event_id': 'cli-job', 'job_id': 'cli-job', 'scope': route.scope,
        'task': 'conversation', 'turn': 'local-turn', 'session_id': 'local-session', 'status': 'completed'}
    try:
        bridge.direct.background_jobs.record(route.scope, 'conversation', 'cli-job', turn='local-turn', session_id='local-session')
        bridge.direct._handle_job_event(event)
        await bridge._process_background_events()
        lines = []
        await display_background_reports(bridge, route.scope, lines.append)
        await display_background_reports(bridge, route.scope, lines.append)
        assert lines == ['小温暖 > 本机后台已完成。']
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_device_queue_has_bounded_admission_without_losing_first_call(tmp_path):
    import asyncio
    harness = DirectHarness({**settings(), 'action_queue_limit': 1}, tmp_path, 'owner-1', [], 'live', executor=FakeExecutor())
    entered, release = asyncio.Event(), asyncio.Event()
    async def perform():
        entered.set()
        await release.wait()
        return {'done': True}
    context = {'turn_id': 'queue-turn', 'scope': 'owner-scope'}
    first = asyncio.create_task(harness.execute_local('local-fixture', {}, context, 'first', perform, authorized=True))
    await entered.wait()
    rejected = await harness.execute_local('local-fixture', {}, context, 'second', perform, authorized=True)
    assert rejected == {'error': 'device_queue_full', 'not_executed': True}
    release.set()
    assert (await first)['result']['done']
    assert harness._pending_actions == 0
    await harness.close()


def test_only_older_observed_images_fold_after_a_newer_image_was_seen():
    from warmagent.context_lifecycle import project_tool_messages
    messages = [{'role': 'tool', 'tool_call_id': name, 'content': json.dumps({
        'host_receipt_id': name, 'image_manifest': {'file': name + '.json', 'sha256': name, 'image_count': 1}})}
        for name in ('first-image', 'latest-image')]
    untouched, _ = project_tool_messages(messages, {'first-image'})
    assert all('image_manifest' in json.loads(m['content']) for m in untouched)
    projected, stats = project_tool_messages(messages, {'first-image', 'latest-image'})
    assert 'image_manifest_ref' in json.loads(projected[0]['content'])
    assert 'image_manifest' not in json.loads(projected[0]['content'])
    assert 'image_manifest' in json.loads(projected[1]['content'])
    assert stats['folded_results'] == 1
    assert 'image_manifest' in json.loads(messages[0]['content'])
