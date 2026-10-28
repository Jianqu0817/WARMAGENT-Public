import asyncio
import sqlite3

import pytest

from warmagent.activities import ActivityCoordinator


class FakeClock:
    def __init__(self, value=0):
        self.value = value

    def __call__(self):
        return self.value

    def set(self, value):
        self.value = value


def available(**overrides):
    result = {"online": True, "authorized": True, "busy": False,
              "budget_available": True}
    result.update(overrides)
    return result


@pytest.mark.asyncio
async def test_schedule_view_shows_default_scan_rest_and_manual_pause(tmp_path):
    async def runner(activity, **kwargs):
        return {'action': 'rest', 'summary': '休息', 'goal': 'fixture'}
    coordinator = ActivityCoordinator(tmp_path / 'schedule.sqlite3', runner, available, clock=lambda: 0)
    assert coordinator.schedule_view(100)['next_check_at'] == 600
    coordinator.checkpoint(goal='fixture', status='active')
    await coordinator.tick(600)
    view = coordinator.schedule_view(700)
    assert view['reason'] == 'rest' and view['next_check_at'] == 1200
    assert coordinator.overview()['next_wakeup'] is None
    coordinator.pause(now=701)
    assert coordinator.schedule_view(702)['next_check_at'] is None


@pytest.mark.asyncio
async def test_new_activity_waits_600_seconds_then_existing_steps_do_not(tmp_path):
    clock = FakeClock()
    calls = []

    async def runner(activity, *, prompt, reconcile_only):
        calls.append((activity, reconcile_only, prompt))
        if activity is None:
            return {"action": "create", "goal": "读一篇短文", "summary": "找到短文",
                    "next_step": "读第一段", "protocol": {"section": 1}}
        return {"summary": "读到第二段", "next_step": "做笔记"}

    coordinator = ActivityCoordinator(tmp_path / "db.sqlite", runner, available, clock=clock)
    assert (await coordinator.tick(599))["reason"] == "interval"
    assert not calls
    assert (await coordinator.tick(600))["ran"]
    assert coordinator.overview()["goal"] == "读一篇短文"
    assert (await coordinator.tick(601))["ran"]
    assert len(calls) == 2
    assert "每次只完成一个" in calls[0][2]


@pytest.mark.asyncio
async def test_no_model_call_when_unavailable(tmp_path):
    calls = []

    def runner(*args, **kwargs):
        calls.append(args)

    coordinator = ActivityCoordinator(tmp_path / "db.sqlite", runner,
        lambda: available(online=False), clock=FakeClock())
    result = await coordinator.tick(600)
    assert result["reason"] == "unavailable"
    assert calls == []


@pytest.mark.asyncio
async def test_owner_message_pauses_at_callback_boundary_then_quiet_resume_180s(tmp_path):
    clock = FakeClock(0)
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def runner(activity, *, prompt, reconcile_only):
        calls.append(reconcile_only)
        if activity is None:
            return {"action": "create", "goal": "整理资料", "summary": "已列出两篇资料"}
        started.set()
        await release.wait()
        return {"next_step": "比较资料"}

    coordinator = ActivityCoordinator(tmp_path / "db.sqlite", runner, available, clock=clock)
    await coordinator.tick(600)
    clock.set(601)
    running = asyncio.create_task(coordinator.tick())
    await started.wait()
    first = coordinator.on_owner_message(602)
    assert first["activity_summary"] == "已列出两篇资料"
    assert coordinator.should_pause()
    # The activity summary is returned once for the chat to explain.
    assert coordinator.on_owner_message(603)["activity_summary"] == ""
    coordinator.on_chat_delivered(610)
    release.set()
    await running
    assert coordinator.overview()["status"] == "paused"
    assert (await coordinator.tick(789))["reason"] == "paused"
    assert (await coordinator.tick(790))["ran"]
    assert calls == [False, False, False]


@pytest.mark.asyncio
async def test_rest_and_explicit_continue(tmp_path):
    calls = []

    async def runner(activity, *, prompt, reconcile_only):
        calls.append(activity)
        if activity is None:
            return {"action": "create", "goal": "整理书签", "summary": "已整理"}
        if len(calls) == 2:
            return {"action": "rest"}
        return {"summary": "继续整理"}

    coordinator = ActivityCoordinator(tmp_path / "db.sqlite", runner, available, clock=FakeClock())
    await coordinator.tick(600)
    await coordinator.tick(601)
    assert coordinator.overview()["status"] == "resting"
    assert (await coordinator.tick(1000))["reason"] == "rest"
    assert coordinator.claim_resume(10000)["accepted"]
    assert (await coordinator.tick(10000))["ran"]
    assert coordinator.overview()["summary"] == "继续整理"


@pytest.mark.asyncio
async def test_restart_marks_inflight_unknown_and_reconcile_keeps_receipts(tmp_path):
    path = tmp_path / "activity.sqlite3"
    clock = FakeClock(600)
    coordinator = ActivityCoordinator(path, lambda *a, **k: None, available, clock=clock)
    # Simulate an activity checkpointed before a tool call and a process crash.
    coordinator.checkpoint(goal="整理照片", summary="核对中", next_step="检查回执",
                           protocol={"phase": "verify", "cursor": 2},
                           receipts=[{"id": "receipt-1", "state": "completed"}],
                           status="running")
    coordinator.record_receipt({"id": "receipt-1", "state": "completed"})
    coordinator.update_protocol({"phase": "verify", "cursor": 2})
    restarted_calls = []

    async def runner(activity, *, prompt, reconcile_only):
        restarted_calls.append((activity, reconcile_only))
        assert activity["status"] == "unknown"
        assert activity["protocol"] == {"phase": "verify", "cursor": 2}
        return {"status": "running", "summary": "确认旧动作已经完成",
                "receipts": [{"id": "receipt-1", "state": "completed"}]}

    restarted = ActivityCoordinator(path, runner, available, clock=clock)
    assert restarted.overview()["status"] == "unknown"
    assert (await restarted.tick(1200))["reason"] == "unknown"
    assert restarted.claim_resume(1201)["accepted"]
    assert (await restarted.tick(1201))["ran"]
    activity, reconcile_only = restarted_calls[0]
    assert reconcile_only is True
    assert len(restarted.overview()["receipts"]) == 1
    assert restarted.overview()["status"] == "running"


@pytest.mark.asyncio
async def test_background_activity_continues_when_owner_messages(tmp_path):
    async def runner(activity, *, prompt, reconcile_only):
        if activity is None:
            return {"action": "create", "goal": "备份索引", "background": True}
        return {"next_step": "检查下一项"}

    coordinator = ActivityCoordinator(tmp_path / "db.sqlite", runner, available, clock=FakeClock())
    await coordinator.tick(600)
    result = coordinator.on_owner_message(601)
    assert result["background_continues"] is True
    assert result["pause_requested"] is False
    assert (await coordinator.tick(602))["ran"]


@pytest.mark.asyncio
async def test_blocked_status_persists_and_resting_resumes_at_default_deadline(tmp_path):
    clock = FakeClock()
    calls = []

    async def runner(activity, *, prompt, reconcile_only):
        calls.append(activity)
        if activity is None:
            return {"action": "create", "goal": "整理资料"}
        if len(calls) == 2:
            return {"action": "blocked", "summary": "需要人工判断"}
        if len(calls) == 3:
            return {"action": "rest"}
        return {"summary": "恢复整理"}

    coordinator = ActivityCoordinator(tmp_path / "db.sqlite", runner, available,
                                       interval=10, clock=clock)
    await coordinator.tick(600)
    await coordinator.tick(601)
    assert coordinator.overview()["status"] == "blocked"
    assert coordinator.claim_resume(602)["accepted"]
    await coordinator.tick(602)
    assert coordinator.overview()["status"] == "resting"
    assert coordinator.overview()["rest_until"] == 612
    assert (await coordinator.tick(611))["reason"] == "rest"
    assert (await coordinator.tick(612))["ran"]
    assert coordinator.overview()["status"] == "active"


@pytest.mark.asyncio
async def test_continue_with_resting_status_gets_a_bounded_wake_time(tmp_path):
    calls = []

    async def runner(activity, *, prompt, reconcile_only):
        calls.append(activity)
        if activity is None:
            return {"action": "create", "goal": "整理一份资料"}
        if len(calls) == 2:
            return {"action": "continue", "status": "resting", "summary": "先歇一会儿"}
        return {"summary": "接着整理"}

    coordinator = ActivityCoordinator(tmp_path / "db.sqlite", runner, available,
                                       interval=10, clock=FakeClock())
    await coordinator.tick(600)
    await coordinator.tick(601)
    assert coordinator.overview()["status"] == "resting"
    assert coordinator.overview()["rest_until"] == 611
    assert (await coordinator.tick(610))["reason"] == "rest"
    assert (await coordinator.tick(611))["ran"]
    assert coordinator.overview()["summary"] == "接着整理"


@pytest.mark.asyncio
async def test_owner_pause_wins_over_rest_result_returning_from_inflight_step(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    async def runner(activity, *, prompt, reconcile_only):
        started.set()
        await release.wait()
        return {"action": "rest", "status": "resting", "summary": "准备休息"}

    coordinator = ActivityCoordinator(tmp_path / "db.sqlite", runner, available,
                                       interval=10, clock=FakeClock())
    coordinator.checkpoint(goal="进行中的资料整理", status="active")
    step = asyncio.create_task(coordinator.tick(600))
    await started.wait()
    coordinator.on_owner_message(601)
    release.set()
    await step
    activity = coordinator.overview()
    assert activity["status"] == "paused"
    assert activity["pause_requested"] is True
    assert activity["last_chat"] == 601
    assert activity["rest_until"] == 610
    assert (await coordinator.tick(780))["reason"] == "paused"


@pytest.mark.asyncio
async def test_restart_repairs_legacy_resting_deadline_and_preserves_control_states(tmp_path):
    path = tmp_path / "legacy.sqlite"
    clock = FakeClock(100)
    calls = []

    async def runner(activity, *, prompt, reconcile_only):
        calls.append((activity, reconcile_only))
        return {"summary": "恢复后继续"}

    first = ActivityCoordinator(path, runner, available, interval=10, clock=clock)
    first.checkpoint(goal="旧休息任务", status="resting")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE idle_activity SET updated=100,rest_until=NULL WHERE id=1")
    restarted = ActivityCoordinator(path, runner, available, interval=10, clock=clock)
    assert restarted.overview()["rest_until"] == 110
    assert (await restarted.tick(109))["reason"] == "rest"
    assert (await restarted.tick(110))["ran"]
    assert len(calls) == 1
    assert calls[0][0]["status"] == "running"
    assert calls[0][1] is False

    preserved = {}
    for status in ("paused", "unknown", "cancelled"):
        state_path = tmp_path / f"{status}.sqlite"
        state = ActivityCoordinator(state_path, runner, available, clock=clock)
        state.checkpoint(goal=f"{status} task", status="active")
        if status == "paused":
            state.pause()
        elif status == "unknown":
            state.checkpoint(status="unknown")
        else:
            state.stop()
        before = state.overview()
        reopened = ActivityCoordinator(state_path, runner, available, clock=clock)
        after = reopened.overview()
        preserved[status] = after["status"]
        assert after["status"] == before["status"]
        assert after["rest_until"] == before["rest_until"] is None
        if status in {"paused", "unknown"}:
            assert after["pause_requested"] is before["pause_requested"]
    assert preserved == {"paused": "paused", "unknown": "unknown", "cancelled": "cancelled"}


@pytest.mark.asyncio
async def test_outbox_success_clears_notification_pending_and_failure_retries(tmp_path):
    clock = FakeClock()
    steps, deliveries = [], []

    async def runner(activity, *, prompt, reconcile_only):
        steps.append(activity)
        if activity is None:
            return {"action": "create", "goal": "整理资料"}
        protocol = {**activity["protocol"], "notification_pending": {
            "call_id": "finish-1", "text": "整理有进展"}}
        return {"action": "complete", "summary": "已完成", "protocol": protocol,
                "notify_owner": "整理有进展"}

    def notify(text):
        deliveries.append(text)
        if len(deliveries) == 1:
            raise RuntimeError("outbox unavailable")

    coordinator = ActivityCoordinator(tmp_path / "db.sqlite", runner, available,
                                       notify_owner=notify, clock=clock)
    await coordinator.tick(600)
    failed = await coordinator.tick(601)
    assert failed["reason"] == "notification_retry_pending"
    assert coordinator._row()["protocol"]["notification_pending"]["text"] == "整理有进展"
    retried = await coordinator.tick(602)
    assert retried["reason"] == "notification_queued"
    assert "notification_pending" not in coordinator._row()["protocol"]
    assert deliveries == ["整理有进展", "整理有进展"]
    assert len(steps) == 2


@pytest.mark.asyncio
async def test_management_pause_survives_running_step_and_never_chat_auto_resumes(tmp_path):
    clock = FakeClock()
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def runner(activity, *, prompt, reconcile_only):
        calls.append(activity)
        if activity is None:
            return {"action": "create", "goal": "整理文件"}
        started.set()
        await release.wait()
        return {"summary": "边界步骤已结束"}

    coordinator = ActivityCoordinator(tmp_path / "db.sqlite", runner, available, clock=clock)
    await coordinator.tick(600)
    active_step = asyncio.create_task(coordinator.tick(601))
    await started.wait()
    paused = coordinator.pause(602)
    assert paused["status"] == "paused"
    assert paused["pause_requested"] is True
    assert paused["last_chat"] is None
    release.set()
    await active_step
    assert coordinator.overview()["status"] == "paused"
    assert coordinator.overview()["pause_requested"] is True
    assert (await coordinator.tick(10_000))["reason"] == "paused"
    coordinator.on_owner_message(10_001)
    assert coordinator.overview()["last_chat"] is None
    assert (await coordinator.tick(10_181))["reason"] == "paused"
    assert len(calls) == 2
    assert coordinator.claim_resume(10_182)["accepted"]
