from pathlib import Path

import pytest

from warmagent.background_jobs import BackgroundJobs
from warmagent.direct_harness import DirectHarness, _NodeToolHost


class FakeNodeHost(_NodeToolHost):
    def __init__(self):
        super().__init__({})

    async def start(self):
        return [{"name": "pwsh", "description": "PowerShell",
                 "parameters": {"type": "object", "properties": {
                     "run_in_background": {"type": "boolean"}},
                     "required": ["run_in_background"], "additionalProperties": False}}]

    async def execute(self, name, args, call_id, **kwargs):
        return {"value": {"kind": "background", "jobId": "job-1"},
                "content": [{"type": "text", "text": "started background job job-1"}],
                "isError": False}

    async def close(self):
        pass


def test_persisted_job_completion_is_scoped_deduplicated_and_acknowledged(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    store = BackgroundJobs(path)
    store.record("scope-a", "task-a", "job-1", turn="turn-a", session_id="session-a")
    event = {"event_id": "job-1", "job_id": "job-1", "scope": "scope-a", "task": "task-a",
             "turn": "turn-a", "session_id": "session-a", "status": "completed",
             "output_preview": "ok", "output_ref": {"spill_paths": ["full-output.log"]}}
    assert store.observe_completed("scope-b", "task-a", event) is False
    assert store.pending_events("scope-a", "task-a") == []
    assert store.observe_completed("scope-a", "task-a", event) is True
    assert store.observe_completed("scope-a", "task-a", event) is False
    pending = store.pending_events("scope-a", "task-a")
    assert pending == [event]
    assert pending[0]["output_ref"]["spill_paths"] == ["full-output.log"]
    assert store.ack("job-1") is True
    assert store.ack("job-1") is False
    assert store.pending_events("scope-a", "task-a") == []


@pytest.mark.asyncio
async def test_direct_harness_reports_one_validated_same_session_completion(tmp_path):
    events = []
    harness = DirectHarness({"enabled": True, "owner_id": "owner-1",
        "permission_profile": "full"}, tmp_path, "owner-1", ["wechat|private-1"],
        "live", executor=FakeNodeHost(), on_job_event=events.append)
    handlers = await harness.handlers({"author_kind": "human", "channel_source": {
        "sender_id": "owner-1", "platform_id": "wechat", "bot_id": "bot-1",
        "group_id": "private-1", "message_id": "message-1"}}, context={
            "scope": "scope-a", "activity_id": "task-a", "turn_id": "turn-a"})
    result = await handlers["harness__pwsh"][1].execute_call(
        "call-1", {"run_in_background": True})
    assert result["result"]["value"]["jobId"] == "job-1"
    event = {"event_id": "job-1", "job_id": "job-1", "scope": "scope-a", "task": "task-a",
        "turn": "turn-a", "session_id": DirectHarness._session_identity(
            {"scope": "scope-a"}, "task-a", "turn-a")[0], "status": "completed",
        "cause": "producer", "output_preview": "done", "output_ref": {"spill_paths": []}}
    harness._handle_job_event(event)
    harness._handle_job_event(event)
    assert events == [event]
    assert harness.background_jobs.pending_events("scope-a", "task-a") == [event]
    forged = {**event, "job_id": "job-2", "event_id": "job-2", "scope": "scope-b"}
    harness._handle_job_event(forged)
    assert len(events) == 1
    await harness.close()


def test_host_restart_marks_process_local_running_jobs_unknown(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    first = BackgroundJobs(path)
    first.record("scope-a", "task-a", "job-1", turn="turn-a", session_id="session-a")
    restarted = BackgroundJobs(path)
    with __import__("sqlite3").connect(path) as db:
        status = db.execute("SELECT status FROM background_jobs WHERE job_id='job-1'").fetchone()[0]
    assert status == "unknown"
    pending = restarted.pending_events("scope-a", "task-a")
    assert len(pending) == 1 and pending[0]["event_id"] == "hostlost:job-1"
    assert "不要重复执行" in pending[0]["note"]


def test_node_host_uses_sdk_settled_subscription_and_non_consuming_output_ref():
    source = Path("src/warmagent/harness/tool-host.mjs").read_text(encoding="utf-8")
    assert "ctx.jobs.events.subscribe({ owners: 'scope' }" in source
    assert "event.type !== 'settled'" in source
    assert "ctx.jobs.readAt(event.job.id, 0, metadata.session_id)" in source
    assert "kind: 'job_event'" in source
    assert "result.value?.kind === 'background'" in source
