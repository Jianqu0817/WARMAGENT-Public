"""Offline checks for the user's four URLs and Bili-only plugin route."""
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from warmagent.harness_guide import RECIPES, LOCAL_RESOURCE_RULES
from warmagent.opencli import OpenCLI
from warmagent.site_routes import SITE_ENTRYPOINTS, targets_bilibili
from test_warm_runtime import incoming, make_bridge


def test_requested_urls_and_plugin_instruction_are_available_in_both_guides():
    assert SITE_ENTRYPOINTS == {
        '小红书': 'https://www.xiaohongshu.com/explore',
        '小黑盒': 'https://xiaoheihe.cn/app/bbs/home',
        '知乎': 'https://www.zhihu.com/', '贴吧': 'https://tieba.baidu.com/'}
    for url in SITE_ENTRYPOINTS.values():
        assert url in '\n'.join(RECIPES['browser'])
    assert 'B站只用既有dsh-bili-humanism插件' in LOCAL_RESOURCE_RULES
    assert 'bili_humanism' in '\n'.join(RECIPES['video'])
    assert targets_bilibili('https://www.bilibili.com/video/BV1')
    assert targets_bilibili('site:bilibili.com 视频')
    assert not targets_bilibili('https://bilibili.com.example.org/')


@pytest.mark.asyncio
@pytest.mark.parametrize('args', [
    ['bilibili', 'hot'], ['browser', 'open', 'https://www.bilibili.com/'],
    ['web', 'read', '--url', 'https://b23.tv/fixture']])
async def test_opencli_bili_access_is_redirected_before_browser_or_process_start(tmp_path, args):
    client = OpenCLI({'enabled': True, 'bilibili_plugin_only': True}, tmp_path)
    async def forbidden():
        raise AssertionError('must not start a browser')
    client.open_browser = forbidden
    result = await client.run(args, {})
    assert result['error'] == 'bilibili_use_existing_plugin' and result['not_executed']


@pytest.mark.asyncio
async def test_old_browser_and_web_search_redirect_to_plugin_without_network(tmp_path):
    bridge, _ = make_bridge(tmp_path)
    bridge.opencli.settings.update(enabled=True, bilibili_plugin_only=True)
    bridge.direct.settings.update(enabled=True)
    bridge.direct.mode = 'live'
    bridge._web_tool = lambda root: ({'type': 'function', 'function': {'name': 'web_search'}}, None)
    try:
        entries = bridge._browser_handlers({'scope': 'scope', 'turn_id': 'turn'}, authorized=True)
        result = await entries['harness__browser_open'][1].execute_call('bili', {'url': 'https://www.bilibili.com/'})
        assert result['error'] == 'bilibili_use_existing_plugin'
        app = bridge._application(incoming(), start_workers=False)
        result = await app.conversation.tool_handlers['web_search'][1](query='site:bilibili.com 某视频')
        assert result['error'] == 'bilibili_use_existing_plugin'
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_bili_scan_uses_plugin_authority_receipt_and_retains_source(tmp_path):
    bridge, _ = make_bridge(tmp_path, mode='live', direct=True)
    bridge.opencli.settings.update(enabled=True, bilibili_plugin_only=True)
    bridge.direct.settings.update(enabled=True, owner_id='owner', permission_profile='full', autonomy_granted=True)
    bridge.proactive_config = {'web_scan_minutes': 0, 'bili_scan_minutes': 0}
    calls = []
    async def execute(name, args, context, call_id):
        calls.append((name, args, context, call_id))
        native = {'result': {'value': json.dumps({'taskId': 'fixture-task', 'result': [
            {'bvid': 'BV1234567890', 'title': '插件候选', 'author': '作者'}]})},
                'receipt_ref': {'activity': context['activity_id'], 'turn': context['turn_id'], 'call_id': call_id}}
        if context['scope'] == 'scope-b':
            path = tmp_path / 'full-plugin-receipt.json'
            path.write_text(json.dumps(native), encoding='utf-8')
            return {'result_complete': False, 'full_receipt': {'file': str(path)},
                    'receipt_ref': native['receipt_ref']}
        return native
    bridge.direct.execute = execute
    try:
        first = await bridge._scan_bili_material('scope-a')
        second = await bridge._scan_bili_material('scope-b')
        assert second is not None
        assert calls[0][0] == 'harness__bili_humanism_candidates'
        assert calls[0][2]['autonomous_grant'] is True
        assert calls[0][2]['activity_id'] != calls[1][2]['activity_id']
        with sqlite3.connect(bridge.path) as db:
            payload = json.loads(db.execute('SELECT payload FROM proactive_materials WHERE id=?', (first,)).fetchone()[0])
        assert payload['provider'] == 'dsh-bili-humanism' and payload['receipt_ref']['call_id'] == 'candidates'
        found = await bridge._material_candidates('scope-a', datetime.now(timezone.utc), set())
        assert len(found) == 1 and 'https://www.bilibili.com/video/BV1234567890' in found[0].excerpt
        assert '"plays": null' in found[0].excerpt  # plugin candidates do not report play count
        bridge._save_material('scope-a', 'bili_duty', 'old-api', {'videos': [
            {'bvid': 'BV1', 'title': '旧接口候选', 'plays': 12000}]})
        assert await bridge._material_candidates('scope-a', datetime.now(timezone.utc), set()) == []
    finally:
        await bridge.close()
