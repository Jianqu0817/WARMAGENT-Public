import json

import pytest

from myatri.conversation.budget import TokenBudget
from myatri.providers.gateway import FixtureProvider, Gateway
from warmagent.activities import ActivityCoordinator
from warmagent.activity_runner import ActivityRunner


def call(call_id, name, args):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def answer(call_id, name, args):
    return {"tool_calls": [call(call_id, name, args)],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}}


class App:
    def __init__(self, responses):
        self.config = {"chat": {}}
        self.provider = FixtureProvider(responses)
        self.gateway = Gateway(self.provider, daily_token_limit=50000)
        self.persona = type("Persona", (), {"read_state": lambda self: type("State", (), {
            "ref": "persona@1", "payload": {"entries": {"P00": "小温暖"}}})()})()
        self.emotions = type("Emotion", (), {"read": lambda self: {"enabled": False},
            "context_block": lambda self, state: ""})()
        self.conversation = type("Conversation", (), {"context": type("Context", (), {
            "budget": TokenBudget(limit=12000, output_reserve=128, safety=32)})()})()


def setup(tmp_path, app, handlers=None):
    clock = [0.0]
    box = {}

    async def run(activity, *, prompt, reconcile_only=False):
        return await box["runner"].step(activity, prompt=prompt, reconcile_only=reconcile_only)

    coordinator = ActivityCoordinator(tmp_path / "activity.sqlite3", run,
        lambda: {"online": True, "authorized": True, "busy": False, "budget_available": True},
        notify_owner=box.setdefault("notifications", []).append, clock=lambda: clock[0])
    box["runner"] = ActivityRunner(lambda: app, lambda: handlers or {}, lambda: coordinator)
    return coordinator, box["runner"], clock, box["notifications"]


@pytest.mark.asyncio
async def test_schedule_wakeup_resumes_same_protocol_with_note_then_finishes(tmp_path):
    app = App([
        answer("create", "finish_activity", {"action": "create", "goal": "整理资料", "summary": "开始整理"}),
        answer("create-confirm", "finish_activity", {"action": "continue", "summary": "目标已保存"}),
        answer("wake", "activity_schedule_wakeup", {"delay_seconds": 5, "resume_note": "继续核对两条资料"}),
        answer("done", "finish_activity", {"action": "complete", "summary": "核对完毕", "notify_owner": "资料已核对。"}),
    ])
    coordinator, _, clock, notices = setup(tmp_path, app)
    for stamp in (600, 601, 602, 603, 604, 605):
        clock[0] = stamp
        await coordinator.tick(stamp)
    wake = coordinator.overview()["next_wakeup"]
    assert wake is not None
    assert wake["due_at"] == 610
    assert coordinator.overview()["status"] == "resting"

    clock[0] = 610
    resumed = await coordinator.tick(610)
    assert resumed["ran"] is True
    request = app.provider.requests[-1]
    context = next(m["content"] for m in request.messages if "当前活动状态（数据）" in m["content"])
    assert "继续核对两条资料" in context
    assert "wakeup_context" in context
    await coordinator.tick(611)
    await coordinator.tick(612)
    assert coordinator.overview()["status"] == "completed"
    assert notices == ["资料已核对。"]


@pytest.mark.asyncio
async def test_focus_tool_updates_limited_policy_and_reserved_handlers_are_ignored(tmp_path):
    app = App([answer("focus", "activity_set_focus", {
        "policy": "focus_first", "duration_seconds": 60, "reason": "完成当前整理"})])
    fake_schema = {"type": "function", "function": {"name": "activity_set_focus",
        "parameters": {"type": "object", "properties": {"policy": {"type": "string"}}}}}
    invoked = []
    coordinator, runner, clock, _ = setup(tmp_path, app, {"activity_set_focus": (fake_schema,
        lambda **kwargs: invoked.append(kwargs))})
    coordinator.checkpoint(goal="整理资料", status="active", protocol={"activity_id": "task-1"})
    clock[0] = 10
    await coordinator.tick(10)
    result = await coordinator.tick(10)
    overview = coordinator.overview()
    assert overview["focus_policy"] == "focus_first"
    assert overview["focus_until"] == 70
    assert overview["focus_reason"] == "完成当前整理"
    assert coordinator.should_defer_owner(now=20) is True
    assert coordinator.should_defer_owner(urgent=True, now=20) is False
    assert invoked == []
    assert result["activity"]["protocol"]["pending_calls"] == []


@pytest.mark.asyncio
async def test_schedule_mixed_batch_is_blocked_without_any_tool_effect(tmp_path):
    app = App([{
        "tool_calls": [call("wake", "activity_schedule_wakeup", {
            "delay_seconds": 10, "resume_note": "继续"}),
            call("other", "write_note", {"text": "should not run"})],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20}}])
    effects = []
    schema = {"type": "function", "function": {"name": "write_note", "parameters": {
        "type": "object", "properties": {"text": {"type": "string"}},
        "required": ["text"], "additionalProperties": False}}}
    coordinator, _, _, _ = setup(tmp_path, app, {"write_note": (schema, lambda **kwargs: effects.append(kwargs))})
    coordinator.checkpoint(goal="整理资料", status="active", protocol={"activity_id": "task-1"})
    await coordinator.tick(0)
    assert coordinator.overview()["status"] == "blocked"
    assert coordinator.overview()["next_wakeup"] is None
    assert effects == []


@pytest.mark.asyncio
async def test_explicit_coordinator_rejection_is_not_an_uncertain_effect(tmp_path):
    app = App([answer("wake-rejected", "activity_schedule_wakeup", {
        "delay_seconds": 10, "resume_note": "稍后继续"})])
    coordinator, _, clock, _ = setup(tmp_path, app)
    coordinator.checkpoint(goal="整理资料", status="active",
                           protocol={"activity_id": "task-rejected"})

    def reject_wakeup(**kwargs):
        raise ValueError("stale_activity_control")

    coordinator.schedule_wakeup = reject_wakeup
    clock[0] = 10
    await coordinator.tick(10)
    clock[0] = 11
    await coordinator.tick(11)

    activity = coordinator.overview()
    receipt = next(row for row in activity["receipts"] if row["call_id"] == "wake-rejected")
    result = json.loads(next(message["content"] for message in activity["protocol"]["messages"]
        if message.get("tool_call_id") == "wake-rejected"))
    assert receipt["state"] == "completed"
    assert result == {"error": "stale_activity_control", "not_executed": True}
    assert activity["protocol"].get("uncertain_calls", []) == []
    assert activity["status"] == "running"

