import asyncio
import json
from pathlib import Path
import pytest

from warmagent.direct_harness import DirectHarness


SCHEMA = {"type": "object", "properties": {"query": {"type": "string"}},
          "required": ["query"], "additionalProperties": False}


class FakeExecutor:
    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail
        self.starts = 0

    async def start(self):
        self.starts += 1
        return [{"name": "dsh-tool-search", "description": "search",
                 "parameters": SCHEMA}]

    async def execute(self, name, args, call_id):
        self.calls.append((name, args, call_id))
        if self.fail:
            raise RuntimeError("unknown outcome")
        return {"content": [{"type": "text", "text": '{"hits":2}'}], "isError": False}

    async def close(self):
        pass


def settings():
    return {"enabled": True, "owner_id": "owner-1", "permission_profile": "full",
            "autonomy_granted": True}


def test_harness_session_identity_isolated_by_scope_and_activity():
    chat_one = DirectHarness._session_identity(
        {"scope": "owner-scope"}, "conversation", "turn-1")
    chat_two = DirectHarness._session_identity(
        {"scope": "owner-scope"}, "conversation", "turn-2")
    activity_one = DirectHarness._session_identity(
        {"scope": "owner-scope"}, "activity-1", "turn-a")
    activity_one_next = DirectHarness._session_identity(
        {"scope": "owner-scope"}, "activity-1", "turn-b")
    activity_two = DirectHarness._session_identity(
        {"scope": "owner-scope"}, "activity-2", "turn-a")
    chat_other_scope = DirectHarness._session_identity(
        {"scope": "other-scope"}, "conversation", "turn-1")
    assert chat_one == chat_two
    assert chat_one[0] != chat_other_scope[0]
    assert activity_one == activity_one_next
    assert activity_one[0] != activity_two[0]
    assert activity_one[0] != chat_one[0]


def owner_raw():
    return {"author_kind": "human", "channel_source": {
        "sender_id": "owner-1", "platform_id": "wechat", "bot_id": "bot-1",
        "group_id": "private-1", "message_id": "msg-1",
    }}


@pytest.mark.asyncio
async def test_large_receipt_keeps_preview_and_readable_complete_result(tmp_path):
    executor = FakeExecutor()
    async def large(name, args, call_id):
        executor.calls.append(call_id)
        return {'stdout': '内存信息' * 3000}
    executor.execute = large
    harness = DirectHarness(settings(), tmp_path, 'owner-1', ['wechat|private-1'], 'live', executor=executor)
    handlers = await harness.handlers(owner_raw(), context={'turn_id': 'large'})
    invoke = handlers['harness__dsh-tool-search'][1].execute_call
    result = await invoke('large-id', {'query': 'memory'})
    assert result['result_complete'] is False
    assert '内存信息' in result['result_preview']
    assert len(json.dumps(result, ensure_ascii=False).encode()) < 6000
    assert json.loads(Path(result['full_receipt']['file']).read_text(encoding='utf-8'))['result']['stdout'] == '内存信息' * 3000
    assert await invoke('large-id', {'query': 'memory'}) == result
    assert len(executor.calls) == 1
    await harness.close()


@pytest.mark.asyncio
async def test_chat_and_activity_share_one_host_startup(tmp_path):
    executor = FakeExecutor()
    start = executor.start
    async def slow_start():
        await asyncio.sleep(0)
        return await start()
    executor.start = slow_start
    harness = DirectHarness(settings(), tmp_path, 'owner-1', [], 'live', executor=executor)
    catalogs = await asyncio.gather(harness.catalog(), harness.catalog())
    assert catalogs[0] == catalogs[1]
    assert executor.starts == 1
    await harness.close()


@pytest.mark.asyncio
async def test_direct_tool_owner_gate_catalog_and_result(tmp_path):
    executor = FakeExecutor()
    harness = DirectHarness(settings(), tmp_path, "owner-1", ["wechat|private-1"],
                            "live", executor=executor)
    assert list(await harness.catalog()) == ["harness__dsh-tool-search"]
    denied = await harness.handlers({"author_kind": "human", "channel_source": {
        **owner_raw()["channel_source"], "sender_id": "stranger"}},
        context={"activity_id": "chat", "turn_id": "turn-1"})
    assert denied == {}
    result = await harness.execute("harness__dsh-tool-search", {"query": "x"}, {
        "principal": {"author_kind": "human", "channel_source": {
            **owner_raw()["channel_source"], "sender_id": "stranger"}},
        "activity_id": "chat", "turn_id": "turn-1"}, "call-denied")
    assert result == {"error": "computer_owner_required"}
    assert executor.calls == []
    assert executor.starts == 1  # The earlier catalog call was the only host startup.

    handlers = await harness.handlers(owner_raw(), context={
        "activity_id": "chat", "turn_id": "turn-1", "device": "host"})
    result = await handlers["harness__dsh-tool-search"][1].execute_call(
        "call-1", {"query": "memory"})
    assert result["host_receipt_id"] == "call-1"
    assert result["result"]["content"][0]["text"] == '{"hits":2}'
    assert executor.calls == [("dsh-tool-search", {"query": "memory"}, "call-1")]
    await harness.close()


@pytest.mark.asyncio
async def test_duplicate_call_returns_receipt_without_reexecution(tmp_path):
    executor = FakeExecutor()
    harness = DirectHarness(settings(), tmp_path, "owner-1", ["wechat|private-1"],
                            "live", executor=executor)
    handlers = await harness.handlers(owner_raw(), context={
        "activity_id": "activity-1", "turn_id": "turn-2"})
    invoke = handlers["harness__dsh-tool-search"][1].execute_call
    first = await invoke("same-id", {"query": "memory"})
    second = await invoke("same-id", {"query": "memory"})
    assert second == first
    assert len(executor.calls) == 1
    assert await invoke("same-id", {"query": "different"}) == {
        "error": "call_identity_conflict"}
    assert len(executor.calls) == 1
    await harness.close()


@pytest.mark.asyncio
async def test_alias_arguments_are_idempotent_and_persist_original_and_normalized(tmp_path):
    executor = FakeExecutor()
    harness = DirectHarness(settings(), tmp_path, "owner-1", ["wechat|private-1"],
                            "live", executor=executor)
    handlers = await harness.handlers(owner_raw(), context={
        "scope": "scope-a", "activity_id": "activity-a", "turn_id": "turn-a"})
    invoke = handlers["harness__dsh-tool-search"][1].execute_call
    first = await invoke("alias-call", {"Query": "memory"})
    second = await invoke("alias-call", {"query": "memory"})
    assert second == first
    assert len(executor.calls) == 1
    import sqlite3
    with sqlite3.connect(harness.db_path) as db:
        row = db.execute("SELECT original_arguments,normalized_arguments,scope FROM calls").fetchone()
    assert json.loads(row[0]) == {"Query": "memory"}
    assert json.loads(row[1]) == {"query": "memory"}
    assert row[2] == "scope-a"
    await harness.close()


@pytest.mark.asyncio
async def test_receipt_reader_pages_complete_json_and_survives_restart(tmp_path):
    executor = FakeExecutor()
    async def large(name, args, call_id):
        executor.calls.append((name, args, call_id))
        return {"stdout": "回执内容" * 1800}
    executor.execute = large
    context = {"scope": "scope-a", "activity_id": "activity-a", "turn_id": "turn-a"}
    harness = DirectHarness(settings(), tmp_path, "owner-1", ["wechat|private-1"],
                            "live", executor=executor)
    handlers = await harness.handlers(owner_raw(), context=context)
    action = await handlers["harness__dsh-tool-search"][1].execute_call(
        "receipt-call", {"query": "large"})
    assert action["receipt_ref"] == {"activity": "activity-a", "turn": "turn-a",
                                     "call_id": "receipt-call"}
    await harness.close()

    restarted = DirectHarness(settings(), tmp_path, "owner-1", ["wechat|private-1"],
                               "live", executor=executor)
    readers = await restarted.handlers(owner_raw(), context=context)
    read_call = readers["harness_read_receipt"][1].execute_call
    pages = []
    offset = 0
    while True:
        page = await read_call(f"read-{offset}", {"call_id": "receipt-call",
                                                   "offset": offset, "limit": 1379})
        assert "error" not in page
        pages.append(page["text"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    reconstructed = "".join(pages)
    assert len(reconstructed) == page["total_chars"]
    assert json.loads(reconstructed)["result"]["stdout"] == "回执内容" * 1800
    assert len(executor.calls) == 1
    await restarted.close()


@pytest.mark.asyncio
async def test_receipt_reader_enforces_scope_and_is_absent_without_authorization(tmp_path):
    executor = FakeExecutor()
    harness = DirectHarness(settings(), tmp_path, "owner-1", ["wechat|private-1"],
                            "live", executor=executor)
    context = {"scope": "scope-a", "activity_id": "activity-a", "turn_id": "turn-a"}
    handlers = await harness.handlers(owner_raw(), context=context)
    await handlers["harness__dsh-tool-search"][1].execute_call("scoped-call", {"query": "x"})
    reader = handlers["harness_read_receipt"][1].execute_call
    cross_turn = await reader("read-cross-turn", {"call_id": "scoped-call", "turn": "turn-b"})
    # A missing receipt at a different turn is not treated as a scope violation.
    assert cross_turn == {"error": "receipt_not_found"}
    other_scope = await harness.handlers(owner_raw(), context={**context, "scope": "scope-b"})
    assert await other_scope["harness_read_receipt"][1].execute_call(
        "read-cross-scope", {"call_id": "scoped-call"}) == {"error": "receipt_scope_denied"}
    conflict = await other_scope["harness__dsh-tool-search"][1].execute_call(
        "scoped-call", {"query": "x"})
    assert conflict == {"error": "call_identity_conflict"}
    denied = await harness.handlers(None, context=context)
    assert denied == {}
    assert executor.calls == [("dsh-tool-search", {"query": "x"}, "scoped-call")]
    await harness.close()


@pytest.mark.asyncio
async def test_receipt_reader_caps_encoded_page_size_and_reassembles_escaped_unicode(tmp_path):
    executor = FakeExecutor()
    payload = "汉" * 2500 + '"\\' * 1250
    async def large(name, args, call_id):
        executor.calls.append((name, args, call_id))
        return {"stdout": payload}
    executor.execute = large
    context = {"scope": "scope-a", "activity_id": "activity-a", "turn_id": "turn-a"}
    harness = DirectHarness(settings(), tmp_path, "owner-1", ["wechat|private-1"],
                            "live", executor=executor)
    handlers = await harness.handlers(owner_raw(), context=context)
    await handlers["harness__dsh-tool-search"][1].execute_call(
        "budget-call", {"query": "large"})
    read_call = handlers["harness_read_receipt"][1].execute_call
    pages = []
    offset = 0
    while True:
        page = await read_call(f"budget-read-{offset}", {"call_id": "budget-call",
                                                            "offset": offset})
        assert "error" not in page
        assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) < 6000
        pages.append(page["text"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    reconstructed = "".join(pages)
    assert len(reconstructed) == page["total_chars"]
    assert json.loads(reconstructed)["result"]["stdout"] == payload
    assert len(executor.calls) == 1
    await harness.close()


@pytest.mark.asyncio
async def test_scoped_receipt_reads_across_turns_after_restart_and_legacy_is_exact_turn(tmp_path):
    executor = FakeExecutor()
    first_context = {"scope": "scope-a", "activity_id": "activity-a", "turn_id": "turn-a"}
    first = DirectHarness(settings(), tmp_path, "owner-1", ["wechat|private-1"],
                          "live", executor=executor)
    first_handlers = await first.handlers(owner_raw(), context=first_context)
    await first_handlers["harness__dsh-tool-search"][1].execute_call(
        "cross-turn-call", {"query": "x"})
    await first.close()

    restarted = DirectHarness(settings(), tmp_path, "owner-1", ["wechat|private-1"],
                              "live", executor=executor)
    next_context = {"scope": "scope-a", "activity_id": "activity-a", "turn_id": "turn-b"}
    reader = (await restarted.handlers(owner_raw(), context=next_context)
              )["harness_read_receipt"][1].execute_call
    page = await reader("cross-turn-read", {"call_id": "cross-turn-call",
                                             "activity": "activity-a", "turn": "turn-a"})
    assert json.loads(page["text"])["result"]["content"][0]["text"] == '{"hits":2}'
    assert len(executor.calls) == 1

    import sqlite3
    with sqlite3.connect(restarted.db_path) as db:
        db.execute("UPDATE calls SET scope=NULL WHERE call_id='cross-turn-call'")
    denied = await reader("legacy-cross-turn", {"call_id": "cross-turn-call",
                                                 "activity": "activity-a", "turn": "turn-a"})
    assert denied == {"error": "receipt_scope_denied"}
    with sqlite3.connect(restarted.db_path) as db:
        db.execute("UPDATE calls SET scope='' WHERE call_id='cross-turn-call'")
    denied_empty_scope = await reader("legacy-empty-scope-cross-turn", {
        "call_id": "cross-turn-call", "activity": "activity-a", "turn": "turn-a"})
    assert denied_empty_scope == {"error": "receipt_scope_denied"}
    await restarted.close()


@pytest.mark.asyncio
async def test_unknown_outcome_is_not_retried_and_autonomy_needs_grant(tmp_path):
    executor = FakeExecutor(fail=True)
    harness = DirectHarness(settings(), tmp_path, "owner-1", [], "live", executor=executor)
    context = {"activity_id": "activity-2", "turn_id": "turn-3"}
    denied = await harness.handlers(None, context=context, autonomous_grant=False)
    assert denied == {}
    assert await harness.execute("harness__dsh-tool-search", {"query": "x"}, context,
                                 "call-unknown") == {"error": "computer_owner_required"}
    assert executor.starts == 0
    allowed = await harness.handlers(None, context=context, autonomous_grant=True)
    invoke = allowed["harness__dsh-tool-search"][1].execute_call
    first = await invoke("call-unknown", {"query": "x"})
    second = await invoke("call-unknown", {"query": "x"})
    assert first["error"] == second["error"] == "outcome_unknown"
    assert len(executor.calls) == 1
    assert await harness.execute("harness__missing", {}, context, "call-missing") == {
        "error": "computer_owner_required"}
    await harness.close()


@pytest.mark.asyncio
async def test_unknown_tool_and_non_host_device_are_refused(tmp_path):
    executor = FakeExecutor()
    harness = DirectHarness(settings(), tmp_path, "owner-1", ["wechat|private-1"],
                            "live", executor=executor)
    context = {"principal": owner_raw(), "activity_id": "chat", "turn_id": "turn-4"}
    assert await harness.execute("harness__missing", {}, context, "id-1") == {
        "error": "unknown_harness_tool"}
    context["device"] = "laptop-2"
    assert await harness.execute("harness__dsh-tool-search", {"query": "x"}, context, "id-2") == {
        "error": "computer_device_unavailable"}
    assert executor.calls == []
    await harness.close()


@pytest.mark.asyncio
async def test_locked_native_host_session_workdir_todo_and_jobs(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    node = project_root / "var" / "harness" / "node" / "node.exe"
    runtime = project_root / "var" / "harness" / "runtime"
    if not node.is_file() or not (runtime / "node_modules" / ".pnpm").is_dir():
        pytest.skip("locked local Harness runtime is not installed")

    harness_settings = {
        **settings(), "node_path": str(node), "runtime_root": str(runtime),
        "project_root": str(tmp_path),
    }
    empty_native_config = tmp_path / "empty-harness-config.json"
    empty_native_config.write_text(json.dumps({"skills": [], "mcpServers": []}), encoding="utf-8")
    harness_settings["native_config"] = str(empty_native_config)
    harness = DirectHarness(harness_settings, tmp_path / "state", "owner-1",
                            ["wechat|private-1"], "live")
    raw = owner_raw()

    async def call(handlers, name, args, call_id):
        result = await handlers[f"harness__{name}"][1].execute_call(call_id, args)
        assert result.get("error") is None
        assert result["result"]["isError"] is False
        return result["result"]

    try:
        chat_one = await harness.handlers(raw, context={
            "scope": "smoke", "activity_id": "conversation", "turn_id": "turn-1"})
        first_dir = tmp_path / "chat-one"
        first_dir.mkdir()
        await call(chat_one, "working_directory", {"cd": str(first_dir)}, "wd-chat-one")
        current = await call(chat_one, "working_directory", {}, "wd-chat-one-read")
        assert Path(current["value"]["cwd"]) == first_dir

        await call(chat_one, "write", {"file_path": str(first_dir / "note.txt"),
                  "content": "temporary harness smoke"}, "write-temp")
        read = await call(chat_one, "read", {"file_path": str(first_dir / "note.txt")}, "read-temp")
        assert "temporary harness smoke" in read["value"]["lines"][0]["text"]

        todo = await call(chat_one, "todo_write", {"todos": [
            {"content": "temporary smoke task", "status": "completed"}]}, "todo-temp")
        assert todo["value"]["todos"][0]["content"] == "temporary smoke task"

        chat_two = await harness.handlers(raw, context={
            "scope": "smoke", "activity_id": "conversation", "turn_id": "turn-2"})
        current = await call(chat_two, "working_directory", {}, "wd-chat-two-read")
        assert Path(current["value"]["cwd"]) == first_dir

        finished_job = await call(chat_one, "pwsh", {
            "description": "Start a temporary output job",
            "command": "Start-Sleep -Milliseconds 400; Write-Output 'finished across turns'",
            "run_in_background": True}, "job-finished-across-turns")
        finished_job_id = finished_job["value"]["jobId"]
        killed_job = await call(chat_one, "pwsh", {
            "description": "Start a temporary stoppable job",
            "command": "Start-Sleep -Seconds 30; Write-Output 'should be stopped'",
            "run_in_background": True}, "job-kill-across-turns")
        killed_job_id = killed_job["value"]["jobId"]
        finished = await call(chat_two, "job_output", {
            "job_id": finished_job_id, "wait": True, "timeout_ms": 10000},
            "job-output-from-next-turn")
        assert "finished across turns" in json.dumps(finished, ensure_ascii=False)
        stopped = await call(chat_two, "job_kill", {"job_id": killed_job_id},
                             "job-kill-from-next-turn")
        assert stopped["value"]["outcome"] == "cancellation-requested"
        assert stopped["value"]["job"]["status"] == "stopping"

        activity_dir = tmp_path / "activity"
        activity_dir.mkdir()
        activity = await harness.handlers(None, context={
            "scope": "smoke", "activity_id": "activity-smoke", "turn_id": "activity-smoke"},
            autonomous_grant=True)
        await call(activity, "working_directory", {"cd": str(activity_dir)}, "wd-activity")
        current = await call(activity, "working_directory", {}, "wd-activity-read")
        assert Path(current["value"]["cwd"]) == activity_dir
        current = await call(chat_two, "working_directory", {}, "wd-chat-two-again")
        assert Path(current["value"]["cwd"]) == first_dir

        command = await call(activity, "pwsh", {
            "description": "Get the current directory",
            "command": "Get-Location | Select-Object -ExpandProperty Path"}, "pwsh-smoke")
        assert str(activity_dir) in command["value"]["stdout"]["text"]
        job = await call(activity, "pwsh", {
            "description": "Start a read-only directory query",
            "command": "Get-Location | Select-Object -ExpandProperty Path",
            "run_in_background": True}, "job-smoke")
        job_id = job["value"]["jobId"]
        result = await call(activity, "job_output", {
            "job_id": job_id, "wait": True, "timeout_ms": 10000}, "job-output-smoke")
        assert activity_dir.name in json.dumps(result, ensure_ascii=False)
    finally:
        await harness.close()
