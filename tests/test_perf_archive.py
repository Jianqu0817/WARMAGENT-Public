"""Selective copies keep immutable archive, snapshot and integrity semantics."""
import json
import os
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest

from myatri.contracts import Record
from myatri.contracts import digest
from myatri.storage.archive import ArchiveStore, ArchiveError, ConflictError, IntegrityError, IdempotencyError


def source(identifier='original', **updates):
    return Record(record_id=identifier, record_kind='raw_message', agent_id='perf', user_id='fixture',
        author_kind='human', body_markdown='合成原文', payload={'role': 'user', 'nested': {'values': [1, 2]}}, **updates)


def test_selective_returns_and_private_load_have_independent_nested_values(tmp_path):
    store = ArchiveStore(tmp_path, 'perf', 'fixture')
    row = source()
    store.commit([row], 'seed')
    store.get_record(row.record_id).payload['nested']['values'].append(99)
    store.list_records('raw_message')[0].payload['nested']['values'].clear()
    manifests, records = store._load()
    manifests[0]['records'].clear()
    records[row.ref].payload['nested']['values'].append(98)
    assert store.get_record(row.record_id).model_dump() == row.model_dump()
    assert store.verify_archive() == []


def test_snapshots_revisions_and_append_do_not_change_cached_past(tmp_path):
    store = ArchiveStore(tmp_path, 'perf', 'fixture')
    root = source()
    first = store.commit([root], 'seed')
    version = Record(record_id='run', record_kind='cognitive_run', agent_id='perf', user_id='fixture',
        source_refs=[root.ref], body_markdown='版本一')
    second = store.commit([version], 'v1')
    assert store.snapshot() == second
    old = version.model_dump(exclude={'content_hash'}) | {'revision': 2, 'supersedes_revision': 1, 'body_markdown': '版本二'}
    store.commit([Record.model_validate(old)], 'v2', expected={'run': 1})
    assert store.get_record('run').revision == 2
    assert store.get_record('run', 1).body_markdown == '版本一'
    assert store.get_record('run', snapshot=second).revision == 1
    assert [r.record_id for r in store.list_records(snapshot=first)] == [root.record_id]
    assert store.list_records('missing_kind') == []
    with pytest.raises(ConflictError):
        store.commit([Record.model_validate(old)], 'bad-cas', expected={'run': 1})
    assert store.commit([version], 'v1') == second
    with pytest.raises(IdempotencyError):
        store.commit([source('other')], 'v1')


@pytest.mark.parametrize('failure', ['after_files', 'after_manifest', 'after_projection'])
def test_crash_publication_and_projection_recovery(tmp_path, failure):
    store = ArchiveStore(tmp_path, 'perf', 'fixture')
    first = source()
    snapshot = store.commit([first], 'seed')
    store.snapshot()
    extra = source('extra')
    with pytest.raises(RuntimeError, match='Injected crash'):
        store.commit([extra], 'extra', fail_at=failure)
    recovered = ArchiveStore(tmp_path, 'perf', 'fixture')
    assert len(recovered.list_records(snapshot=snapshot)) == 1
    assert len(recovered.list_records()) == (1 if failure == 'after_files' else 2)
    recovered.commit([extra], 'extra')
    assert recovered.get_record('extra').model_dump() == extra.model_dump()
    assert recovered.verify_archive() == []


def test_file_change_is_detected_and_forced_verification_rehashes(tmp_path):
    store = ArchiveStore(tmp_path, 'perf', 'fixture')
    row = source()
    store.commit([row], 'seed')
    assert store.get_record('original') == row
    manifest = json.loads(next(store.manifest_path.glob('*.json')).read_text(encoding='utf-8'))
    path = store.domain_path / manifest['records'][0]['path']
    before = path.stat()
    path.write_bytes(path.read_bytes().replace('合成原文'.encode(), '伪造原文'.encode()))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert store.verify_archive()
    with pytest.raises(IntegrityError):
        store.get_record('original')


def test_concurrent_reads_do_not_expose_shared_mutable_records(tmp_path):
    store = ArchiveStore(tmp_path, 'perf', 'fixture')
    row = source()
    store.commit([row], 'seed')

    def read(number):
        result = store.get_record('original')
        result.payload['nested']['values'].append(number)
        return result.payload['nested']['values']

    with ThreadPoolExecutor(4) as pool:
        assert list(pool.map(read, range(8))) == [[1, 2, i] for i in range(8)]
    assert store.get_record('original') == row


def test_pinned_read_phase_is_read_only_and_does_not_see_future_commits(tmp_path):
    store = ArchiveStore(tmp_path, 'perf', 'fixture')
    first = store.commit([source()], 'first')
    view = store.read_view()
    store.commit([source('later')], 'later')
    assert view.snapshot() == first
    assert store.snapshot() != first
    with pytest.raises(KeyError):
        view.get_record('later')
    with pytest.raises(ArchiveError, match='Read views cannot'):
        view.commit([source('forbidden')], 'forbidden')
    with pytest.raises(ArchiveError, match='Unknown snapshot'):
        view.list_records(snapshot=store.snapshot())
    view.get_record('original').payload['nested']['values'].clear()
    assert store.get_record('original').payload['nested']['values'] == [1, 2]
    assert view.get_record('original').payload['nested']['values'] == [1, 2]


def test_pinned_phase_validates_once_but_authoritative_reads_still_check_files(tmp_path, monkeypatch):
    store = ArchiveStore(tmp_path, 'perf', 'fixture')
    store.commit([source()], 'first')
    store.get_record('original')
    calls = []
    fingerprint = store._fingerprint

    def counted(path):
        calls.append(path)
        return fingerprint(path)

    monkeypatch.setattr(store, '_fingerprint', counted)
    view = store.read_view()
    initial = len(calls)
    assert initial > 0
    for _ in range(5):
        view.snapshot()
        view.get_record('original')
        view.list_records('raw_message')
    assert len(calls) == initial
    store.get_record('original')
    assert len(calls) > initial


@pytest.mark.asyncio
async def test_a1_already_organized_cutoff_uses_one_verified_phase(tmp_path, monkeypatch):
    from warmagent.memory_repair import ExperienceOrganizer

    store = ArchiveStore(tmp_path, 'perf', 'fixture')
    messages = [source(f'source_{i}') for i in range(8)]
    runs = [Record(record_id='run_exp_' + digest({'message': row.ref, 'version': 'v1'})[:24],
        record_kind='cognitive_run', agent_id='perf', user_id='fixture', source_refs=[row.ref],
        payload={'decision': 'organized'}) for row in messages]
    store.commit([*messages, *runs], 'organized')
    store.snapshot()
    calls = []
    fingerprint = store._fingerprint

    def counted(path):
        calls.append(path)
        return fingerprint(path)

    monkeypatch.setattr(store, '_fingerprint', counted)
    store.read_view()
    one_phase = len(calls)
    calls.clear()

    def unexpected_commit(_proposal):
        raise AssertionError('already organized cutoff must not write or call a model')

    assert await ExperienceOrganizer(store, SimpleNamespace()).organize_batch(messages, unexpected_commit) == []
    assert len(calls) == one_phase
