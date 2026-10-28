"""Offline synthetic archives only; no channel startup, API or real messages."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from myatri.contracts import Event, Record, SourceSpan
from myatri.memory.index import MemoryIndex
from myatri.memory.snapshot import read_snapshot
from myatri.retrieval import RecallService
from myatri.storage import ArchiveStore
from warmagent.incremental_index import IncrementalMemoryIndex, install_incremental_index


def add_fact(store, number, text, *, revision=1):
    raw = Record(record_id=f'raw-{number}-v{revision}', record_kind='raw_message',
        agent_id=store.agent_id, user_id=store.user_id, author_kind='human',
        body_markdown=text, payload={'role': 'user'})
    event = Record(record_id=f'event-{number}', revision=revision, record_kind='event',
        supersedes_revision=revision - 1 if revision > 1 else None,
        agent_id=store.agent_id, user_id=store.user_id, author_kind='system',
        source_refs=[raw.ref], body_markdown=text,
        payload=Event(occurred_at=None, participants=['owner'], fact_summary=text,
            source_spans=[SourceSpan(message_ref=raw.ref, start=0, end=len(text), quote=text)],
            event_fingerprint=f'fact-{number}-{revision}', reality_scope='reported').model_dump())
    store.commit([raw, event], idempotency_key=f'fact-{number}-{revision}',
        expected={raw.record_id: 0, event.record_id: revision - 1})
    return event


async def base_index(tmp_path):
    store = ArchiveStore(tmp_path, agent_id='warm', user_id='fixture')
    add_fact(store, 1, '星河把蓝色钥匙放在书架。')
    add_fact(store, 2, '星河喜欢在秋天散步。')
    index = MemoryIndex(store)
    await index.rebuild()
    return store, index


@pytest.mark.asyncio
async def test_delta_immediate_cached_and_restart_keeps_base_watermark(tmp_path):
    store, original = await base_index(tmp_path)
    base = original.watermark
    index = IncrementalMemoryIndex.adapt(original, merge_delay_seconds=3600)
    try:
        latest = add_fact(store, 3, '星河把备用蓝色钥匙放在抽屉。')
        result = await index.update()
        assert result == original.generation
        assert index.watermark == base != store.snapshot()
        assert index.merge_state == 'pending'
        bundle = RecallService(store, index).recall('备用蓝色钥匙', budget=4000)
        assert latest.ref in {record.ref for record in bundle.records}
        assert 'vector-delta:event' in bundle.reasons[latest.ref]
        with sqlite3.connect(index.vector_cache_path) as db:
            before = db.execute('SELECT COUNT(*) FROM vectors').fetchone()[0]
        again = RecallService(store, index).recall('备用蓝色钥匙', budget=4000)
        assert latest.ref in {record.ref for record in again.records}
        with sqlite3.connect(index.vector_cache_path) as db:
            assert db.execute('SELECT COUNT(*) FROM vectors').fetchone()[0] == before
    finally:
        await index.close()
    restarted = IncrementalMemoryIndex.adapt(MemoryIndex(store), merge_delay_seconds=3600)
    try:
        await restarted.update()
        assert restarted.watermark == base
        assert restarted.merge_state == 'pending'
        assert latest.ref in {r.ref for r in RecallService(store, restarted).recall('备用蓝色钥匙').records}
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_revision_and_historical_snapshot_do_not_leak_future(tmp_path):
    store, original = await base_index(tmp_path)
    old_claim = Record(record_id='claim', record_kind='interpretation', agent_id='warm',
        user_id='fixture', author_kind='system', source_refs=['event-1@1'],
        body_markdown='蓝色钥匙可能放在书架。', payload={'status': 'provisional'})
    store.commit([old_claim], idempotency_key='old-claim')
    await original.rebuild()
    base = original.watermark
    index = IncrementalMemoryIndex.adapt(original, merge_delay_seconds=3600)
    try:
        new = Record(record_id='claim', revision=2, supersedes_revision=1,
            record_kind='interpretation', agent_id='warm', user_id='fixture', author_kind='system',
            source_refs=['event-1@1'], body_markdown='更正推测：蓝色钥匙可能放在抽屉。',
            payload={'status': 'corrected'})
        store.commit([new], idempotency_key='corrected-claim', expected={'claim': 1})
        await index.update()
        current = RecallService(store, index).recall('蓝色钥匙', budget=4000)
        current_refs = {r.ref for r in current.records if r.record_kind == 'interpretation'}
        assert new.ref in current_refs and old_claim.ref not in current_refs
        old = RecallService(store, index).recall('蓝色钥匙', snapshot=base, budget=4000)
        old_refs = {r.ref for r in old.records}
        assert old_claim.ref in old_refs and new.ref not in old_refs
        filtered = RecallService(store, index).recall('蓝色钥匙',
            filters={'candidate_refs': ['event-2@1']}, budget=4000)
        assert new.ref not in {r.ref for r in filtered.records}
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_concurrent_updates_coalesce_and_writes_during_merge_remain_pending(tmp_path):
    store, original = await base_index(tmp_path)
    index = IncrementalMemoryIndex.adapt(original, merge_delay_seconds=3600)
    calls = []
    entered, release = asyncio.Event(), asyncio.Event()
    async def fake_build():
        calls.append(store.snapshot())
        entered.set()
        await release.wait()
        # Simulate an installed generation covering only the earlier cutoff.
        index.watermark = calls[-1]
    index._build_once = fake_build
    try:
        add_fact(store, 3, '星河的蓝色杯子在书桌。')
        await asyncio.gather(*(index.update() for _ in range(4)))
        first_task = index._merge_task
        assert not calls
        index._merge_now.set()
        await entered.wait()
        add_fact(store, 4, '星河的旅行计划改到周末。')
        await index.update()
        assert index._merge_task is first_task
        release.set()
        for _ in range(100):
            if index.merge_state == 'pending':
                break
            await asyncio.sleep(.01)
        assert len(calls) == 1
        assert index.watermark != store.snapshot()
        request = json.loads(index.request_path.read_text(encoding='utf-8'))
        assert request['target']['watermark'] == store.snapshot()
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_worker_failure_preserves_base_and_pending_request(tmp_path):
    store, original = await base_index(tmp_path)
    index = IncrementalMemoryIndex.adapt(original, merge_delay_seconds=0)
    base = index.watermark
    async def fail():
        raise RuntimeError('fixture worker failure')
    index._build_once = fail
    try:
        new = add_fact(store, 3, '星河准备整理书架。')
        await index.update()
        await index._merge_task
        assert index.merge_state == 'failed' and index.watermark == base
        assert new.ref in {r.ref for r in RecallService(store, index).recall('整理书架').records}
        assert json.loads(index.request_path.read_text(encoding='utf-8'))['state'] == 'failed'
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_real_local_worker_publishes_and_reloads_generation(tmp_path):
    store, original = await base_index(tmp_path)
    index = IncrementalMemoryIndex.adapt(original, merge_delay_seconds=3600)
    try:
        latest = add_fact(store, 3, '星河把橙色雨伞放在门口。')
        await asyncio.wait_for(index.rebuild(), 30)
        assert index.watermark == store.snapshot() and index.merge_state == 'idle'
        assert latest.ref in {hit['ref'] for hit in index.query('橙色雨伞')}
        reopened = MemoryIndex(store)
        assert reopened.generation == index.generation
        assert reopened.query('橙色雨伞') == index.query('橙色雨伞')
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_application_wiring_rebinds_all_holders(tmp_path):
    _, original = await base_index(tmp_path)
    selector = SimpleNamespace(index=original)
    context = SimpleNamespace(index=original, selector=selector)
    app = SimpleNamespace(index=original, recall=SimpleNamespace(index=original),
        conversation=SimpleNamespace(index=original, context=context))
    install_incremental_index(app, {})
    assert app.index is original
    install_incremental_index(app, {'incremental_index_enabled': True})
    assert isinstance(app.index, IncrementalMemoryIndex)
    assert app.index is app.recall.index is app.conversation.index is context.index is selector.index
    await app.index.close()


@pytest.mark.asyncio
async def test_close_terminates_owned_worker_and_keeps_pending_request(tmp_path, monkeypatch):
    store, original = await base_index(tmp_path)
    index = IncrementalMemoryIndex.adapt(original, merge_delay_seconds=0)
    started, terminated = asyncio.Event(), asyncio.Event()
    class Process:
        returncode = None
        async def wait(self):
            started.set()
            await terminated.wait()
            return self.returncode
        def terminate(self):
            self.returncode = -15
            terminated.set()
    process = Process()
    async def spawn(*args, **kwargs):
        assert args[1:3] == ('-m', 'warmagent.index_worker')
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    base = index.watermark
    add_fact(store, 3, '星河准备周末去书店。')
    await index.update()
    await asyncio.wait_for(started.wait(), 3)
    await index.close()
    assert terminated.is_set() and index.watermark == base
    assert json.loads(index.request_path.read_text(encoding='utf-8'))['state'] == 'pending'


@pytest.mark.asyncio
async def test_bridge_installs_enabled_index_and_closes_it(tmp_path):
    from test_wechat import make_bridge
    from warmagent.bridge import Incoming
    bridge, _ = make_bridge(tmp_path)
    bridge.config['memory'] = {'incremental_index_enabled': True}
    bridge.engine_config['embedding'] = {'backend': 'tfidf'}
    message = Incoming(platform_id='xiaowennuan-wechat', bot_id='bot', group_id='room@chatroom',
        sender_id='owner', sender_name='fixture', message_id='test',
        sent_at='2026-10-27T00:00:00+08:00', text='synthetic input', mentioned=True)
    try:
        app = bridge._application(message, start_workers=False)
        index = app.index
        assert isinstance(index, IncrementalMemoryIndex)
        assert app.recall.index is app.conversation.index is app.conversation.context.selector.index is index
        assert index._merge_task is None
    finally:
        await bridge.close()
    assert index._closed


@pytest.mark.asyncio
async def test_deep_merge_barrier_finishes_its_cutoff_despite_future_writes(tmp_path):
    store, original = await base_index(tmp_path)
    index = IncrementalMemoryIndex.adapt(original, merge_delay_seconds=3600)
    calls = []
    async def build_with_future_write():
        cutoff = read_snapshot(store)
        calls.append(cutoff.watermark)
        add_fact(store, 10 + len(calls), '合并期间到来的后续聊天。')
        index.watermark, index._sequence = cutoff.watermark, cutoff.sequence
        index.generation = 'index_fixture' + str(len(calls))
    index._build_once = build_with_future_write
    try:
        add_fact(store, 3, '本次深维护请求覆盖的原文。')
        await asyncio.wait_for(index.rebuild(), 3)
        assert len(calls) == 1
        assert index.watermark == calls[0] != store.snapshot()
        assert index.merge_state == 'pending' and not index._merge_task.done()
    finally:
        await index.close()
