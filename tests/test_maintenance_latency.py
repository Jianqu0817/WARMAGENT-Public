import asyncio
import json
import threading
import time

import pytest

from myatri.contracts import ModelResponse, Record, digest
from myatri.cognition.experience import IngestProgressPending
from myatri.storage import ArchiveStore
from warmagent import maintenance as maintenance_module
from warmagent.latency import run_sync
from warmagent.memory_repair import ExperienceOrganizer
from test_warm_runtime import incoming, make_bridge
from warmagent.maintenance import BridgeMaintenance


class EmptyQueue:
    def get_meta(self, name, default=None):
        return default


@pytest.mark.asyncio
async def test_slow_pending_archive_scan_does_not_block_event_loop():
    maintenance = BridgeMaintenance(type("App", (), {"queue": EmptyQueue()})(), turns=1)
    scan_finished = threading.Event()

    def slow_scan():
        time.sleep(0.12)
        scan_finished.set()
        return [], []

    maintenance.pending = slow_scan

    async def foreground_tick():
        await asyncio.sleep(0.02)
        assert not scan_finished.is_set()

    await asyncio.gather(maintenance.run([]), foreground_tick())
    assert scan_finished.is_set()


@pytest.mark.asyncio
async def test_cancelled_threaded_commit_finishes_before_stage_can_release():
    started, finished = threading.Event(), threading.Event()

    def slow_commit():
        started.set()
        time.sleep(0.06)
        finished.set()

    task = asyncio.create_task(run_sync(slow_commit))
    while not started.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.asyncio
async def test_a1_archive_work_and_sync_commit_callback_stay_off_loop(tmp_path):
    store = ArchiveStore(tmp_path / "archive", "agent", "user")
    message = Record(record_id="a1_latency_fixture", record_kind="raw_message",
        agent_id="agent", user_id="user", author_kind="human", body_markdown="合成A1来源",
        payload={"role": "user"})
    store.commit([message], "a1-latency-source")
    main_thread = threading.get_ident()
    read_threads, write_threads, gateway_threads = [], [], []
    original_list, original_commit = store.list_records, store.commit

    def list_records(kind=None, snapshot=None):
        read_threads.append(threading.get_ident())
        time.sleep(0.01)
        return original_list(kind, snapshot)

    def commit(records, idempotency_key, expected=None, **kwargs):
        write_threads.append(threading.get_ident())
        return original_commit(records, idempotency_key, expected=expected, **kwargs)

    store.list_records = list_records
    store.commit = commit

    class Gateway:
        provider = type("Provider", (), {"name": "synthetic"})()
        group_support_review = False

        async def generate(self, request):
            gateway_threads.append(threading.get_ident())
            event = {"message_ref": message.ref, "quote": "合成A1来源", "summary": "合成事件",
                "event_key": "synthetic-a1-event", "occurred_at": None,
                "reality_scope": "reported", "participants": []}
            return ModelResponse(provider="synthetic", model="schema-fixture",
                content=json.dumps({"events": [event]}, ensure_ascii=False))

    timings = {}

    def record_timing(name, seconds):
        timings[name] = round(timings.get(name, 0.0) + seconds, 4)

    def submit(proposal):
        return store.commit(proposal.records, proposal.proposal_id, expected=proposal.read_versions)

    result = await ExperienceOrganizer(store, Gateway()).organize_batch(
        [message], submit, timing_sink=record_timing)

    assert result == [message.ref]
    assert read_threads and all(thread != main_thread for thread in read_threads)
    assert len(write_threads) == 2 and all(thread != main_thread for thread in write_threads)
    assert gateway_threads == [main_thread]
    assert timings["batch_material_selection"] >= 0
    assert timings["batch_checkpoint_commit"] >= 0
    assert timings["proposal_commit"] >= 0
    assert len([r for r in store.list_records("event") if message.ref in r.source_refs]) == 1


@pytest.mark.asyncio
async def test_a1_progress_and_cursor_survive_a_failed_emotion_stage(tmp_path, monkeypatch):
    bridge, _ = make_bridge(tmp_path)
    route = incoming("group", sender="member", text="维护断点合成消息", message_id="maintenance-cursor")
    bridge.register_route(route)
    app = bridge._application(route)
    raw = Record(record_id="maintenance_cursor_fixture", record_kind="raw_message",
        agent_id="xiaowennuan", user_id="group", author_kind="human",
        body_markdown="维护断点合成消息", payload={"role": "user", "sender_id": "member"})
    app.store.commit([raw], "maintenance-cursor-fixture")
    maintenance = bridge.library_app.maintenance
    maintenance.deep_every = 1000
    maintenance.persona_every = 1000
    emotion_calls = {"count": 0}

    class FailOnceEmotion:
        def __init__(self, store, gateway, config):
            pass

        async def propose_batch(self, refs):
            emotion_calls["count"] += 1
            if emotion_calls["count"] == 1:
                raise RuntimeError("synthetic emotion interruption")
            return None

    monkeypatch.setattr(maintenance_module, "EmotionAgent", FailOnceEmotion)
    key = "maintenance_" + digest([raw.ref])[:24]
    try:
        with pytest.raises(RuntimeError, match="synthetic emotion interruption"):
            await maintenance.run([raw.ref])
        progress = app.queue.get_meta(key)
        assert progress["memory"] is True
        assert progress.get("emotion_0") is not True
        assert app.queue.get_meta("maintenance_cursor") is None
        assert len([r for r in app.store.list_records("event") if raw.ref in r.source_refs]) == 1

        result = await maintenance.run([raw.ref])
        assert result["kind"] == "maintenance_done"
        assert app.queue.get_meta("maintenance_cursor") == raw.ref
        progress = app.queue.get_meta(key)
        assert progress["memory"] is True and progress["emotion_0"] is True and progress["index"] is True
        assert progress["stage_seconds"]["memory"] >= 0
        assert progress["substage_seconds"]["memory"]["proposal_commit"] >= 0
        assert len([r for r in app.store.list_records("event") if raw.ref in r.source_refs]) == 1
        assert len([r for r in app.store.list_records("cognitive_run")
                    if r.payload.get("role") == "A1" and raw.ref in r.source_refs]) == 1
        assert (await maintenance.run([raw.ref]))["kind"] == "noop"
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_before_compact_releases_background_lock_without_losing_stage(tmp_path, monkeypatch):
    bridge, _ = make_bridge(tmp_path)
    route = incoming("group", sender="member", text="前台压缩合成消息", message_id="maintenance-compaction")
    bridge.register_route(route)
    app = bridge._application(route)
    raw = Record(record_id="maintenance_compaction_fixture", record_kind="raw_message",
        agent_id="xiaowennuan", user_id="group", author_kind="human",
        body_markdown="前台压缩合成消息", payload={"role": "user", "sender_id": "member"})
    app.store.commit([raw], "maintenance-compaction-fixture")
    maintenance = bridge.library_app.maintenance
    waiting = asyncio.Event()
    forced = asyncio.Event()
    maintenance.app.foreground_pending = lambda: (waiting.set() or True)
    original = maintenance._run_locked

    async def run_locked(refs, *, reason="turns"):
        if reason == "before_compaction":
            async with maintenance.lock:
                forced.set()
                return {"kind": "maintenance_done", "message_refs": refs}
        return await original(refs, reason=reason)

    monkeypatch.setattr(maintenance, "_run_locked", run_locked)
    background = asyncio.create_task(maintenance.run([raw.ref]))
    compaction = None
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        compaction = asyncio.create_task(maintenance.before_compact())
        with pytest.raises(IngestProgressPending):
            await asyncio.wait_for(background, 1)
        await asyncio.wait_for(compaction, 1)
        assert forced.is_set()
        assert not maintenance.lock.locked()
        assert app.queue.get_meta("maintenance_cursor") is None
        assert app.queue.get_meta("maintenance_" + digest([raw.ref])[:24]).get("memory") is not True
    finally:
        if compaction and not compaction.done():
            compaction.cancel()
            await asyncio.gather(compaction, return_exceptions=True)
        if not background.done():
            background.cancel()
            await asyncio.gather(background, return_exceptions=True)
        await bridge.close()
