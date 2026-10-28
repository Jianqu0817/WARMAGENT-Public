"""Personal-life slices using synthetic stores and fake models only."""
import json
from datetime import datetime, timezone
import sqlite3

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
import pytest

from myatri.contracts import ModelResponse
from test_warm_runtime import incoming, make_bridge
from warmagent.local_web import LOCAL_CHAT, create_app
from warmagent.monitor import create_monitor_app
from warmagent.personal_space import PersonalSpace
from warmagent.personal_view import personal_dashboard, personal_scopes
from warmagent.self_direction import SelfDirection


def test_duplicate_new_id_preserves_original_revision_and_share_receipt(tmp_path):
    space = PersonalSpace(tmp_path / 'notes.sqlite3', allowed_source=lambda *_: True)
    first = space.put('a', 'first', 'reflection', '音乐 真好听', 'source@1')
    space.mark_shared('a', ['first'])
    shared = space.get('a', 'first')
    duplicate = space.put('a', 'new-id', 'reflection', '音乐\n真好听', 'source@1')
    assert duplicate['duplicate_of'] == 'first'
    assert duplicate['revision'] == first['revision']
    assert duplicate['updated_at'] == first['updated_at'] and duplicate['shared_at'] == shared['shared_at']
    assert space.read('a', 'new-id') is None
    assert space.put('a', 'new-source', 'reflection', '音乐 真好听', 'other@1')['note_id'] == 'new-source'
    assert space.put('b', 'other-scope', 'reflection', '音乐 真好听', 'source@1')['note_id'] == 'other-scope'
    assert space.put('a', 'question', 'question', '音乐 真好听', 'source@1')['note_id'] == 'question'
    changed = space.put('a', 'first', 'reflection', '这首音乐的鼓点值得再听一遍', 'source@1')
    assert changed['revision'] == 2 and changed['shared_at'] is None


def test_topic_selection_finds_older_material_without_padding_irrelevant_chat(tmp_path):
    space = PersonalSpace(tmp_path / 'notes.sqlite3')
    space.put('a', 'music', 'reflection', '想研究爵士音乐的节奏。')
    for i in range(24):
        space.put('a', f'new{i}', 'idea', f'今天整理数学练习第{i}页。')
    page = json.loads(space.context('a', limit=2, max_chars=700, query='爵士音乐有意思吗'))
    assert [row['note_id'] for row in page['notes']] == ['music']
    assert len(space.context('a', query='爵士音乐', max_chars=180)) <= 180
    assert not json.loads(space.context('a', query='今天吃饭了吗'))['notes']
    space.put('a', 'generic', 'reflection', '我喜欢爵士音乐。')
    assert not json.loads(space.context('a', query='我喜欢你'))['notes']
    space.archive('a', 'generic')
    assert not json.loads(space.context('b', query='爵士音乐'))['notes']
    space.archive('a', 'music')
    assert not json.loads(space.context('a', query='爵士音乐'))['notes']
    assert len(json.loads(space.context('a', query='最近在忙什么'))['notes']) == 3


def test_same_words_on_another_day_can_be_a_new_feeling(tmp_path, monkeypatch):
    clock = [1793196000.0]
    monkeypatch.setattr('warmagent.personal_space.time.time', lambda: clock[0])
    space = PersonalSpace(tmp_path / 'notes.sqlite3')
    space.put('a', 'today', 'reflection', '今天听歌很开心')
    clock[0] += 86400
    next_day = space.put('a', 'tomorrow', 'reflection', '今天听歌很开心')
    assert next_day['note_id'] == 'tomorrow' and 'duplicate_of' not in next_day


def test_interest_selection_and_recent_writing_include_shared_history(tmp_path):
    direction = SelfDirection(tmp_path / 'direction.sqlite3')
    direction.add_interest('a', 'music', '研究爵士音乐', 'source@1', motivation='喜欢节奏', next_step='听鼓点')
    direction.add_interest('a', 'math', '研究数学公式', 'source@1')
    assert [row['interest_id'] for row in direction.activity_context('a', query='爵士音乐')['interests']] == ['music']
    assert not direction.activity_context('a', query='吃饭了吗')['interests']
    space = PersonalSpace(tmp_path / 'notes.sqlite3')
    space.put('a', 'old', 'idea', '旧的未分享想法')
    space.put('a', 'new', 'idea', '刚写过的感受')
    space.mark_shared('a', ['new'])
    assert json.loads(space.context('a', limit=1, recent=True))['notes'][0]['note_id'] == 'new'


@pytest.mark.asyncio
async def test_chat_context_and_exact_text_share_use_original_outbox_receipts(tmp_path):
    text = '这首爵士音乐的鼓点很有意思。'
    bridge, provider = make_bridge(tmp_path, [ModelResponse(provider='fixture', model='fixture', content=text)])
    route = incoming('private', sender='owner', text='最近听的爵士音乐怎么样')
    try:
        bridge.register_route(route)
        note = bridge.personal_space.put(route.scope, 'music', 'later', text)
        bridge.personal_space.put(route.scope, 'math', 'idea', '想整理数学公式。')
        assert bridge._personal_context(route.scope, query='吃饭了吗') == {}
        assert bridge._personal_context(incoming().scope, query='爵士音乐') == {}
        result = await bridge.receive(route)
        assert result['status'] == 'prepared' and len(provider.requests) == 1
        environment = next(m['content'] for m in provider.requests[0].messages
                           if m.get('content', '').startswith('宿主环境快照'))
        assert text in environment and '数学公式' not in environment
        with sqlite3.connect(bridge.path) as db:
            envelope = json.loads(db.execute('SELECT envelope FROM deliveries WHERE id=?', (result['id'],)).fetchone()[0])
        assert envelope['personal_note_versions'] == {'music': note['revision']}
        assert bridge.personal_space.get(route.scope, 'music')['shared_at'] is None
        await bridge.claim(result['id'])
        await bridge.acknowledge(result['id'], 'unknown', reason_code='receipt_timeout')
        assert bridge.personal_space.get(route.scope, 'music')['shared_at'] is None
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_full_chat_send_marks_only_the_exact_note_version(tmp_path):
    text = '想再听听爵士音乐的鼓点。'
    bridge, _ = make_bridge(tmp_path, [ModelResponse(provider='fixture', model='fixture', content=text)])
    route = incoming('private', sender='owner', text='爵士音乐怎么样')
    try:
        bridge.register_route(route)
        bridge.personal_space.put(route.scope, 'music', 'later', text)
        result = await bridge.receive(route)
        await bridge.claim(result['id'])
        await bridge.acknowledge(result['id'], 'sent', 0)
        assert bridge.personal_space.get(route.scope, 'music')['shared_at'] is not None
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_read_only_dashboard_pagination_dates_and_artifact_references(tmp_path):
    bridge, provider = make_bridge(tmp_path)
    route = incoming('private', sender='owner')
    try:
        bridge.register_route(route)
        for i in range(24):
            bridge.personal_space.put(route.scope, f'n{i:02}', 'reflection', f'合成随笔第{i}篇')
        app = bridge._application(route, start_workers=False)
        source = app.store.append_message('user', '对爵士音乐感兴趣', message_id='interest-source',
                                         channel_source=route.model_dump())
        bridge.self_direction.add_interest(route.scope, 'music', '爵士音乐', source.ref)
        file = tmp_path / 'synthetic.txt'
        file.write_text('synthetic artifact', encoding='utf-8')
        artifact = bridge.artifacts.register('call', route.scope, file, 'text/plain')
        scopes = personal_scopes(bridge.root, 'owner', bridge.config['allowed_groups'])
        page = personal_dashboard(bridge.root, scopes)
        earlier = personal_dashboard(bridge.root, scopes, offset=page['next_offset'])
        assert len(page['notes']) == 20 and len(earlier['notes']) == 4
        assert page['interests'][0]['interest_id'] == 'music'
        assert page['artifacts'][0]['id'] == artifact['id']
        assert 'path' not in page['artifacts'][0]
        # Beijing midnight differs from UTC midnight.
        stamp = datetime(2026, 10, 28, 16, 30, tzinfo=timezone.utc).timestamp()
        with sqlite3.connect(bridge.personal_space.path) as db:
            db.execute('UPDATE personal_note SET updated_at=? WHERE note_id=?', (stamp, 'n00'))
        assert any(row['note_id'] == 'n00' for row in personal_dashboard(bridge.root, scopes, day='2026-10-29')['notes'])
        assert all(row['shared_at'] is None for row in page['notes'])
        with pytest.raises(PermissionError):
            personal_dashboard(bridge.root, scopes, scope='other-group')
        assert not provider.requests
    finally:
        await bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('surface', ['monitor', 'local'])
async def test_personal_page_requires_session_and_does_not_send_or_share(tmp_path, surface):
    bridge, provider = make_bridge(tmp_path)
    if surface == 'monitor':
        route = incoming('private', sender='owner')
        bridge.register_route(route)
        (tmp_path / 'config').mkdir()
        (tmp_path / 'config/bridge.local.toml').write_text(
            '[bridge]\nowner_id="owner"\nstate_root="groups"\nallowed_groups=["fixture|owner"]\n', encoding='utf-8')
        app = create_monitor_app(tmp_path)
    else:
        app = create_app(bridge)
        route = app[LOCAL_CHAT].route(app[LOCAL_CHAT].selected_scope)
    bridge.personal_space.put(route.scope, 'note', 'later', '<script>synthetic text</script>')
    file = tmp_path / 'artifact.html'
    file.write_text('<script>synthetic artifact</script>', encoding='utf-8')
    artifact = bridge.artifacts.register('call', route.scope, file, 'text/html')
    client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    try:
        assert (await client.get('/api/personal')).status == 403
        assert (await client.get('/personal')).status == 200
        assert (await client.get('/assets/personal/personal_view.js')).status == 200
        response = await client.get('/api/personal')
        assert response.status == 200
        assert (await response.json())['notes'][0]['text'].startswith('<script>')
        assert (await client.get('/api/personal?scope=other')).status == 403
        assert (await client.get('/api/personal?day=not-a-date')).status == 400
        assert (await client.get('/api/personal', headers={'Origin': 'https://example.invalid'})).status == 403
        url = '/api/personal/artifact?scope='+route.scope+'&id='+artifact['id']
        download = await client.get(url)
        assert download.status == 200 and 'attachment;' in download.headers['Content-Disposition']
        assert download.content_type == 'application/octet-stream'
        assert (await client.get('/api/personal/artifact?scope=other&id='+artifact['id'])).status == 403
        file.write_text('changed', encoding='utf-8')
        assert (await client.get(url)).status == 409
        assert bridge.personal_space.get(route.scope, 'note')['shared_at'] is None
        assert not provider.requests
    finally:
        await client.close()
        await bridge.close()
