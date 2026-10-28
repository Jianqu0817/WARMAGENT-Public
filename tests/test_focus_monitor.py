"""Authenticated activity controls against a temporary runtime, no live channel."""

from aiohttp.test_utils import TestClient, TestServer
import pytest

from warmagent.server import create_app
from test_warm_runtime import make_bridge


@pytest.mark.asyncio
async def test_focus_and_wakeup_controls_roundtrip_through_authenticated_api(tmp_path):
    bridge, provider = make_bridge(tmp_path)
    bridge.activities.checkpoint(goal='合成目标', protocol={'activity_id': 'monitor-task'}, status='active')
    token = 'synthetic-monitor-token-' * 3
    client = TestClient(TestServer(create_app(bridge, token)))
    await client.start_server()
    headers = {'Authorization': 'Bearer ' + token}
    try:
        assert (await client.get('/v1/admin/activity')).status == 401
        response = await client.post('/v1/admin/activity', headers=headers, json={
            'action': 'set_focus', 'policy': 'adaptive', 'duration_seconds': 120})
        assert response.status == 200
        state = await response.json()
        assert state['activity']['focus_policy'] == 'adaptive'
        assert state['activity']['control_revision'] == 1
        wake = bridge.activities.schedule_wakeup(delay_seconds=60, resume_note='继续合成目标', call_id='wake-ui')
        response = await client.get('/v1/admin/activity', headers=headers)
        assert (await response.json())['activity']['next_wakeup']['wake_id'] == wake['wake_id']
        response = await client.post('/v1/admin/activity', headers=headers,
            json={'action': 'cancel_wakeup', 'wake_id': wake['wake_id']})
        assert response.status == 200
        assert (await response.json())['activity']['next_wakeup'] is None
        assert provider.requests == []
    finally:
        await client.close()
        await bridge.close()
