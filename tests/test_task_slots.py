import json
import sqlite3

import pytest

from warmagent.activities import ActivityCoordinator
from warmagent.background_jobs import BackgroundJobs
from warmagent.task_slots import TaskSlots


def coordinator(path):
    return ActivityCoordinator(path, lambda *_args, **_kwargs: None, lambda: True)


def park(slots, activity, *, scope="scope-a", now=100, deadline=200, job="job-1"):
    activity.checkpoint(goal="old goal", summary="saved summary", next_step="continue",
        device="host", status="waiting_external", background=True, receipts=[{"call_id": "old"}],
        protocol={"activity_id": "task-1", "pending_calls": [], "messages": [{"role": "user"}],
            "event_wait": {"job_id": job, "deadline": deadline, "resume_note": "resume note", "call_id": "tool-1"}})
    return slots.park_current(scope, now)


def test_park_event_resume_preserves_owner_controls_and_waits_for_current_slot(tmp_path):
    path = tmp_path / "activities.sqlite3"
    activity = coordinator(path)
    activity.set_focus("focus_first", now=10)
    activity.on_chat_delivered(now=50)
    with sqlite3.connect(path) as db:
        before = db.execute("SELECT control_revision,focus_policy,last_chat FROM idle_activity WHERE id=1").fetchone()
    slots = TaskSlots(path)
    assert park(slots, activity)
    assert slots.park_current("scope-a", now=101) is True
    with sqlite3.connect(path) as db:
        empty = db.execute("SELECT goal,protocol,receipts,status,last_check,control_revision,"
                           "focus_policy,last_chat FROM idle_activity WHERE id=1").fetchone()
    assert empty[0] == "" and json.loads(empty[1]) == {} and json.loads(empty[2]) == []
    assert empty[3] == "empty" and empty[4] == 100
    assert empty[5] == before[0]
    assert empty[6] == before[1]
    assert empty[7] == before[2]

    event = {"event_id": "job-1", "job_id": "job-1", "scope": "scope-a",
             "task": "task-1", "status": "completed", "output_preview": "finished"}
    assert slots.notify("scope-b", "job-1", event) is False
    assert slots.notify("scope-a", "job-1", event) is True
    assert slots.notify("scope-a", "job-1", event) is False
    activity.checkpoint(goal="new goal", summary="new task", protocol={"activity_id": "task-2"},
                        status="active")
    assert slots.activate_next(300, quiet_period=0) is False
    assert activity.overview()["goal"] == "new goal"
    activity.checkpoint(status="completed")
    revision = activity.overview()["control_revision"]
    assert slots.activate_next(301, quiet_period=0) is True
    resumed = activity.overview()
    assert resumed["goal"] == "old goal" and resumed["summary"] == "saved summary"
    assert resumed["control_revision"] == revision
    assert resumed["protocol"]["activity_id"] == "task-1"
    assert "event_wait" not in resumed["protocol"]
    assert resumed["protocol"]["external_event"] == event
    assert slots.list("scope-a")[0]["status"] == "active"


@pytest.mark.parametrize("notify_first", [True, False])
def test_timeout_and_completion_race_wins_once(tmp_path, notify_first):
    path = tmp_path / "activities.sqlite3"
    activity = coordinator(path)
    slots = TaskSlots(path)
    assert park(slots, activity)
    event = {"event_id": "job-1", "job_id": "job-1", "scope": "scope-a",
             "task": "task-1", "status": "completed"}
    if notify_first:
        assert slots.notify("scope-a", "job-1", event)
        assert slots.expire(200) == 0
        winning = event
    else:
        assert slots.expire(200) == 1
        assert slots.notify("scope-a", "job-1", event) is False
        winning = {"type": "timeout", "job_id": "job-1", "deadline": 200.0, "occurred_at": 200.0}
    assert slots.expire(201) == 0
    slot = slots.list("scope-a")[0]
    assert slot["status"] == "ready"
    with sqlite3.connect(path) as db:
        stored = db.execute("SELECT event FROM task_slots WHERE task_id='task-1'").fetchone()[0]
    assert json.loads(stored) == winning


def test_pause_records_completion_cancel_does_not_reactivate_and_scope_is_fenced(tmp_path):
    path = tmp_path / "activities.sqlite3"
    activity = coordinator(path)
    slots = TaskSlots(path)
    assert park(slots, activity)
    revision = slots.list("scope-a")[0]["revision"]
    paused = slots.control("task-1", "pause", expected_revision=revision, now=101)
    assert paused["accepted"] and paused["status"] == "paused"
    event = {"event_id": "job-1", "job_id": "job-1", "scope": "scope-a",
             "task": "task-1", "status": "completed"}
    assert slots.notify("scope-a", "job-1", event) is True
    assert slots.list("scope-a")[0]["status"] == "paused"
    stale = slots.control("task-1", "resume", version=paused["version"], now=102)
    assert stale["accepted"] is False and stale["reason"] == "stale_revision"
    current_version = slots.list("scope-a")[0]["version"]
    resumed = slots.control("task-1", "resume", version=current_version, now=102)
    assert resumed["accepted"] and resumed["status"] == "ready"
    assert slots.activate_next(103, quiet_period=0)
    activity.checkpoint(status="completed")
    revision = slots.list("scope-a")[0]["revision"]
    cancelled = slots.control("task-1", "cancel", revision, now=104)
    assert cancelled["accepted"] and cancelled["status"] == "cancelled"
    assert slots.control("task-1", "resume", cancelled["revision"], now=105)["accepted"] is False
    assert slots.activate_next(200, quiet_period=0) is False


def test_background_job_hostloss_is_deduplicated_and_metadata_never_contains_output(tmp_path):
    path = tmp_path / "background.sqlite3"
    jobs = BackgroundJobs(path)
    jobs.record("scope-a", "task-a", "job-a", turn="turn-a", session_id="session-a")
    restarted = BackgroundJobs(path)
    assert restarted.get("scope-b", "job-a") is None
    assert restarted.get("scope-a", "job-a")["status"] == "unknown"
    events = restarted.list_pending()
    assert len(events) == 1 and events[0]["event_id"] == "hostlost:job-a"
    assert "不要重复执行" in events[0]["note"]
    assert restarted.list_pending() == events
    assert restarted.list_jobs("scope-a", "task-a")[0]["status"] == "unknown"
    assert "output_preview" not in restarted.list_jobs("scope-a", "task-a")[0]


def test_background_job_get_and_page_scope_and_verified_spill_path(tmp_path):
    path = tmp_path / "background.sqlite3"
    spill = tmp_path / "output.txt"
    spill.write_text("A" * 7000, encoding="utf-8")
    jobs = BackgroundJobs(path)
    jobs.record("scope-a", "task-a", "job-a", turn="turn-a", session_id="session-a")
    event = {"event_id": "job-a", "job_id": "job-a", "scope": "scope-a",
             "task": "task-a", "turn": "turn-a", "session_id": "session-a",
             "status": "completed", "output_preview": "preview",
             "output_ref": {"spill_paths": [str(spill)]}}
    assert jobs.observe_completed("scope-a", "task-a", event)
    assert jobs.get("scope-b", "job-a") is None
    assert jobs.get("scope-a", "job-a")["status"] == "completed"
    first = jobs.page("scope-a", "job-a", 0, 4000)
    second = jobs.page("scope-a", "job-a", first["next_offset"], 4000)
    assert first["text"] == "A" * 4000 and first["complete"] is True
    assert second["text"] == "A" * 3000 and second["next_offset"] is None
    assert jobs.page("scope-b", "job-a") is None
    with pytest.raises(ValueError):
        jobs.page("scope-a", "job-a", 0, 4001)


def test_sync_current_updates_only_matching_terminal_task_slot(tmp_path):
    path = tmp_path / "activities.sqlite3"
    activity = coordinator(path)
    slots = TaskSlots(path)
    activity.checkpoint(goal="current", protocol={"activity_id": "task-current"}, status="active")
    with sqlite3.connect(path) as db:
        db.execute("""INSERT INTO task_slots(task_id,scope,revision,status,filter_job,payload,created,updated)
            VALUES('task-current','scope-a',2,'active','job-current','{}',1,1)""")
        db.execute("""INSERT INTO task_slots(task_id,scope,revision,status,filter_job,payload,created,updated)
            VALUES('task-waiting','scope-a',1,'waiting','job-waiting','{}',2,2)""")
    activity.checkpoint(status="completed")
    assert slots.sync_current(now=300) is True
    assert slots.sync_current(now=301) is False
    rows = {row[0]: row[1] for row in sqlite3.connect(path).execute(
        "SELECT task_id,status FROM task_slots")}
    assert rows == {"task-current": "completed", "task-waiting": "waiting"}


def test_hostlost_event_restores_checkpoint_as_unknown_for_reconciliation(tmp_path):
    path = tmp_path / "activities.sqlite3"
    activity = coordinator(path)
    slots = TaskSlots(path)
    assert park(slots, activity)
    event = {"event_id": "hostlost:job-1", "job_id": "job-1", "scope": "scope-a",
             "task": "task-1", "status": "unknown", "type": "host_lost",
             "note": "do not repeat"}
    assert slots.notify("scope-a", "job-1", event)
    assert slots.activate_next(300, quiet_period=0)
    current = activity._row()
    assert current["status"] == "unknown"
    assert current["protocol"]["external_event"] == event
    assert "event_wait" not in current["protocol"]
    assert slots.list("scope-a")[0]["status"] == "unknown"
