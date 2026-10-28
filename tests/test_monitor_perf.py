import json

from aiohttp.test_utils import TestClient, TestServer
import pytest

from warmagent.monitor_perf import performance_reports


def test_saved_performance_numbers_and_no_private_environment(tmp_path):
    report = tmp_path / 'reports/perf/native-record-20261026-000000'
    report.mkdir(parents=True)
    report.joinpath('environment.json').write_text(json.dumps({'source': 'private-path'}))
    report.joinpath('summary.json').write_text(json.dumps([
        {'case': 'list_records', 'python_ms': 32, 'rust_serializer_ms': 8,
            'raw': {'python': [30, 32, 34], 'rust_serializer': [7, 8, 9]}},
        {'case': 'invalid', 'python_ms': True, 'rust_serializer_ms': 1}]))
    result = performance_reports(tmp_path)
    row = result['reports'][0]['rows'][0]
    assert row['ratio'] == .25 and row['rounds'] == 3
    assert len(result['reports'][0]['rows']) == 1
    assert 'private-path' not in json.dumps(result)
    assert result['measurement'] == 'saved_offline_benchmark'


def test_saved_performance_absent_or_incomplete(tmp_path):
    assert performance_reports(tmp_path)['reports'] == []
    report = tmp_path / 'reports/perf/native-text-20261026-000000'
    report.mkdir(parents=True)
    report.joinpath('summary.json').write_text('[')
    assert performance_reports(tmp_path)['reports'][0]['status'] == 'unavailable'


@pytest.mark.asyncio
async def test_performance_reaches_monitor_api_and_page(tmp_path):
    from warmagent.monitor import create_monitor_app

    report = tmp_path / 'reports/perf/native-text-20261026-000000'
    report.mkdir(parents=True)
    report.joinpath('environment.json').write_text('{"rounds":3}')
    report.joinpath('summary.json').write_text(json.dumps([
        {'case': 'select_summaries', 'fixture': '100x120', 'python_ms': 2, 'cpp_ms': .9}]))
    async with TestClient(TestServer(create_monitor_app(tmp_path))) as client:
        page = await client.get('/')
        assert '性能验证与原生优化' in await page.text()
        response = await client.get('/api/metrics')
        assert response.status == 200
        data = await response.json()
        assert data['performance']['reports'][0]['rows'][0]['rounds'] == 3
        assert data['performance']['reports'][0]['rows'][0]['ratio'] == .45
