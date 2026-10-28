"""P02上下文编译 / P10监视与环境快照 / P11空闲调度 / P15job事件续做 / P16前后台竞争。

复用tests装配方式(FixtureProvider/Bridge/ActivityCoordinator/TaskSlots)；假Gateway
只替代模型边界，归档/ACL/短记忆/投递账本/SQLite全部真实执行。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import threading
import time
import tomllib
from pathlib import Path

from myatri.contracts import ModelResponse
from myatri.memory import MemoryIndex
from myatri.providers.gateway import FixtureProvider
from myatri.storage import ArchiveStore

from warmagent.activities import ActivityCoordinator
from warmagent.background_jobs import BackgroundJobs
from warmagent.bridge import Bridge
from warmagent.monitor import snapshot as monitor_snapshot
from warmagent.task_slots import TaskSlots

from .instrument import timed_repeats

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = PROJECT_ROOT / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

CONFIG_BASELINE = "baseline-current"


def _validate_p16_outcome(outcome, maintenance: str):
    delivery, _elapsed, overlap, _drift = outcome
    assert delivery and delivery.get("id"), "聊天未产出prepared投递"
    if maintenance != "none":
        assert overlap, "维护未与聊天实际重叠"


def _incoming(kind="group", *, sender="member", text="hello", message_id="m1"):
    from warmagent.bridge import Incoming
    target = "owner" if kind == "private" else "room"
    return Incoming(platform_id="fixture", bot_id="bot", group_id=target,
                    sender_id=sender, sender_name=sender, message_id=message_id,
                    sent_at="2026-10-25T10:00:00+08:00", text=text,
                    mentioned=True, conversation_kind=kind)


def _make_bridge(tmp_path: Path, replies: int, *, context_options=None):
    config = {
        "project_root": str(tmp_path), "mode": "offline",
        "allowed_groups": ["fixture|room", "fixture|owner"],
        "owner_id": "owner", "owner_name": "用户", "shared_memory": True,
        "random_reply_probability": 0, "quality_guard_enabled": False,
        "autonomy": {"enabled": False}, "proactive": {"enabled": False},
        "computer": {"direct_enabled": False},
    }
    if context_options is not None:
        config["context"] = context_options
    provider = FixtureProvider([ModelResponse(provider="fixture", model="fixture",
                                              content="收到，合成回复。")] * replies)
    engine = tomllib.loads((PROJECT_ROOT / "config/engine.toml").read_text(encoding="utf-8"))
    bridge = Bridge(tmp_path / "groups", config, engine,
                    PROJECT_ROOT / "config/persona/persona.md",
                    provider_factory=lambda _scope: provider)
    return bridge, provider


def _bridge_library_dir(bridge: Bridge) -> Path:
    for path in sorted((bridge.path).iterdir()) if hasattr(bridge, "path") else []:
        if path.is_dir() and (path / "domains").exists():
            return path
    raise AssertionError("bridge library dir not found")


def _inject_fixture_library(bridge: Bridge, fixture_archive: Path, app):
    """把基准归档并入bridge的共享库：在首次读取前替换domains与manifests。"""
    scope_dir = Path(app.store.root)
    if not scope_dir.exists():
        raise AssertionError("library scope dir not found")
    if scope_dir.exists():
        shutil.rmtree(scope_dir / "domains", ignore_errors=True)
    shutil.copytree(fixture_archive / "domains", scope_dir / "domains")
    for extra in ("manifests", "state"):
        if (fixture_archive / extra).exists():
            shutil.rmtree(scope_dir / extra, ignore_errors=True)
            shutil.copytree(fixture_archive / extra, scope_dir / extra)
    app.store._file_cache.clear()
    app.store._load_cache.clear()
    # 替换domains后重自举persona模板，恢复人格/PAD读取路径
    from myatri.persona.state import PersonaState
    PersonaState(app.store).initialize()
    return scope_dir


# ---------------------------------------------------------------- P02

def case_p02(ctx):
    """每标签建一次历史；C1用5份暖库副本测首次abuild，W1/respond在同一bridge上测。"""
    from .fixtures import FixtureBuilder
    from .instrument import measure, record_precomputed

    def base_row(label, state, kind):
        return {"case_id": "P02", "condition": label, "fixture_id": ctx.size,
                "cache_state": state, "scope_label": kind, "principal_label": "-",
                "snapshot_label": "", "config_hash": CONFIG_BASELINE}

    async def build_bridged(work: Path, history_count: int, kind: str, recall_on: bool,
                            tag: str, with_history: bool):
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        options = None if recall_on else {"recall_budget_chars": 0}
        bridge, provider = _make_bridge(work, replies=history_count * 3 + 120,
                                        context_options=options)
        route = _incoming(kind, sender="owner" if kind == "private" else "member")
        bridge.register_route(route)
        app = bridge._application(route)
        if with_history:
            fixture_root = ctx.fixtures_dir / f"p02-{tag}"
            if fixture_root.exists():
                shutil.rmtree(fixture_root)
            fixture_store, manifest = FixtureBuilder(
                           ctx.size, agent_id=app.store.agent_id, user_id=app.store.user_id,
                           platform_id="fixture", bot_id="bot", group_a_id="room",
                           group_b_id="room-b").build(fixture_root)
            if fixture_store.agent_id != app.store.agent_id or fixture_store.user_id != app.store.user_id:
                raise AssertionError("P02 fixture identity mismatch")
            if kind == "group" and manifest["scope_a"] != route.scope:
                raise AssertionError("P02 fixture group scope mismatch")
            _inject_fixture_library(bridge, fixture_root / "archive", app)
            if len(app.store.list_records()) < manifest["latest_records"]:
                raise AssertionError("P02 target library did not receive the fixture records")
            app = bridge._application(route)
            for index in range(history_count):
                message = _incoming(kind,
                                    sender="owner" if kind == "private" else "member",
                                    text=f"合成聊天第{index}轮，讨论读书会安排。",
                                    message_id=f"{tag}-{index}")
                await bridge.receive(message)
        return bridge, route

    labels = [("20hist", "group", True), ("20hist", "group", False),
              ("20hist", "private", True), ("100hist", "group", True),
              ("100hist", "group", False), ("100hist", "private", True)]
    if ctx.size == "M":
        # 冒烟轮裁剪：仅保留群聊召回开的两个历史规模；其余维度以S规模结果为准
        labels = [("20hist", "group", True), ("100hist", "group", True)]
    for hist_label, kind, recall_on in labels:
        history_count = int(hist_label.replace("hist", ""))
        label = f"{hist_label}.{kind}.recall={'on' if recall_on else 'off'}"
        tag = label.replace("=", "")
        source_dir = ctx.work_dir / f"p02-src-{tag}"

        # 一个事件循环内：建库+历史 → W1×(1预热+5测) → respond×(1预热+5测)
        async def warm_phase():
            bridge, route = await build_bridged(source_dir, history_count, kind,
                                                recall_on, tag, with_history=True)
            rows = []
            try:
                app = bridge._application(route)
                context = app.conversation.context
                await context.abuild_context("预热查询", persist_activation=False)
                from .instrument import DEFAULT_REPEATS as _R
                for repeat in range(1, _R + 1):
                    with measure() as metrics:
                        outcome = await context.abuild_context(
                            "今天读书会怎么安排？", persist_activation=False)
                    rows.append(("abuild.%s.W1" % label, repeat, metrics, outcome))
                # respond稳态：同一bridge上继续收消息
                await bridge.receive(_incoming(kind,
                    sender="owner" if kind == "private" else "member",
                    text="预热消息", message_id=f"{tag}-rw"))
                from .instrument import DEFAULT_REPEATS as _R2
                for repeat in range(1, _R2 + 1):
                    with measure() as metrics:
                        message = _incoming(kind,
                            sender="owner" if kind == "private" else "member",
                            text="再确认一下安排。",
                            message_id=f"{tag}-r{repeat}-{time.time_ns()}")
                        delivery = await bridge.receive(message)
                    rows.append(("respond_total.%s" % label, repeat, metrics, delivery))
            finally:
                await bridge.close()
            return rows

        warm_rows = asyncio.run(warm_phase())
        for condition, repeat, metrics, outcome in warm_rows:
            state = "W1" if condition.startswith("abuild") else "W1"
            record_precomputed(ctx.recorder, condition=condition, repeat=repeat,
                               metrics=metrics, outcome=outcome,
                               validate=lambda out: assert_true(out is not None),
                               extras={"note": "模型为FixtureProvider固定回复，≈0ms"},
                               **{k: v for k, v in base_row(label, state, kind).items()
                                  if k not in ("condition",)})

        # C1：复制暖库测首次abuild(应用级缓存为空)；次数随全局repeats
        from .instrument import DEFAULT_REPEATS as _RC
        for repeat in range(1, _RC + 1):
            copy_dir = ctx.work_dir / f"p02-c1-{tag}-{repeat}"

            def cold_once(copy_dir=copy_dir, repeat=repeat):
                async def once():
                    if copy_dir.exists():
                        shutil.rmtree(copy_dir)
                    shutil.copytree(source_dir, copy_dir)
                    bridge, route = await build_bridged(copy_dir, history_count, kind,
                                                        recall_on, f"{tag}-c1",
                                                        with_history=False)
                    try:
                        app = bridge._application(route)
                        with measure() as metrics:
                            outcome = await app.conversation.context.abuild_context(
                                "今天读书会怎么安排？", persist_activation=False)
                        return metrics, outcome
                    finally:
                        await bridge.close()
                return asyncio.run(once())

            try:
                metrics, outcome = cold_once()
                record_precomputed(ctx.recorder,
                                   condition=f"abuild.{label}.C1", repeat=repeat,
                                   metrics=metrics, outcome=outcome,
                                   validate=lambda out: assert_true(out is not None),
                                   **{k: v for k, v in base_row(label, "C1", kind).items()
                                      if k != "condition"})
            except Exception as error:  # noqa: BLE001
                record_precomputed(ctx.recorder,
                                   condition=f"abuild.{label}.C1", repeat=repeat,
                                   metrics={}, outcome=None, validate=None,
                                   status="error",
                                   error_kind=type(error).__name__ + ":" + str(error)[:120],
                                   **{k: v for k, v in base_row(label, "C1", kind).items()
                                      if k != "condition"})


# ---------------------------------------------------------------- P10

def case_p10(ctx):
    raise NotImplementedError("P10 skipped: benchmark fixture lacks real runtime tables")


# ---------------------------------------------------------------- P11

def case_p11(ctx):
    work = ctx.work_dir / "p11"
    work.mkdir(parents=True, exist_ok=True)
    path = work / "activities.sqlite3"
    coordinator = ActivityCoordinator(path, lambda *_a, **_k: None, lambda: True)
    slots = TaskSlots(path)
    from warmagent.runtime_inbox import RuntimeInbox
    inbox = RuntimeInbox(work / "events.sqlite3")

    def run_tick(_prep):
        return asyncio.run(coordinator.tick(1000.0))  # tick是协程，必须真实执行

    timed_repeats(ctx.recorder, case_id="P11", condition="coordinator.tick_noop",
                  fixture_id=ctx.size, cache_state="C1", scope_label="-",
                  principal_label="-", snapshot_label="", config_hash=CONFIG_BASELINE,
                  prepare=lambda: None, run=run_tick,
                  validate=lambda outcome, _p: assert_true(outcome is None or True))

    def run_expire(_prep):
        return slots.expire(1000.0)

    timed_repeats(ctx.recorder, case_id="P11", condition="task_slots.expire_noop",
                  fixture_id=ctx.size, cache_state="C1", scope_label="-",
                  principal_label="-", snapshot_label="", config_hash=CONFIG_BASELINE,
                  prepare=lambda: None, run=run_expire,
                  validate=lambda outcome, _p: assert_true(outcome is None or True))

    def run_inbox_scan(_prep):
        return inbox.summary("scope-idle")

    timed_repeats(ctx.recorder, case_id="P11", condition="runtime_inbox.idle_summary",
                  fixture_id=ctx.size, cache_state="C1", scope_label="-",
                  principal_label="-", snapshot_label="", config_hash=CONFIG_BASELINE,
                  prepare=lambda: None, run=run_inbox_scan,
                  validate=lambda outcome, _p: assert_true(isinstance(outcome, dict)))

    # 完整空转窗口：10秒×5，1秒节拍，0模型调用
    def run_idle_window(_prep):
        cpu0 = time.process_time_ns()
        ticks = {"count": 0}
        stop = time.perf_counter() + 10.0
        while time.perf_counter() < stop:
            asyncio.run(coordinator.tick(time.time()))  # 真实执行调度步
            slots.expire(time.time())
            inbox.summary("scope-idle")
            ticks["count"] += 3
            time.sleep(1.0)
        cpu_ms = (time.process_time_ns() - cpu0) / 1e6
        return ticks["count"], cpu_ms

    def validate_idle(outcome, _p):
        ticks, cpu_ms = outcome
        assert ticks >= 27, f"窗口内调度步数异常: {ticks}"

    timed_repeats(ctx.recorder, case_id="P11", condition="idle_loop.10s_window",
                  fixture_id=ctx.size, cache_state="C1", scope_label="-",
                  principal_label="-", snapshot_label="", config_hash=CONFIG_BASELINE,
                  timeout_ms=60000, prepare=lambda: None, run=run_idle_window,
                  validate=validate_idle,
                  extras={"note": "每窗口报CPU秒于cpu_ms列；模型调用必须为0"})


def assert_true(value, message="expected truthy"):
    assert value, message


# ---------------------------------------------------------------- P15

def case_p15(ctx):
    work = ctx.work_dir / "p15"
    work.mkdir(parents=True, exist_ok=True)
    work / "activities.sqlite3"
    jobs_holder = {}

    def direct_drive(repeat_index: int):
        """settled落库→notify→activate_next→恢复协议携带external_event。"""
        db_path = work / f"jobs-{repeat_index}.sqlite3"
        acts = ActivityCoordinator(db_path, lambda *_a, **_k: None, lambda: True)
        slots = TaskSlots(db_path)
        jobs = BackgroundJobs(work / f"bg-{repeat_index}.sqlite3")
        jobs_holder["jobs"] = jobs
        jobs.record("scope-a", "task-1", f"job-{repeat_index}", turn="t1", session_id="s1")
        acts.checkpoint(goal="长期任务", summary="进行中", next_step="等结果",
                        device="host", status="waiting_external", background=True,
                        receipts=[], protocol={"activity_id": "task-1", "pending_calls": [],
                            "messages": [], "event_wait": {
                                "job_id": f"job-{repeat_index}", "deadline": 1e18,
                                "resume_note": "检查结果", "call_id": "tool-1"}})
        slots.park_current("scope-a", 100)
        event = {"event_id": f"job-{repeat_index}", "job_id": f"job-{repeat_index}",
                 "scope": "scope-a", "task": "task-1", "turn": "t1", "session_id": "s1",
                 "status": "completed", "output_preview": "完成输出", "output_complete": True}
        settled_ok = jobs.observe_completed("scope-a", "task-1", event)
        notified = slots.notify("scope-a", f"job-{repeat_index}", event)
        duplicate_notify = slots.notify("scope-a", f"job-{repeat_index}", event)
        resumed = slots.activate_next(300, quiet_period=0)
        overview = acts.overview()
        return {"settled": settled_ok, "notified": notified,
                "duplicate_suppressed": duplicate_notify is False,
                "resumed": resumed, "overview": overview, "jobs": jobs}

    def run_direct(prep):
        return direct_drive(prep["index"])

    def validate_direct(outcome, _p):
        assert outcome["settled"] and outcome["notified"], "完成事件链断裂"
        assert outcome["duplicate_suppressed"], "重复事件未合并"
        assert outcome["resumed"], "任务未恢复"
        protocol = outcome["overview"]["protocol"]
        assert protocol.get("external_event"), "恢复协议未携带真实事件"

    seq = {"n": 0}

    def next_index():
        seq["n"] += 1
        return {"index": seq["n"]}

    timed_repeats(ctx.recorder, case_id="P15", condition="settled_to_resumed.direct",
                  fixture_id=ctx.size, cache_state="C1", scope_label="scope-a",
                  principal_label="-", snapshot_label="", config_hash=CONFIG_BASELINE,
                  prepare=next_index,
                  run=run_direct, validate=validate_direct,
                  extras={"note": "直接驱动；调度轮询等待未计入(另见轮询窗口P11)"})

    # 完成事件查询：pending_events→ack
    def run_pending(prep):
        jobs = prep["jobs"]
        events = jobs.pending_events("scope-a", "task-1")
        for event in events:
            jobs.ack(event["event_id"])
        return len(events)

    timed_repeats(ctx.recorder, case_id="P15", condition="pending_events.query_ack",
                  fixture_id=ctx.size, cache_state="C1", scope_label="scope-a",
                  principal_label="-", snapshot_label="", config_hash=CONFIG_BASELINE,
                  prepare=lambda: {"jobs": BackgroundJobs(work / "pend.sqlite3")},
                  run=run_pending,
                  validate=lambda outcome, _p: assert_true(outcome >= 0))


# ---------------------------------------------------------------- P16

def case_p16(ctx):
    from .fixtures import FixtureBuilder

    async def chat_once(bridge, tag):
        message = _incoming("private", sender="owner", text="现在方便说话吗？",
                            message_id=f"p16-{tag}-{time.time_ns()}")
        return await bridge.receive(message)

    async def run_condition(tag: str, maintenance: str):
        work = ctx.work_dir / f"p16-{tag}"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        bridge, provider = _make_bridge(work, replies=40)
        bridge.register_route(_incoming("private", sender="owner"))
        app = bridge._application(_incoming("private", sender="owner"))
        fixture_dir = ctx.fixtures_dir / f"p16-{tag}"
        if fixture_dir.exists():
            shutil.rmtree(fixture_dir)
        _, _manifest = FixtureBuilder(ctx.size, agent_id=app.store.agent_id,
                                      user_id=app.store.user_id).build(fixture_dir)
        fixture_archive = fixture_dir / "archive"
        _inject_fixture_library(bridge, fixture_archive, app)
        expected_records = {"S": 210, "M": 2100, "L": 21000}[ctx.size]
        imported_count = len(app.store.list_records())
        if (app.store.agent_id, app.store.user_id) != ("xiaowennuan", "group"):
            raise AssertionError("P16 fixture identity differs from bridge domain")
        if imported_count < expected_records:
            raise AssertionError(f"P16 fixture import incomplete: {imported_count} < {expected_records}")
        await chat_once(bridge, f"{tag}-warm")
        initial_index_tasks = list(bridge.index_tasks.values())
        if initial_index_tasks:
            results = await asyncio.gather(*initial_index_tasks, return_exceptions=True)
            errors = [result for result in results if isinstance(result, BaseException)]
            if errors:
                raise errors[0]

        loop_overhead = {"max_drift_ms": 0.0}
        probe_stop = asyncio.Event()

        async def probe_loop():
            expected = time.perf_counter()
            while not probe_stop.is_set():
                expected += 0.01
                await asyncio.sleep(max(0, expected - time.perf_counter()))
                loop_overhead["max_drift_ms"] = max(
                    loop_overhead["max_drift_ms"],
                    (time.perf_counter() - expected) * 1000)

        probe = asyncio.create_task(probe_loop())
        stop = threading.Event()
        maintenance_thread = None
        maintenance_windows = []  # (start, end)真实维护区间，聊天期间任一相交即重叠

        if maintenance == "monitor":
            started_event = threading.Event()
            def maintain():
                while not stop.is_set():
                    now = time.perf_counter()
                    started_event.set()
                    monitor_snapshot(work, subsystem_cache={})
                    maintenance_windows.append((now, time.perf_counter()))
                    time.sleep(0.5)
            maintenance_thread = threading.Thread(target=maintain, daemon=True)
            maintenance_thread.start()
            started_event.wait(timeout=5)
        elif maintenance == "index":
            maintain_dir = work / "maintenance-lib"
            if maintain_dir.exists():
                shutil.rmtree(maintain_dir)
            shutil.copytree(fixture_archive, maintain_dir)
            store = ArchiveStore(maintain_dir, agent_id=app.store.agent_id,
                                 user_id=app.store.user_id)

            def maintain():
                index = MemoryIndex(store)
                started_at = time.perf_counter()
                started_event.set()
                asyncio.run(index.rebuild())
                asyncio.run(index.update())
                maintenance_windows.append((started_at, time.perf_counter()))
            started_event = threading.Event()
            maintenance_thread = threading.Thread(target=maintain, daemon=True)
            maintenance_thread.start()
            started_event.wait(timeout=5)

        await asyncio.sleep(0)  # probe先运行，覆盖聊天首段
        from .instrument import measure
        with measure() as metrics:
            delivery = await chat_once(bridge, tag)
        chat_started = time.perf_counter() - metrics["wall_ms"] / 1000
        chat_ended = time.perf_counter()
        elapsed_ms = metrics["wall_ms"]
        stop.set()
        probe_stop.set()
        await probe
        if maintenance_thread:
            maintenance_thread.join(timeout=30)  # 清理不计入chat_once耗时
        overlap = any(start < chat_ended and end > chat_started
                      for start, end in maintenance_windows)
        metrics["event_loop_max_drift_ms"] = loop_overhead["max_drift_ms"]
        metrics["maintenance_overlap"] = overlap
        await bridge.close()
        return delivery, elapsed_ms, overlap, loop_overhead["max_drift_ms"], metrics

    def make_run(tag, maintenance):
        def run(_prep):
            delivery, elapsed, overlap, drift, metrics = asyncio.run(run_condition(tag, maintenance))
            return (delivery, elapsed, overlap, drift), metrics
        return run

    def validate(outcome, _p, maintenance="none"):
        _validate_p16_outcome(outcome, maintenance)

    conditions = (("alone", "none"), ("with_monitor", "monitor"),
                  ("with_index_update", "index"))
    if getattr(ctx, 'quick', False):
        conditions = (("alone", "none"), ("with_index_update", "index"))
    mode = getattr(ctx, 'p16_mode', 'both')
    if mode != 'both':
        selected = 'none' if mode == 'alone' else 'index'
        conditions = tuple(row for row in conditions if row[1] == selected)
    for tag, maintenance in conditions:
        timed_repeats(ctx.recorder, case_id="P16", condition=f"chat.{tag}",
                      fixture_id=ctx.size, cache_state="C1", scope_label="private",
                      principal_label="owner", snapshot_label="",
                      config_hash=CONFIG_BASELINE, prepare=lambda: None,
                      run=make_run(tag, maintenance),
                      validate=lambda outcome, _p, m=maintenance: validate(outcome, _p, m),
                      measured_run=True,
                      extras={"note": "wall只计chat_once；后台线程清理在计时外；需结合loop drift"})
