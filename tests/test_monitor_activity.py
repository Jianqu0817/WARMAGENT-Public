"""Offline contracts for the monitor activity proxy and controls."""
from aiohttp.test_utils import TestClient, TestServer
import pytest

from warmagent import control_proxy, monitor


@pytest.mark.asyncio
async def test_activity_proxy_uses_authenticated_admin_route_and_projects_fields(tmp_path, monkeypatch):
    (tmp_path / 'config').mkdir()
    (tmp_path / 'var').mkdir()
    (tmp_path / 'config/bridge.local.toml').write_text(
        '[bridge]\nhost="127.0.0.1"\nport=8766\ntoken_file="var/token.txt"\n', encoding='utf-8')
    (tmp_path / 'var/token.txt').write_text('t' * 40, encoding='ascii')
    calls = []

    class Response:
        status_code = 200
        is_success = True
        def json(self):
            return {'ok': True,
                'activity': {'goal': '<goal>', 'status': 'running', 'summary': 'doing work',
                    'next_step': 'continue', 'private_field': 'never shown'},
                'direct_harness': {'enabled': True, 'mode': 'offline', 'profile': 'full',
                    'tools': ['computer_control'], 'error': None, 'token': 'never shown'},
                'scheduler': {'rest_until': 123, 'last_poll_at': 456, 'private_field': 'never shown'},
                'other': 'never shown'}

    class Client:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return Response()

    monkeypatch.setattr(control_proxy.httpx, 'AsyncClient', lambda **_: Client())
    data, status = await control_proxy.runtime_settings_proxy(tmp_path, {'action': 'pause'}, activity=True)
    assert status == 200
    assert calls[0][0:2] == ('POST', 'http://127.0.0.1:8766/v1/admin/activity')
    assert calls[0][2]['headers']['Authorization'] == 'Bearer ' + 't' * 40
    assert calls[0][2]['json'] == {'action': 'pause'}
    assert data == {'ok': True, 'scheduler': {'rest_until': 123, 'last_poll_at': 456},
        'activity': {'goal': '<goal>', 'status': 'running', 'summary': 'doing work', 'next_step': 'continue'},
        'direct_harness': {'enabled': True, 'mode': 'offline', 'profile': 'full',
            'tools': ['computer_control'], 'error': None}}


@pytest.mark.asyncio
async def test_activity_monitor_requires_session_and_validates_controls(tmp_path, monkeypatch):
    calls = []
    async def fake_proxy(project, payload=None, **kwargs):
        calls.append((payload, kwargs))
        return {'ok': True, 'activity': None,
            'direct_harness': {'enabled': False, 'mode': 'offline', 'tools': [], 'profile': None, 'error': None}}, 200
    monkeypatch.setattr(monitor, 'runtime_settings_proxy', fake_proxy)
    client = TestClient(TestServer(monitor.create_monitor_app(tmp_path)))
    await client.start_server()
    try:
        denied = await client.get('/api/activity')
        assert denied.status == 403
        page = await client.get('/')
        cookie = page.cookies['monitor_control_session'].value
        html = await page.text()
        assert '自主活动' in html and 'Direct Harness' in html and '未接入' in html
        assert 'data-activity-action="pause"' in html
        headers = {'Cookie': f'monitor_control_session={cookie}'}
        read = await client.get('/api/activity', headers=headers)
        assert read.status == 200
        assert calls[-1] == (None, {'activity': True})
        origin = str(client.make_url('/')).rstrip('/')
        invalid = await client.post('/api/activity', headers={**headers, 'Origin': origin,
            'X-Monitor-Control': '1', 'Content-Type': 'application/json'}, json={'action': 'delete'})
        assert invalid.status == 400
        posted = await client.post('/api/activity', headers={**headers, 'Origin': origin,
            'X-Monitor-Control': '1'}, json={'action': 'resume'})
        assert posted.status == 200
        assert calls[-1] == ({'action': 'resume'}, {'activity': True})
    finally:
        await client.close()
