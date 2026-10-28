"""Synthetic share -> confirmed send -> follow-up tool -> reply; no network/model service."""
from datetime import datetime, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest

from myatri.contracts import ModelResponse
from warmagent.proactive import Candidate
from warmagent.shared_sources import material_sources, read_shared_source, shared_source_directory
from test_warm_runtime import incoming, make_bridge


URL = 'https://example.com/reading-club?article=original'
RESULTS = {'results': [{'title': '读书会随笔', 'url': URL, 'snippet': '作者这次先听别人说。'},
                       {'title': '另一篇', 'url': 'https://example.com/other', 'snippet': '其他材料。'}]}


@pytest.mark.parametrize('results', [RESULTS, json.dumps(RESULTS), str(RESULTS)])
def test_actual_sources_survive_current_and_legacy_storage(results):
    sources = material_sources('web_curiosity', {'results': results})
    assert sources[0] == {'title': '读书会随笔', 'url': URL, 'excerpt': '作者这次先听别人说。'}
    assert len(sources) == 2
    assert material_sources('web_curiosity', {'results': "{'results': ["}) == []
    assert material_sources('web_curiosity', {'results': {'error': 'search_timeout'}}) == []


async def share(bridge, scope, kind='web_curiosity', payload=None, *, generate=False):
    mid = bridge._save_material(scope, kind, '读书会', payload or {'results': str(RESULTS)})
    candidate = SimpleNamespace(candidate_id=f'material:{mid}', kind=kind)
    draft = SimpleNamespace(parts=('刚翻到一篇读书会随笔。', '这次作者先听别人说。'), context_hash=str(mid))
    if generate:
        candidate = Candidate(candidate_id=f'material:{mid}', kind=kind,
            created_at=datetime.now(timezone.utc), excerpt=json.dumps(
                {'sources': material_sources(kind, payload or {'results': str(RESULTS)})}, ensure_ascii=False))
        generated = await bridge._proactive_generate(scope=scope, candidate=candidate,
            now=candidate.created_at, instruction='分享实际材料')
        draft = SimpleNamespace(**generated)
    result = await bridge._proactive_deliver(scope=scope, candidate=candidate, draft=draft,
                                            now=datetime.now(timezone.utc))
    return result['id']


@pytest.mark.asyncio
@pytest.mark.parametrize('channel', ['group', 'private'])
async def test_confirmed_share_reaches_real_followup_tool_and_reply(tmp_path, channel):
    call = {'id': 'source-call', 'type': 'function', 'function': {
        'name': 'read_shared_source', 'arguments': '{}'}}
    bridge, provider = make_bridge(tmp_path, [
        ModelResponse(provider='fixture', model='fixture', content='刚翻到一篇读书会随笔。'),
        ModelResponse(provider='fixture', model='fixture', content='', tool_calls=[call]),
        ModelResponse(provider='fixture', model='fixture', content='是这篇：' + URL)])
    route = incoming(channel, sender='owner' if channel == 'private' else 'member')
    bridge.register_route(route)
    app = bridge._application(route, start_workers=False)
    try:
        key = await share(bridge, route.scope, generate=True)
        assert not read_shared_source(bridge.path, route.scope)['available']  # prepared is not shared
        assert (await bridge.claim(key))['claimed']
        await bridge.acknowledge(key, 'sent')
        message_id = 'reply_' + key
        exact = read_shared_source(bridge.path, route.scope, message_id)
        assert exact['source']['url'] == URL and exact['next_index'] == 1
        other = read_shared_source(bridge.path, route.scope, message_id, source_index=1)
        assert other['source']['title'] == '另一篇' and other['next_index'] is None
        assert not read_shared_source(bridge.path, 'other-scope', message_id)['available']
        result = await bridge.receive(incoming(channel, sender=route.sender_id,
            text='这个读书会是啥，发我看看', message_id='follow-up').model_copy(
                update={'reply_to_id': message_id}))
        assert URL in result['reply']
        assert len(provider.requests) == 3
        assert URL in json.dumps(provider.requests[0].messages, ensure_ascii=False)
        first = provider.requests[1]
        assert any(tool['function']['name'] == 'read_shared_source' for tool in first.tools)
        directory = next(m['content'] for m in first.messages
                         if m.get('content', '').startswith('已发送主动分享的来源目录'))
        assert '读书会随笔' in directory and 'read_shared_source' in directory
        assert URL not in directory  # directory is small; full links are fetched on demand
        tool = next(m for m in provider.requests[2].messages if m.get('tool_call_id') == 'source-call')
        assert URL in tool['content'] and 'not_full_article_or_video' in tool['content']
        assert all(r.payload.get('role') != 'user' or r.record_id != key
                   for r in app.store.list_records('raw_message'))
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_sources_survive_reopen_and_never_expose_unknown_or_another_scope(tmp_path):
    bridge, _ = make_bridge(tmp_path)
    route = incoming('private', sender='owner')
    bridge.register_route(route)
    key = await share(bridge, route.scope, 'bili_duty', {'keyword': '读书', 'videos': [
        {'bvid': 'BV1JqxT6HEpX', 'title': '读书会视频', 'description': '搜索简介', 'plays': 12000}]})
    await bridge.claim(key)
    await bridge.acknowledge(key, 'sent')
    path = bridge.path
    await bridge.close()
    reopened, _ = make_bridge(tmp_path)
    try:
        row = read_shared_source(path, route.scope, 'reply_' + key)
        assert row['source']['url'] == 'https://www.bilibili.com/video/BV1JqxT6HEpX'
        assert row['source']['excerpt'] == '搜索简介' and row['source']['plays'] == 12000
        assert not read_shared_source(path, 'another-scope')['available']
        with sqlite3.connect(path) as db:
            assert len(shared_source_directory(db, route.scope)) == 1
            db.execute("UPDATE deliveries SET status='unknown' WHERE id=?", (key,))
        assert not read_shared_source(path, route.scope)['available']
        with sqlite3.connect(path) as db:
            assert shared_source_directory(db, route.scope) == []
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_web_scan_keeps_structured_results_and_material_does_not_cut_urls(tmp_path):
    bridge, _ = make_bridge(tmp_path, mode='live')
    route = incoming('private', sender='owner')
    bridge.register_route(route)
    bridge._application(route, start_workers=False)
    bridge.web_enabled = True
    bridge.proactive_config = {'web_scan_minutes': 0, 'bili_scan_minutes': 0}
    bridge._web_topic_query = lambda scope: '读书会'
    rows = {'results': [*RESULTS['results'], {'title': '长摘录', 'url': 'https://example.com/end',
        'snippet': '实际保留内容' * 1500}]}
    bridge._web_tool = lambda root: (None, SimpleNamespace(search=lambda **kwargs: rows))
    try:
        mid = bridge._scan_web_material(route.scope)
        with sqlite3.connect(bridge.path) as db:
            payload = json.loads(db.execute('SELECT payload FROM proactive_materials WHERE id=?', (mid,)).fetchone()[0])
        assert payload['results'] == rows
        candidates = await bridge._material_candidates(route.scope, datetime.now(timezone.utc), set())
        excerpt = json.loads(next(c.excerpt for c in candidates if c.candidate_id == f'material:{mid}'))
        assert excerpt['sources'][-1]['url'] == 'https://example.com/end'
        assert len(excerpt['sources'][-1]['excerpt']) == 400
    finally:
        await bridge.close()
