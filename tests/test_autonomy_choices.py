import copy

import jsonschema
import pytest

from warmagent.activities import ActivityCoordinator
from warmagent.activity_runner import ActivityRunner, FINISH_ACTIVITY
from warmagent.tool_catalog import ToolCatalog
from test_activity_runner import FakeApp, finish_response


def _build(tmp_path, app, *, notify_owner=None, settings=None):
    box = {}
    results = []

    async def run_step(activity, *, prompt, reconcile_only=False):
        result = await box["runner"].step(activity, prompt=prompt,
                                          reconcile_only=reconcile_only)
        results.append(result)
        return result

    coordinator = ActivityCoordinator(tmp_path / "activity.sqlite3", run_step,
        lambda: {"online": True, "authorized": True, "busy": False,
                 "budget_available": True},
        notify_owner=notify_owner, interval=600, clock=lambda: 0)
    box["runner"] = ActivityRunner(lambda: app, lambda: {}, lambda: coordinator,
                                    settings=settings)
    return coordinator, box["runner"], results


def test_finish_schema_bounds_optional_private_note_ids():
    parameters = FINISH_ACTIVITY["function"]["parameters"]
    field = parameters["properties"]["share_note_ids"]
    assert "share_note_ids" not in parameters["required"]
    assert field["type"] == "array" and field["maxItems"] == 5
    assert field["items"] == {"type": "string", "maxLength": 128}
    assert "notify_owner" in field["description"] and "实际引用" in field["description"]
    jsonschema.validate({"action": "continue", "summary": "继续"}, parameters)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"action": "continue", "summary": "继续",
            "share_note_ids": ["n1", "n2", "n3", "n4", "n5", "n6"]}, parameters)


@pytest.mark.asyncio
async def test_note_ids_survive_notification_retry_in_protocol(tmp_path):
    app = FakeApp([finish_response("finish-share", "complete", summary="整理完成",
        notify_owner="我把这条随笔整理好了。", share_note_ids=["note-2026-01"])])
    coordinator_ref = {}
    attempts, pending_seen = [], []

    async def notify_owner(text):
        attempts.append(text)
        pending_seen.append(copy.deepcopy(
            coordinator_ref["coordinator"].overview()["protocol"]["notification_pending"]))
        if len(attempts) == 1:
            raise RuntimeError("fixture_delivery_failure")

    coordinator, _, results = _build(tmp_path, app, notify_owner=notify_owner)
    coordinator_ref["coordinator"] = coordinator
    coordinator.checkpoint(goal="整理随笔", summary="整理中", status="active",
        protocol={"activity_id": "share-test"})

    await coordinator.tick(600)
    failed = await coordinator.tick(601)
    assert failed["reason"] == "notification_retry_pending"
    result = results[-1]
    assert result["notify_owner"] == "我把这条随笔整理好了。"
    assert result["note_ids"] == ["note-2026-01"]
    assert result["owner_requested"] is False
    assert coordinator._row()["protocol"]["notification_pending"] == {
        "call_id": "finish-share", "text": "我把这条随笔整理好了。",
        "note_ids": ["note-2026-01"], "owner_requested": False}
    receipt = next(row for row in coordinator._row()["receipts"]
                   if row["call_id"] == "finish-share")
    assert receipt["result"]["share_note_ids"] == ["note-2026-01"]

    recovered = await coordinator.tick(602)
    assert recovered["reason"] == "notification_queued"
    assert attempts == ["我把这条随笔整理好了。"] * 2
    assert [item["note_ids"] for item in pending_seen] == [["note-2026-01"]] * 2
    assert "notification_pending" not in coordinator._row()["protocol"]


@pytest.mark.asyncio
async def test_note_ids_without_notification_do_not_enter_receipt_or_pending(tmp_path):
    app = FakeApp([finish_response("finish-private", "complete", summary="记录完成",
        share_note_ids=["note-private"])])
    notifications = []
    coordinator, _, results = _build(tmp_path, app, notify_owner=notifications.append)
    coordinator.checkpoint(goal="整理", status="active",
        protocol={"activity_id": "no-notify-test"})

    await coordinator.tick(600)
    await coordinator.tick(601)
    result = results[-1]
    receipt = next(row for row in coordinator._row()["receipts"]
                   if row["call_id"] == "finish-private")
    assert "note_ids" not in result and "notify_owner" not in result
    assert "share_note_ids" not in receipt["result"]
    assert "notification_pending" not in coordinator._row()["protocol"]
    assert notifications == []


@pytest.mark.asyncio
async def test_owner_plan_feedback_is_marked_before_plan_completion(tmp_path):
    app = FakeApp([finish_response("finish-plan", "complete", summary="安排完成",
        notify_owner="你安排的步骤已完成。", share_note_ids=["note-actual"])])
    notifications = []
    coordinator, _, results = _build(tmp_path, app, notify_owner=notifications.append)
    coordinator.checkpoint(goal="原目标", summary="原摘要", next_step="原下一步",
        status="active", protocol={"activity_id": "plan-test"})
    coordinator.plans.submit(source_ref="owner-turn@1", scope="private:owner",
        owner_id="owner", source_text="先做这一步", mode="next", instruction="核对指定资料")

    await coordinator.tick(600)
    await coordinator.tick(601)
    result = results[-1]
    assert result["owner_requested"] is True
    assert result["note_ids"] == ["note-actual"]
    pending = result["protocol"]["notification_pending"]
    assert pending["owner_requested"] is True and pending["note_ids"] == ["note-actual"]
    assert result["protocol"]["owner_instruction"]["status"] == "completed"
    assert notifications == ["你安排的步骤已完成。"]


@pytest.mark.asyncio
async def test_continue_with_new_goal_records_previous_direction_and_clears_old_step(tmp_path):
    app = FakeApp([finish_response("redirect-self", "continue", summary="换个方向试试",
        goal="改看短篇小说",)])
    coordinator, _, _ = _build(tmp_path, app)
    coordinator.checkpoint(goal="整理长篇小说资料", summary="原方向进展", next_step="查作者生平",
        status="active", protocol={"activity_id": "self-direction"})

    await coordinator.tick(600)
    await coordinator.tick(601)
    row = coordinator._row()
    assert row["goal"] == "改看短篇小说"
    assert row["summary"] == "换个方向试试"
    assert row["next_step"] == ""
    assert row["protocol"]["previous_self_direction"] == {
        "goal": "整理长篇小说资料", "summary": "原方向进展", "next_step": "查作者生平"}
    receipt = next(item for item in row["receipts"] if item["call_id"] == "redirect-self")
    assert receipt["result"]["goal"] == "改看短篇小说"


@pytest.mark.asyncio
async def test_owner_plan_rejects_self_redirect_without_losing_plan_or_notifying(tmp_path):
    app = FakeApp([finish_response("redirect-owner", "continue", summary="尝试改方向",
        goal="跳过用户安排", next_step="做别的", notify_owner="已完成用户安排。",
        share_note_ids=["note-unshared"])])
    notifications = []
    coordinator, _, results = _build(tmp_path, app, notify_owner=notifications.append)
    coordinator.checkpoint(goal="用户安排的目标", summary="安排摘要", next_step="先完成指定步骤",
        status="active", protocol={"activity_id": "owner-plan",
            "owner_instruction": {"status": "applied", "request_id": "plan-live"}})

    await coordinator.tick(600)
    await coordinator.tick(601)
    row = coordinator._row()
    assert row["goal"] == "用户安排的目标"
    assert row["summary"] == "安排摘要"
    assert row["next_step"] == "先完成指定步骤"
    assert row["protocol"]["owner_instruction"]["status"] == "applied"
    assert "previous_self_direction" not in row["protocol"]
    assert "notification_pending" not in row["protocol"]
    assert "notify_owner" not in results[-1] and "note_ids" not in results[-1]
    receipt = next(item for item in row["receipts"] if item["call_id"] == "redirect-owner")
    assert receipt["result"]["error"] == "owner_plan_goal_locked"
    assert notifications == []


@pytest.mark.asyncio
async def test_empty_activity_can_rest_without_creating_goal_or_skipping_interval(tmp_path):
    app = FakeApp([finish_response("rest-empty", "rest", summary="先歇一会儿")])
    coordinator, _, _ = _build(tmp_path, app)

    result = await coordinator.tick(600)
    row = coordinator._row()
    assert result["reason"] == "step"
    assert row["status"] == "empty" and row["goal"] == ""
    assert row["protocol"]["pending_calls"] == []
    assert coordinator.schedule_view(600)["next_check_at"] == 1200
    assert (await coordinator.tick(601))["reason"] == "interval"
    assert len(app.provider.requests) == 1
    assert "探索、娱乐、创作" in app.provider.requests[0].messages[0]["content"]


@pytest.mark.asyncio
async def test_memory_projection_refreshes_after_restore_without_duplicate(tmp_path):
    app = FakeApp([{"content": "先记住当前想法。"}, {"content": "继续时看到了新想法。"}])
    versions = iter(["old-interest", "new-interest"])

    def memory_context(_app, _activity):
        return {"interests": [next(versions)]}

    coordinator, runner, _ = _build(tmp_path, app,
        settings={"memory_context_getter": memory_context})
    coordinator.checkpoint(goal="整理想法", summary="", status="active",
        protocol={"activity_id": "memory-refresh"})
    activity = coordinator.overview()

    first = await runner.step(activity, prompt="idle prompt")
    assert first["protocol"]["memory_context_index"] == 4
    restored = copy.deepcopy(activity)
    restored["protocol"] = copy.deepcopy(first["protocol"])
    restored["protocol"].pop("memory_context_index")  # legacy persisted protocol

    second = await runner.step(restored, prompt="idle prompt")
    request = app.provider.requests[-1]
    projections = [message["content"] for message in request.messages
        if message.get("role") == "system"
        and message.get("content", "").startswith("有限的owner范围记忆材料")]
    assert len(projections) == 1 and "new-interest" in projections[0]
    assert "old-interest" not in projections[0]
    assert second["protocol"]["memory_context_index"] == 4
    assert second["protocol"]["activity_context_index"] == 3
    assert len(second["protocol"]["messages"]) == len(first["protocol"]["messages"]) + 1


def test_personal_and_interest_tools_are_discovered_by_tasks_keywords():
    def entry(name, description):
        schema = {"type": "function", "function": {"name": name,
            "description": description, "parameters": {"type": "object",
                "properties": {}, "additionalProperties": False}}}
        return schema, lambda **_kwargs: None

    handlers = {
        "harness__personal_note": entry("harness__personal_note", "记录随笔、便笺与灵感"),
        "harness__direction_update_interest": entry("harness__direction_update_interest", "更新兴趣进展"),
        "harness__direction_set_mode": entry("harness__direction_set_mode", "查看休息与睡眠状态"),
    }
    handlers.update({f"harness__fixture_{i}": entry(f"harness__fixture_{i}", "fixture")
                     for i in range(7)})
    catalog = ToolCatalog(handlers)
    expected = {"harness__personal_note", "harness__direction_update_interest",
                "harness__direction_set_mode"}
    assert expected <= set(catalog.category_tools("tasks"))
    for query, tool_name in (("兴趣", "harness__direction_update_interest"),
                             ("随笔", "harness__personal_note"),
                             ("休息", "harness__direction_set_mode")):
        result = catalog.discover(query=query)
        assert tool_name in {item["function"]["name"] for item in result["tools"]}
        assert "tasks" in result["categories"]
