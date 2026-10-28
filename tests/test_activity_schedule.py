import asyncio
from datetime import datetime
from pathlib import Path
import tomllib
from zoneinfo import ZoneInfo

import pytest

from warmagent.activities import (ActivityCoordinator, reset_step_revision_context,
                                  step_revision_context)


def available():
    return {"online": True, "authorized": True, "budget_available": True, "busy": False}


def make(path, runner, clock=lambda: 0):
    return ActivityCoordinator(path, runner, available, interval=600, clock=clock)


def beijing(value):
    return datetime.fromisoformat('2026-10-27T' + value).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


@pytest.mark.asyncio
@pytest.mark.parametrize('clock_time,blocked', [
    ('08:59:59', False), ('09:00:00', True), ('11:59:59', True), ('12:00:00', False),
    ('13:59:59', False), ('14:00:00', True), ('16:59:59', True), ('17:00:00', False)])
async def test_cost_quiet_hours_boundaries_skip_model_and_availability(tmp_path, clock_time, blocked):
    now = beijing(clock_time)
    calls = []
    async def run(*args, **kwargs):
        calls.append('model')
        return {'action': 'rest'}
    def ready():
        calls.append('availability')
        return available()
    coord = ActivityCoordinator(tmp_path / 'activity.sqlite', run, ready, interval=0, clock=lambda: now,
        cost_quiet_hours=['09:00-12:00', '14:00-17:00'])
    result = await coord.tick(now)
    assert result['ran'] is not blocked
    if blocked:
        assert result['reason'] == 'cost_quiet_hours' and not calls
        assert coord.overview() is None
    else:
        assert calls == ['availability', 'model']


@pytest.mark.asyncio
async def test_cost_window_preserves_due_wakeup_then_runs_once_at_noon(tmp_path):
    calls = []
    async def run(*args, **kwargs):
        calls.append(kwargs)
        return {'action': 'rest'}
    coord = ActivityCoordinator(tmp_path / 'activity.sqlite', run, available, interval=1800,
        clock=lambda: beijing('08:59:00'), cost_quiet_hours=['09:00-12:00', '14:00-17:00'])
    coord.checkpoint(goal='继续学习', protocol={'activity_id': 'fixture'}, status='resting')
    wake = coord.schedule_wakeup(delay_seconds=120, resume_note='自动预约', call_id='fixture')
    before = coord._row()
    assert (await coord.tick(beijing('09:01:00')))['reason'] == 'cost_quiet_hours'
    assert coord._row() == before
    assert coord.overview()['next_wakeup']['wake_id'] == wake['wake_id']
    view = coord.schedule_view(beijing('09:01:00'))
    assert view['next_check_at'] == beijing('12:00:00')
    assert view['reason'] == 'cost_quiet_hours'
    assert (await coord.tick(beijing('12:00:00')))['ran']
    assert len(calls) == 1 and coord.overview()['next_wakeup'] is None


def test_schedule_projects_future_quiet_window_and_keeps_manual_pause(tmp_path):
    coord = ActivityCoordinator(tmp_path / 'activity.sqlite', lambda *a, **k: None, available,
        interval=1800, clock=lambda: beijing('08:50:00'), cost_quiet_hours=['09:00-12:00', '14:00-17:00'])
    assert coord.schedule_view(beijing('08:50:00'))['next_check_at'] == beijing('12:00:00')
    coord.checkpoint(goal='活动', status='active')
    assert coord.schedule_view(beijing('14:30:00'))['next_check_at'] == beijing('17:00:00')
    coord.pause()
    view = coord.schedule_view(beijing('14:30:00'))
    assert view['next_check_at'] is None and view['reason'] == 'paused'
    assert view['cost_quiet_timezone'] == 'Asia/Shanghai'


@pytest.mark.asyncio
async def test_thirty_minute_default_keeps_explicit_wakeup_and_chat_resume(tmp_path):
    settings = tomllib.loads((Path(__file__).resolve().parents[1] /
        'config/bridge.example.toml').read_text(encoding='utf-8'))['bridge']['autonomy']
    assert settings['scan_interval_seconds'] == 1800
    calls = []
    async def runner(activity, **kwargs):
        calls.append(activity)
        return {'action': 'create' if activity is None else 'rest', 'goal': 'task', 'summary': 'fixture'}
    coord = ActivityCoordinator(tmp_path / 'slow.sqlite', runner, available,
        interval=settings['scan_interval_seconds'], clock=lambda: 0)
    assert not (await coord.tick(600))['ran']
    assert not (await coord.tick(1799))['ran']
    assert (await coord.tick(1800))['ran']
    assert (await coord.tick(1801))['ran']
    assert coord.overview()['rest_until'] == 3601
    assert not (await coord.tick(2401))['ran']
    coord.checkpoint(protocol={'activity_id': 'scheduled'})
    wake = coord.schedule_wakeup(delay_seconds=60, resume_note='模型预约', call_id='wake', now=2401)
    assert wake['due_at'] == 2461
    assert not (await coord.tick(2460))['ran']
    assert (await coord.tick(2461))['ran']
    coord.checkpoint(status='active')
    coord.on_owner_message(now=2500)
    assert not (await coord.tick(2679))['ran']
    assert (await coord.tick(2680))['ran']
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_wakeup_persists_across_restart_and_fires_once(tmp_path):
    path = tmp_path / "activity.sqlite"
    calls = []

    async def runner(activity, **kwargs):
        calls.append(activity)
        return {"action": "rest"}

    coord = make(path, runner)
    coord.checkpoint(goal="task", protocol={"activity_id": "task-1"}, status="resting")
    wake = coord.schedule_wakeup(delay_seconds=10, resume_note="继续读", call_id="c1", now=0)
    assert coord.overview()["next_wakeup"]["wake_id"] == wake["wake_id"]
    restarted = make(path, runner)
    assert restarted.overview()["next_wakeup"]["wake_id"] == wake["wake_id"]
    assert (await restarted.tick(9))["reason"] == "rest"
    assert len(calls) == 0
    await restarted.tick(10)
    assert len(calls) == 1
    context = restarted.overview()["protocol"]["wakeup_context"]
    assert context == {"wake_id": wake["wake_id"], "resume_note": "继续读", "due_at": 10.0}
    assert restarted.overview()["next_wakeup"] is None
    await restarted.tick(11)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cancelled_wakeup_does_not_revive_resting_task(tmp_path):
    path = tmp_path / "activity.sqlite"
    async def runner(activity, **kwargs):
        return {"action": "rest"}
    coord = make(path, runner)
    coord.checkpoint(goal="task", protocol={"activity_id": "task-1"}, status="resting")
    wake = coord.schedule_wakeup(delay_seconds=1, resume_note="go", call_id="c1", now=0)
    assert coord.cancel_wakeup(wake_id=wake["wake_id"])["cancelled"] is True
    result = await coord.tick(1)
    assert result["reason"] == "rest"
    assert result["activity"]["status"] == "resting"


@pytest.mark.asyncio
async def test_late_rest_result_cannot_overwrite_pause_or_controlled_fields(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    async def runner(activity, **kwargs):
        started.set()
        await release.wait()
        return {"action": "rest", "goal": "stale goal", "summary": "stale", "next_step": "stale"}
    coord = make(tmp_path / "activity.sqlite", runner)
    coord.checkpoint(goal="current", summary="before", next_step="before", protocol={"activity_id": "task-1"}, status="active")
    task = asyncio.create_task(coord.tick(600))
    await started.wait()
    coord.pause(601)
    release.set()
    await task
    activity = coord.overview()
    assert activity["status"] == "paused"
    assert activity["goal"] == "current"
    assert activity["summary"] == "before"
    assert activity["next_step"] == "before"


def test_focus_defaults_expiry_and_urgent_override(tmp_path):
    coord = make(tmp_path / "activity.sqlite", lambda *a, **k: None)
    coord.checkpoint(goal="task", status="active")
    assert coord.overview()["focus_policy"] == "chat_first"
    assert coord.should_defer_owner(now=0) is False
    coord.set_focus("focus_first", duration_seconds=30, now=100, reason="写作")
    assert coord.should_defer_owner(now=129) is True
    assert coord.should_defer_owner(urgent=True, now=129) is False
    assert coord.should_defer_owner(now=130) is False


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), 0, 3601])
def test_focus_rejects_invalid_or_unbounded_duration(tmp_path, duration):
    coord = make(tmp_path / "bad-focus.sqlite", lambda *a, **k: None)
    with pytest.raises(ValueError, match="invalid_focus_duration"):
        coord.set_focus("focus_first", duration_seconds=duration, now=0)


def test_focus_limits_duration_and_reason(tmp_path):
    coord = make(tmp_path / "focus-limits.sqlite", lambda *a, **k: None)
    coord.checkpoint(goal="task", status="active")
    coord.set_focus("adaptive", duration_seconds=3600, reason="x" * 500, now=100)
    assert coord.overview()["focus_until"] == 3700
    with pytest.raises(ValueError, match="invalid_focus_reason"):
        coord.set_focus("chat_first", reason="x" * 501, now=100)
    with pytest.raises(ValueError, match="invalid_focus_reason"):
        coord.set_focus("adaptive", reason=None, now=100)


def test_adaptive_focus_only_defers_during_execution_boundaries(tmp_path):
    coord = make(tmp_path / "adaptive.sqlite", lambda *a, **k: None)
    coord.checkpoint(goal="task", status="active", protocol={"activity_id": "t1"})
    coord.set_focus("adaptive", duration_seconds=300, now=0)
    assert coord.should_defer_owner(now=100) is False  # active and waiting
    coord.checkpoint(protocol={"activity_id": "t1", "pending_calls": [{"id": "c1"}]})
    assert coord.should_defer_owner(now=100) is True
    coord.checkpoint(status="running", protocol={"activity_id": "t1", "pending_calls": []})
    assert coord.should_defer_owner(now=100) is True
    coord.checkpoint(status="resting", protocol={"activity_id": "t1", "pending_calls": [{"id": "c2"}]})
    assert coord.should_defer_owner(now=100) is False
    coord.set_focus("focus_first", duration_seconds=300, now=0)
    assert coord.should_defer_owner(now=100) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["pause", "stop"])
async def test_control_during_availability_prevents_runner_claim(tmp_path, control):
    available_started, release = asyncio.Event(), asyncio.Event()
    runner_calls = []

    async def slow_available():
        available_started.set()
        await release.wait()
        return available()

    async def runner(activity, **kwargs):
        runner_calls.append(activity)
        return {"summary": "ran"}

    coord = ActivityCoordinator(tmp_path / f"{control}.sqlite", runner, slow_available)
    coord.checkpoint(goal="task", protocol={"activity_id": "task"}, status="active")
    pending = asyncio.create_task(coord.tick(600))
    await available_started.wait()
    getattr(coord, control)()
    release.set()
    result = await pending
    assert result["reason"] == "control_changed"
    assert runner_calls == []
    assert coord.overview()["status"] == ("paused" if control == "pause" else "cancelled")


@pytest.mark.asyncio
async def test_runner_exception_after_owner_pause_does_not_mark_unknown(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    async def runner(activity, **kwargs):
        started.set()
        await release.wait()
        raise RuntimeError("late error")

    coord = make(tmp_path / "error.sqlite", runner)
    coord.checkpoint(goal="task", status="active")
    pending = asyncio.create_task(coord.tick(600))
    await started.wait()
    coord.pause(601)
    release.set()
    result = await pending
    assert result["reason"] == "control_changed"
    assert coord.overview()["status"] == "paused"


@pytest.mark.asyncio
async def test_late_unknown_paused_by_owner_reconciles_after_quiet_period(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    runner_calls = []

    async def runner(activity, *, prompt, reconcile_only):
        runner_calls.append((activity, reconcile_only))
        if len(runner_calls) == 1:
            started.set()
            await release.wait()
            return {"unknown": True, "protocol": {
                "activity_id": "task-unknown",
                "uncertain_calls": [{"call_id": "effect-1"}],
            }}
        return {"protocol": {"activity_id": "task-unknown", "uncertain_calls": []}}

    coord = ActivityCoordinator(tmp_path / "late-unknown.sqlite", runner, available,
                                quiet_period=180)
    coord.checkpoint(goal="task", protocol={"activity_id": "task-unknown"}, status="active")
    pending = asyncio.create_task(coord.tick(600))
    await started.wait()
    coord.on_owner_message(601)
    release.set()
    await pending
    paused = coord.overview()
    assert paused["status"] == "paused"
    assert paused["protocol"]["uncertain_calls"] == [{"call_id": "effect-1"}]

    assert (await coord.tick(780))["reason"] == "paused"
    resumed = await coord.tick(781)
    assert resumed["ran"] is True
    assert runner_calls[1][1] is True
    assert runner_calls[1][0]["outcome_unknown"] is True
    assert runner_calls[1][0]["protocol"]["uncertain_calls"] == [{"call_id": "effect-1"}]


def test_stale_runner_focus_cannot_rebase_context_revision(tmp_path):
    coord = make(tmp_path / "focus.sqlite", lambda *a, **k: None)
    coord.checkpoint(goal="task", status="active")
    old_revision = coord.overview()["control_revision"]
    token = step_revision_context(old_revision)
    try:
        coord.pause(1)
        with pytest.raises(ValueError, match="stale_activity_control"):
            coord.set_focus("focus_first", now=2)
    finally:
        reset_step_revision_context(token)
    activity = coord.overview()
    assert activity["status"] == "paused"
    assert activity["focus_policy"] == "chat_first"


def test_due_wakeup_rechecks_current_control_snapshot(tmp_path):
    coord = make(tmp_path / "due-cas.sqlite", lambda *a, **k: None)
    coord.checkpoint(goal="task", protocol={"activity_id": "task"}, status="resting")
    coord.schedule_wakeup(delay_seconds=1, resume_note="resume", call_id="wake-1", now=0)
    stale_snapshot = coord._row()
    coord.pause(0.5)
    assert coord._due_wakeup(stale_snapshot, 1) is None
    assert coord.overview()["status"] == "paused"
    assert coord.overview()["protocol"].get("wakeup_context") is None
    assert coord._next_wakeup() is None  # revision mismatch marks the old plan stale


def test_manual_checkpoint_cannot_reactivate_paused_or_terminal_activity(tmp_path):
    coord = make(tmp_path / "checkpoint-guard.sqlite", lambda *a, **k: None)
    coord.checkpoint(goal="task", status="active")
    coord.pause(1)
    coord.checkpoint(status="active")
    assert coord.overview()["status"] == "paused"
    coord.stop(2)
    coord.checkpoint(status="active")
    assert coord.overview()["status"] == "cancelled"

