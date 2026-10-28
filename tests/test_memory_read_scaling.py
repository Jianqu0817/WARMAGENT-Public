"""Bound archive reads while retaining exact provenance and channel ACLs."""
from myatri.contracts import Record, digest
from myatri.storage import ArchiveStore
from warmagent.group_memory import GroupRecall
from warmagent.identity import IdentityRegistry
from warmagent.memory_timeline import MemoryTimeline
from warmagent.shared_runtime import MemoryVisibility


def _library(tmp_path, count=30):
    store = ArchiveStore(tmp_path / 'archive', agent_id='warmagent', user_id='read-scaling')
    scope = digest(['fixture', 'bot', 'room'])[:32]
    rows = []
    for number in range(count):
        private = number == count - 1
        raw = Record(record_id=f'raw_{number}', record_kind='raw_message',
            agent_id=store.agent_id, user_id=store.user_id, author_kind='human',
            created_at='2026-10-24T12:00:00+00:00', body_markdown=f'读书会安排 {number}',
            payload={'role': 'user', 'channel_source': {'platform_id': 'fixture',
                'bot_id': 'bot', 'group_id': 'room',
                'conversation_kind': 'private' if private else 'group'}})
        event = Record(record_id=f'event_{number}', record_kind='event',
            agent_id=store.agent_id, user_id=store.user_id, author_kind='system',
            created_at=raw.created_at, body_markdown=raw.body_markdown,
            source_refs=[raw.ref], payload={'occurred_at': raw.created_at,
                'fact_summary': raw.body_markdown, 'source_spans': [
                    {'message_ref': raw.ref, 'start': 0, 'end': 3, 'quote': '读书会'}],
                'event_fingerprint': str(number), 'reality_scope': 'reported'})
        rows.extend((raw, event))
    store.commit(rows, 'initial')
    return store, scope


def test_recall_reads_authority_once_and_keeps_acl_after_sender_switch(tmp_path, monkeypatch):
    store, scope = _library(tmp_path)
    ids = IdentityRegistry(tmp_path / 'identities.sqlite3', owner_id='owner', owner_name='Owner')
    recall = GroupRecall(store, None, ids, scope)
    recall.shared_visibility = True
    recall.current_sender = 'owner'
    original = store._load
    loads = []

    def counted(*args, **kwargs):
        loads.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, '_load', counted)
    first = recall.recall('读书会安排', budget=20000)
    assert len(loads) == 1
    assert first.records and 'event_29' not in {r.record_id for r in first.records}
    first.records[0].payload['external_mutation'] = True
    recall.current_sender = 'member'
    second = recall.recall('读书会安排', budget=20000)
    assert len(loads) == 2
    assert 'event_29' not in {r.record_id for r in second.records}
    assert not any(r.payload.get('external_mutation') for r in second.records)
    returned = {r.ref for r in second.records}
    assert any(r.record_kind == 'event' and set(r.source_refs) <= returned for r in second.records)


def test_timeline_source_traversal_reads_authority_once_per_operation(tmp_path, monkeypatch):
    store, scope = _library(tmp_path)
    timeline = MemoryTimeline(store, MemoryVisibility(store, scope))
    original = store._load
    loads = []

    def counted(*args, **kwargs):
        loads.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, '_load', counted)
    result = timeline.view(scope, '2026-10-25T12:00:00Z')
    assert len(loads) == 1
    assert result['recent_total'] == 29
    assert timeline.view(scope, '2026-10-25T12:00:00Z') is result
    assert len(loads) == 2
    expanded = timeline.expand('2026-10', scope)
    assert len(loads) == 3
    assert expanded['sources']
    assert all(row['ref'] != 'raw_29@1' for row in expanded['sources'])


def test_recall_explicit_old_snapshot_excludes_new_records(tmp_path):
    store, scope = _library(tmp_path, count=3)
    old = store.snapshot()
    newer = Record(record_id='new_source', record_kind='raw_message',
        agent_id=store.agent_id, user_id=store.user_id, author_kind='human',
        body_markdown='读书会新地点', payload={'role': 'user'})
    store.commit([newer], 'newer')
    ids = IdentityRegistry(tmp_path / 'identities.sqlite3', owner_id='owner', owner_name='Owner')
    recall = GroupRecall(store, None, ids, scope)
    recall.shared_visibility = True
    recall.current_sender = 'member'
    result = recall.recall('读书会新地点', snapshot=old, budget=20000)
    assert result.snapshot == old
    assert 'new_source' not in {r.record_id for r in result.records}
