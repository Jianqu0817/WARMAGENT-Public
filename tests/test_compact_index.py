"""Offline generations: ranking, sparse matrices, old files, rollback and faults."""
import asyncio
import json

import numpy as np
import pytest
from scipy import sparse

from myatri.memory.index import MemoryIndex
from myatri.retrieval import RecallService
from warmagent.compact_index import CompactMemoryIndex
from warmagent.incremental_index import IncrementalMemoryIndex
from test_incremental_index import add_fact
from myatri.storage import ArchiveStore
from benchmarks.compact_index_perf import FixedLocalVectors


def archive(tmp_path):
    store = ArchiveStore(tmp_path, agent_id='warm', user_id='fixture')
    add_fact(store, 1, '星河把蓝色钥匙放在书架。')
    add_fact(store, 2, '星河喜欢在秋天散步。')
    add_fact(store, 3, '星河把橙色雨伞放在门口。')
    return store


def test_sparse_roundtrip_matches_dense_recall_and_async_query(tmp_path):
    store = archive(tmp_path)
    baseline = MemoryIndex(store)
    asyncio.run(baseline.rebuild())
    query = '橙色雨伞放在哪里'
    old_hits = baseline.query(query)
    old_refs = [r.ref for r in RecallService(store, baseline).recall(query).records]
    compact = CompactMemoryIndex(store)
    asyncio.run(compact.rebuild())
    reopened = CompactMemoryIndex(store)
    assert sparse.isspmatrix_csr(reopened.indexes['event'].matrix)
    hits = reopened.query(query)
    assert [h['ref'] for h in hits] == [h['ref'] for h in old_hits]
    np.testing.assert_allclose([h['score'] for h in hits], [h['score'] for h in old_hits], atol=1e-14)
    assert asyncio.run(reopened.aquery(query)) == hits
    assert [r.ref for r in RecallService(store, reopened).recall(query).records] == old_refs
    assert set(compact.graph.nodes) == set(baseline.graph.nodes)
    assert {(e['from_ref'], e['to_ref']) for e in compact.graph.edges} == {(e['from_ref'], e['to_ref']) for e in baseline.graph.edges}
    state = json.loads((reopened.path / 'generations' / f'{reopened.generation}.json').read_text(encoding='utf-8'))['data']
    assert state['embedding_cache'] == {}
    assert all('vector' not in row for data in state['indexes'].values() for row in data['rows'])


def test_legacy_load_and_both_rollback_directions(tmp_path):
    store = archive(tmp_path)
    legacy = MemoryIndex(store)
    first = asyncio.run(legacy.rebuild())
    index = CompactMemoryIndex(store)
    assert index.generation == first
    second = asyncio.run(index.rebuild())
    index.rollback(first)
    assert MemoryIndex(store).generation == first
    index.rollback(second)
    assert CompactMemoryIndex(store).generation == second


def test_corruption_or_missing_sidecar_never_changes_pointer(tmp_path):
    store = archive(tmp_path)
    index = CompactMemoryIndex(store)
    asyncio.run(index.rebuild())
    pointer = (index.path / 'current.json').read_bytes()
    sidecar = next((index.path / 'generations').glob('*.npz'))
    original = sidecar.read_bytes()
    sidecar.write_bytes(original[:-10])
    with pytest.raises(ValueError, match='checksum'):
        CompactMemoryIndex(store)
    sidecar.unlink()
    with pytest.raises(FileNotFoundError):
        index.rollback(index.generation)
    assert (index.path / 'current.json').read_bytes() == pointer


def test_interrupted_sidecar_write_keeps_previous_generation(tmp_path, monkeypatch):
    store = archive(tmp_path)
    index = CompactMemoryIndex(store)
    first = asyncio.run(index.rebuild())
    from warmagent import compact_index
    write = compact_index._atomic_write
    def interrupted(path, data):
        if path.suffix == '.npz':
            raise OSError('synthetic full disk')
        return write(path, data)
    monkeypatch.setattr(compact_index, '_atomic_write', interrupted)
    with pytest.raises(OSError):
        asyncio.run(index.rebuild())
    assert CompactMemoryIndex(store).generation == first


@pytest.mark.asyncio
async def test_compact_worker_restart_and_delta(tmp_path):
    store = archive(tmp_path)
    original = CompactMemoryIndex(store)
    await original.rebuild()
    index = IncrementalMemoryIndex.adapt(original, merge_delay_seconds=3600)
    try:
        event = add_fact(store, 4, '星河把紫色手套放在玄关。')
        await index.update()
        assert event.ref in {r.ref for r in RecallService(store, index).recall('紫色手套').records}
        await asyncio.wait_for(index.rebuild(), 30)
        reopened = CompactMemoryIndex(store)
        assert reopened.generation == index.generation
        assert reopened.query('紫色手套') == index.query('紫色手套')
    finally:
        await index.close()


def test_dense_memmap_space_filter_and_query_parity(tmp_path):
    store = archive(tmp_path)
    provider = FixedLocalVectors()
    baseline = MemoryIndex(store, provider)
    asyncio.run(baseline.rebuild())
    expected = baseline.query('蓝色钥匙')
    index = CompactMemoryIndex(store, provider)
    asyncio.run(index.rebuild())
    reopened = CompactMemoryIndex(store, provider)
    assert isinstance(reopened.indexes['event'].raw_matrix, np.memmap)
    assert not reopened.indexes['event'].raw_matrix.flags.writeable
    assert reopened.query('蓝色钥匙') == expected
    with pytest.raises(ValueError, match='different vector space'):
        reopened.query('蓝色钥匙', space_id='wrong-space')
    selected = reopened.query('蓝色钥匙', _allowed_refs={'event-1@1'})
    assert all(row['ref'] == 'event-1@1' for row in selected)


@pytest.mark.asyncio
async def test_bridge_compact_factory_is_used_before_loading_and_wired_everywhere(tmp_path):
    from test_wechat import make_bridge
    from warmagent.bridge import Incoming
    bridge, _ = make_bridge(tmp_path)
    bridge.config['memory'] = {'incremental_index_enabled': True, 'compact_index_enabled': True}
    bridge.engine_config['embedding'] = {'backend': 'tfidf'}
    message = Incoming(platform_id='xiaowennuan-wechat', bot_id='bot', group_id='room@chatroom',
        sender_id='owner', sender_name='fixture', message_id='compact-test',
        sent_at='2026-10-29T00:00:00+08:00', text='synthetic input', mentioned=True)
    try:
        app = bridge._application(message, start_workers=False)
        assert app.index._compact_enabled
        assert app.index is app.recall.index is app.conversation.index is app.conversation.context.selector.index
    finally:
        await bridge.close()
