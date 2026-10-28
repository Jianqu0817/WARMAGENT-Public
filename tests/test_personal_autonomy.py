"""Offline vertical slices: private notes -> model context -> outbox -> receipt."""
import json
import sqlite3
import time

import pytest

from myatri.contracts import Record
from warmagent.personal_space import PersonalSpace
from warmagent.server import pending_deliveries
from test_warm_runtime import incoming, make_bridge


def propose(bridge, scope, text, note_id='later', *, owner_requested=False, status='active', task='own-1'):
    bridge.personal_space.put(scope, note_id, 'later', text)
    bridge.activities.checkpoint(goal='自己选的活动', status=status,
        protocol={'activity_id': task, 'notification_pending': {
            'text': text, 'note_ids': [note_id], 'owner_requested': owner_requested}})


@pytest.mark.asyncio
async def test_private_tools_and_projection_do_not_depend_on_external_harness(tmp_path):
    bridge, provider = make_bridge(tmp_path)
    route = incoming('private', sender='owner')
    group = incoming('group', sender='owner')
    try:
        bridge.register_route(route)
        bridge.register_route(group)
        assert bridge._personal_context(route.scope) == {}
        raw = Record(record_id='owner-message', record_kind='raw_message', author_kind='human',
            agent_id='fixture-agent', user_id='owner',
            payload={'channel_source': route.model_dump()}, body_markdown='以后聊这件事')
        tools = await bridge._chat_handlers(raw)
        note = await tools['harness__personal_note'][1](action='write', note_id='poem',
            kind='question', text='想再看看这首诗为什么令人难过')
        assert note['shared_at'] is None
        assert 'harness__direction_update_interest' in tools
        snapshot = bridge.environment_snapshot(route.scope)
        assert snapshot['personal']['notebook']['notes'][0]['text'] == note['text']
        assert 'personal' not in bridge.environment_snapshot(group.scope)
        group_raw = raw.model_copy(update={'payload': {'channel_source': group.model_dump()}})
        assert 'harness__personal_note' not in await bridge._chat_handlers(group_raw)
        stranger = raw.model_copy(update={'payload': {'channel_source': {
            **route.model_dump(), 'sender_id': 'stranger', 'sender_name': '用户'}}})
        assert 'harness__personal_note' not in await bridge._chat_handlers(stranger)
        assert 'harness__personal_note' not in bridge._workspace_handlers(group.scope, 'chat', privileged=True)
        assert not provider.requests
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_activity_gets_fresh_private_notes_without_duplicate_environment_injection(tmp_path):
    bridge, provider = make_bridge(tmp_path)
    route = incoming('private', sender='owner')
    try:
        bridge.register_route(route)
        app = bridge._owner_application()
        bridge.personal_space.put(route.scope, 'idea', 'idea', '第一次想法')
        before = bridge._activity_memory(app, None)
        assert before['personal']['notebook']['notes'][0]['text'] == '第一次想法'
        bridge.personal_space.put(route.scope, 'idea', 'idea', '聊完以后改变了想法')
        after = bridge._activity_memory(app, None)
        assert after['personal']['notebook']['notes'][0]['text'] == '聊完以后改变了想法'
        assert 'personal' not in bridge.activity_runner.settings['environment_getter']()
        assert app.store.list_records('raw_message') == []
        assert not provider.requests
    finally:
        await bridge.close()
    # No migration is required to preserve the notebook across a normal restart.
    reopened = PersonalSpace(tmp_path / 'groups' / 'personal-space.sqlite3')
    assert reopened.get(route.scope, 'idea')['text'] == '聊完以后改变了想法'


@pytest.mark.asyncio
async def test_only_whole_confirmed_delivery_marks_the_exact_note_revision_shared(tmp_path):
    bridge, provider = make_bridge(tmp_path)
    route = incoming('private', sender='owner')
    try:
        bridge.register_route(route)
        propose(bridge, route.scope, '我想和你聊聊这个小发现。')
        await bridge._activity_notify('我想和你聊聊这个小发现。')
        delivery = pending_deliveries(bridge.path, route.scope, kind='autonomy')[0]
        with sqlite3.connect(bridge.path) as db:
            envelope = json.loads(db.execute('SELECT envelope FROM deliveries WHERE id=?',
                (delivery['id'],)).fetchone()[0])
            db.execute('UPDATE deliveries SET reply_parts=? WHERE id=?',
                (json.dumps(['我有个小发现。', '想和你聊聊。']), delivery['id']))
        assert envelope['personal_note_versions'] == {'later': 1}
        assert bridge.personal_space.get(route.scope, 'later')['shared_at'] is None
        assert (await bridge.claim(delivery['id']))['claimed']
        assert (await bridge.acknowledge(delivery['id'], 'sent', 0))['status'] == 'sending'
        assert bridge.personal_space.get(route.scope, 'later')['shared_at'] is None
        await bridge.acknowledge(delivery['id'], 'sent', 1)
        shared_at = bridge.personal_space.get(route.scope, 'later')['shared_at']
        assert shared_at is not None
        assert not bridge.self_direction.share_ready(route.scope, time.time())
        await bridge.acknowledge(delivery['id'], 'sent', 1)
        assert bridge.personal_space.get(route.scope, 'later')['shared_at'] == shared_at
        assert not provider.requests
    finally:
        await bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['unknown', 'new_revision'])
async def test_unknown_receipt_or_newer_note_remains_unshared(tmp_path, change):
    bridge, _ = make_bridge(tmp_path)
    route = incoming('private', sender='owner')
    try:
        bridge.register_route(route)
        propose(bridge, route.scope, '旧想法')
        await bridge._activity_notify('旧想法')
        delivery = pending_deliveries(bridge.path, route.scope, kind='autonomy')[0]
        await bridge.claim(delivery['id'])
        if change == 'new_revision':
            bridge.personal_space.put(route.scope, 'later', 'later', '更新以后的想法')
        await bridge.acknowledge(delivery['id'], 'unknown' if change == 'unknown' else 'sent')
        assert bridge.personal_space.get(route.scope, 'later')['shared_at'] is None
        assert bridge.self_direction.share_ready(route.scope, time.time()) is (change == 'unknown')
    finally:
        await bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['cooldown', 'unknown_in_flight'])
async def test_casual_share_waits_but_owner_reports_and_failures_are_not_suppressed(tmp_path, reason):
    bridge, provider = make_bridge(tmp_path)
    route = incoming('private', sender='owner')
    try:
        bridge.register_route(route)
        if reason == 'cooldown':
            bridge.self_direction.mark_shared(route.scope, time.time())
        else:
            with sqlite3.connect(bridge.path) as db:
                db.execute("INSERT INTO deliveries(id,scope,fingerprint,status,updated,trigger_kind) "
                    "VALUES('older',?,'fixture','unknown',?,'autonomy')", (route.scope, time.time()))
        propose(bridge, route.scope, '等以后合适的时候聊')
        await bridge._activity_notify('等以后合适的时候聊')
        assert pending_deliveries(bridge.path, route.scope, kind='autonomy') == []
        assert bridge.personal_space.get(route.scope, 'later')['shared_at'] is None
        propose(bridge, route.scope, '你安排的事做完了', note_id='report', owner_requested=True, task='owner-task')
        await bridge._activity_notify('你安排的事做完了')
        propose(bridge, route.scope, '有一步执行失败，需要你看看', note_id='failure', status='blocked', task='failed-task')
        await bridge._activity_notify('有一步执行失败，需要你看看')
        assert len(pending_deliveries(bridge.path, route.scope, kind='autonomy')) == 2
        assert not provider.requests
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_plain_idle_contact_uses_private_outbox_and_existing_share_interval(tmp_path):
    bridge, provider = make_bridge(tmp_path)
    route = incoming('private', sender='owner')
    text = '有点想找你聊天，这会儿忙不忙？'
    try:
        bridge.register_route(route)
        bridge.activities.checkpoint(protocol={'activity_id': 'idle-chat',
            'notification_pending': {'text': text, 'note_ids': [], 'owner_requested': False}})
        result = await bridge._activity_notify(text)
        assert result['status'] == 'queued'
        delivery = pending_deliveries(bridge.path, route.scope, kind='autonomy')[0]
        await bridge.claim(delivery['id'])
        await bridge.acknowledge(delivery['id'], 'sent', 0)
        assert not bridge.self_direction.share_ready(route.scope, time.time())
        next_text = '想再聊几句。'
        bridge.activities.checkpoint(protocol={'activity_id': 'idle-chat-next',
            'notification_pending': {'text': next_text, 'note_ids': [], 'owner_requested': False}})
        result = await bridge._activity_notify(next_text)
        assert result['status'] == 'deferred' and result['reason'] == 'share_cooldown'
        assert bridge.personal_space.list(route.scope) == []
        assert not provider.requests
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_receipt_projection_failure_does_not_reopen_delivery_and_ack_repairs_it(tmp_path, monkeypatch):
    bridge, _ = make_bridge(tmp_path)
    route = incoming('private', sender='owner')
    try:
        bridge.register_route(route)
        propose(bridge, route.scope, '一个准备分享的想法')
        await bridge._activity_notify('一个准备分享的想法')
        delivery = pending_deliveries(bridge.path, route.scope, kind='autonomy')[0]
        await bridge.claim(delivery['id'])
        real_mark = bridge.personal_space.mark_shared
        def fail(*args, **kwargs):
            raise sqlite3.OperationalError('synthetic projection fault')
        monkeypatch.setattr(bridge.personal_space, 'mark_shared', fail)
        assert (await bridge.acknowledge(delivery['id'], 'sent'))['status'] == 'sent'
        assert not (await bridge.claim(delivery['id']))['claimed']
        assert bridge.personal_space.get(route.scope, 'later')['shared_at'] is None
        monkeypatch.setattr(bridge.personal_space, 'mark_shared', real_mark)
        await bridge.acknowledge(delivery['id'], 'sent')
        assert bridge.personal_space.get(route.scope, 'later')['shared_at'] is not None
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_deferred_share_is_visible_to_runner_without_retry_loop(tmp_path):
    bridge, provider = make_bridge(tmp_path)
    route = incoming('private', sender='owner')
    try:
        bridge.register_route(route)
        bridge.self_direction.mark_shared(route.scope, time.time())
        propose(bridge, route.scope, '留着下次再说的想法')
        result = await bridge.activities.tick()
        assert result['reason'] == 'notification_deferred'
        protocol = bridge.activities.overview()['protocol']
        assert 'notification_pending' not in protocol
        assert protocol['last_notification']['status'] == 'deferred'
        assert protocol['last_notification']['reason'] == 'share_cooldown'
        assert bridge.personal_space.get(route.scope, 'later')['shared_at'] is None
        assert not (await bridge.activities.tick())['ran']
        assert not provider.requests
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_already_shared_unchanged_note_is_not_sent_again(tmp_path):
    bridge, _ = make_bridge(tmp_path)
    route = incoming('private', sender='owner')
    try:
        bridge.register_route(route)
        propose(bridge, route.scope, '这件事已经讲过了')
        bridge.personal_space.mark_shared(route.scope, ['later'])
        result = await bridge._activity_notify('这件事已经讲过了')
        assert result['reason'] == 'no_unshared_note'
        assert pending_deliveries(bridge.path, route.scope, kind='autonomy') == []
        state = bridge.activities.overview()
        state['protocol']['notification_pending']['note_ids'] = ['', 'missing']
        bridge.activities.checkpoint(protocol=state['protocol'])
        assert (await bridge._activity_notify('这件事已经讲过了'))['reason'] == 'no_unshared_note'
        # A changed observation is a new revision and can be shared normally.
        propose(bridge, route.scope, '现在又有了不同的想法')
        assert (await bridge._activity_notify('现在又有了不同的想法'))['status'] == 'queued'
    finally:
        await bridge.close()
