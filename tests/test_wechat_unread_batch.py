"""Offline acceptance checks for owner-private WeChat unread aggregation."""
import asyncio
import json
import sqlite3
import tomllib
from pathlib import Path

import pytest

from myatri.contracts import ModelResponse
from myatri.providers.gateway import FixtureProvider
from warmagent.bridge import Bridge, Incoming
from warmagent.runtime_inbox import RuntimeInbox
from warmagent.wechat import WeChatRunner


ROOT = Path(__file__).resolve().parents[1]
PLATFORM = "xiaowennuan-wechat"
OWNER_ROUTE = f"{PLATFORM}|owner"
GROUP_ROUTE = f"{PLATFORM}|room@chatroom"


def make_bridge(tmp_path, responses=()):
    config = {
        "project_root": str(tmp_path),
        "mode": "offline",
        "allowed_groups": [OWNER_ROUTE, GROUP_ROUTE, "fixture|owner"],
        "owner_id": "owner",
        "owner_name": "用户",
        "random_reply_probability": 0,
        "quality_guard_enabled": False,
        "proactive": {"enabled": False},
        "wechat_unread": {
            "enabled": True,
            "quiet_seconds": 0.01,
            "max_wait_seconds": 0.05,
        },
    }
    provider = FixtureProvider(list(responses))
    engine = tomllib.loads((ROOT / "config/engine.toml").read_text(encoding="utf-8"))
    bridge = Bridge(tmp_path / "groups", config, engine,
        ROOT / "config/persona/persona.md", provider_factory=lambda _scope: provider)
    return bridge, provider


def message(text, *, kind="private", sender="owner", message_id="m1", platform=PLATFORM):
    target = "owner" if kind == "private" else "room@chatroom"
    return Incoming(
        platform_id=platform,
        bot_id="bot",
        group_id=target,
        sender_id=sender,
        sender_name="改名后的昵称",
        message_id=message_id,
        sent_at="2026-10-28T10:00:00+08:00",
        text=text,
        mentioned=True,
        conversation_kind=kind,
    )


def disable_background_workers(bridge, monkeypatch):
    # Tests drive the durable inbox explicitly so its background loop cannot race them.
    monkeypatch.setattr(bridge, "start", lambda: None)


def provider_text(provider):
    return json.dumps(provider.requests[0].messages, ensure_ascii=False)


class FakeWeChatClient:
    bot_id = "bot"
    group_id = "room@chatroom"
    private_id = "owner"

    def __init__(self):
        self.sent = []

    async def send(self, text, target_id=None):
        self.sent.append((text, target_id))
        return f"receipt-{len(self.sent)}"


async def mature_batch(bridge):
    await asyncio.sleep(0.02)
    return await bridge._process_next_input()


@pytest.mark.asyncio
async def test_owner_private_burst_is_persisted_and_generates_once(tmp_path, monkeypatch):
    bridge, provider = make_bridge(tmp_path, [{"content": "我把这几条一起看了。"}])
    disable_background_workers(bridge, monkeypatch)
    messages = [message(f"第{i}条原文", message_id=f"burst-{i}") for i in range(5)]
    try:
        received = [await bridge.receive(item) for item in messages]
        root_key = messages[0].key
        assert all(item["status"] == "queued" and item["reason"] == "unread_batch"
                   and item["id"] == root_key for item in received)
        for source in messages:
            stored = bridge.input_inbox.get(source.key)
            assert stored["state"] == "pending"
            assert stored["payload"] == source.model_dump()
        assert provider.requests == []

        await asyncio.sleep(0.02)
        result = await bridge._process_next_input()

        assert result["status"] == "prepared" and result["id"] == root_key
        assert len(provider.requests) == 1
        prompt = provider_text(provider)
        assert all(source.text in prompt for source in messages)
        assert all(bridge.input_inbox.get(source.key)["state"] == "completed"
                   for source in messages)
        assert all(bridge.input_inbox.get(source.key)["result"]["id"] == root_key
                   for source in messages)
        with sqlite3.connect(bridge.path) as db:
            prepared = db.execute("SELECT status FROM deliveries WHERE id=?", (root_key,)).fetchone()
            assert prepared == ("prepared",)
            assert db.execute("SELECT COUNT(*) FROM deliveries WHERE scope=? AND status='prepared'",
                              (messages[0].scope,)).fetchone()[0] == 1
        client = FakeWeChatClient()
        runner = WeChatRunner(bridge, client)
        runner.scope_targets = {messages[0].scope: "owner"}
        await runner.poll()
        assert client.sent == [("我把这几条一起看了。", "owner")]
    finally:
        await bridge.close()


def test_unread_batch_quiet_window_resets_but_max_wait_is_bounded(tmp_path):
    quiet = RuntimeInbox(tmp_path / "quiet.sqlite3")
    root = message("第一条", message_id="quiet-1")
    quiet.enqueue(root.key, root.scope, "fp-1", root.model_dump(), batch=True,
        quiet_seconds=1.5, max_wait_seconds=6, now=100)
    assert quiet.claim(now=101.49) is None
    later = message("静默重新计时", message_id="quiet-2")
    quiet.enqueue(later.key, later.scope, "fp-2", later.model_dump(), batch=True,
        quiet_seconds=1.5, max_wait_seconds=6, now=101)
    assert quiet.claim(now=102.49) is None
    assert quiet.claim(now=102.5)["key"] == root.key

    bounded = RuntimeInbox(tmp_path / "max-wait.sqlite3")
    root = message("持续输入", message_id="bounded-0")
    bounded.enqueue(root.key, root.scope, "fp-0", root.model_dump(), batch=True,
        quiet_seconds=1.5, max_wait_seconds=6, now=0)
    for index in range(1, 7):
        source = message(f"持续输入{index}", message_id=f"bounded-{index}")
        bounded.enqueue(source.key, source.scope, f"fp-{index}", source.model_dump(), batch=True,
            quiet_seconds=1.5, max_wait_seconds=6, now=index * 0.9)
    assert bounded.claim(now=5.99) is None
    assert bounded.claim(now=6)["key"] == root.key


def test_unread_not_consumed_before_async_task_wait_remains_pending(tmp_path):
    inbox = RuntimeInbox(tmp_path / 'pending.sqlite3')
    first = message('开始工具任务', message_id='wait-first')
    later = message('执行期间的新问题', message_id='wait-later')
    inbox.enqueue(first.key, first.scope, 'first', first.model_dump(), batch=True, now=100)
    claim = inbox.claim(now=102)
    inbox.enqueue(later.key, later.scope, 'later', later.model_dump(), batch=True, now=103)
    assert inbox.complete(first.key, claim['lease_token'],
        {'id': first.key, 'status': 'waiting_for_computer'}, now=104, consumed_keys=[first.key])
    assert inbox.get(later.key)['state'] == 'pending'
    assert inbox.batch_root(later.key) == later.key
    assert inbox.claim(now=104)['key'] == later.key


def test_claimed_reply_does_not_absorb_the_next_unread_batch(tmp_path):
    inbox = RuntimeInbox(tmp_path / 'sealed.sqlite3')
    first = message('第一轮', message_id='sealed-first')
    later = message('下一轮', message_id='sealed-later')
    inbox.enqueue(first.key, first.scope, 'first', first.model_dump(), batch=True, now=100)
    claim = inbox.claim(now=102)
    inbox.complete(first.key, claim['lease_token'], {'id': first.key, 'status': 'prepared'}, now=103,
        consumed_keys=[first.key])
    assert inbox.seal_batch(first.key)
    inbox.enqueue(later.key, later.scope, 'later', later.model_dump(), batch=True, now=104)
    assert inbox.batch_root(later.key) == later.key


@pytest.mark.asyncio
async def test_unread_batch_is_limited_to_wechat_owner_private(tmp_path, monkeypatch):
    bridge, provider = make_bridge(tmp_path, [{"content": "收到。"}] * 8)
    disable_background_workers(bridge, monkeypatch)
    try:
        outsider = await bridge.receive(message("冒充私聊", sender="someone-else", message_id="spoof"))
        assert outsider == {"status": "ignored", "reason": "private_owner_required"}
        assert bridge.input_inbox.get(message("冒充私聊", sender="someone-else", message_id="spoof").key) is None

        other_platform = await bridge.receive(message("非微信私聊", message_id="other-platform", platform="fixture"))
        assert other_platform["status"] == "prepared"

        group_messages = [message(f"群消息{i}", kind="group", message_id=f"group-{i}") for i in range(3)]
        group_results = [await bridge.receive(item) for item in group_messages]
        assert all(result["status"] == "prepared" for result in group_results)
        assert all(bridge.input_inbox.get(item.key)["state"] == "completed" for item in group_messages)
        assert len(provider.requests) == 4
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_urgent_owner_control_bypasses_unread_wait(tmp_path, monkeypatch):
    bridge, _provider = make_bridge(tmp_path, [{"content": "好，我先停下。"}])
    disable_background_workers(bridge, monkeypatch)
    bridge.activities.checkpoint(goal="离线夹具活动", summary="正在整理", next_step="继续整理",
        protocol={"activity_id": "fixture-activity"}, status="active")
    source = message("暂停任务", message_id="urgent-pause")
    try:
        result = await bridge.receive(source)
        assert result["status"] != "queued" or result.get("reason") != "unread_batch"
        assert bridge.activities.overview()["status"] == "paused"
        assert bridge.input_inbox.get(source.key)["state"] == "completed"
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_duplicate_event_and_restart_preserve_one_pending_original(tmp_path, monkeypatch):
    bridge, first_provider = make_bridge(tmp_path)
    disable_background_workers(bridge, monkeypatch)
    sources = [message("仍在排队", message_id="restart-1"),
               message("第二条仍在排队", message_id="restart-2")]
    bridge_open = True
    try:
        first = await bridge.receive(sources[0])
        duplicate = await bridge.receive(sources[0])
        second = await bridge.receive(sources[1])
        assert first["id"] == duplicate["id"] == second["id"] == sources[0].key
        assert bridge.input_inbox.summary(sources[0].scope)["pending"] == 2
        assert first_provider.requests == []
        for source in sources:
            row = bridge.input_inbox.get(source.key)
            assert row["state"] == "pending" and row["payload"] == source.model_dump()
        await bridge.close()
        bridge_open = False

        restarted, provider = make_bridge(tmp_path, [{"content": "我看到了。"}])
        disable_background_workers(restarted, monkeypatch)
        try:
            replay = await restarted.receive(sources[0])
            assert replay["id"] == sources[0].key and replay["reason"] == "unread_batch"
            assert restarted.input_inbox.summary(sources[0].scope)["pending"] == 2
            for source in sources:
                row = restarted.input_inbox.get(source.key)
                assert row["state"] == "pending" and row["payload"] == source.model_dump()

            await asyncio.sleep(0.02)
            result = await restarted._process_next_input()
            assert result["id"] == sources[0].key
            assert len(provider.requests) == 1
            assert all(restarted.input_inbox.get(source.key)["state"] == "completed"
                       for source in sources)
        finally:
            await restarted.close()
    finally:
        if bridge_open:
            await bridge.close()


@pytest.mark.asyncio
async def test_message_arriving_during_generation_discards_old_draft(tmp_path, monkeypatch):
    bridge, provider = make_bridge(tmp_path, [
        {"content": "旧草稿"},
        {"content": "两条合并后的答复"},
    ])
    disable_background_workers(bridge, monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()
    original_generate = provider.generate
    first_call = True

    async def controlled_generate(request):
        nonlocal first_call
        if first_call:
            first_call = False
            started.set()
            await release.wait()
        return await original_generate(request)

    provider.generate = controlled_generate
    first = message("先问一件事", message_id="during-1")
    second = message("生成时补充的内容", message_id="during-2")
    worker = None
    try:
        queued = await bridge.receive(first)
        assert queued["reason"] == "unread_batch"
        await asyncio.sleep(0.02)
        worker = asyncio.create_task(bridge._process_next_input())
        await asyncio.wait_for(started.wait(), 5)

        arrival = await bridge.receive(second)
        assert arrival["id"] == first.key and arrival["reason"] == "unread_batch"
        assert await bridge._process_next_input() is None
        assert provider.requests == []
        release.set()
        result = await asyncio.wait_for(worker, 8)

        assert result["status"] == "prepared" and result["id"] == first.key
        assert len(provider.requests) == 2
        assert second.text in json.dumps(provider.requests[-1].messages, ensure_ascii=False)
        client = FakeWeChatClient()
        runner = WeChatRunner(bridge, client)
        runner.scope_targets = {first.scope: "owner"}
        await runner.poll()
        assert client.sent == [("两条合并后的答复", "owner")]
        assert bridge.input_inbox.get(first.key)["state"] == "completed"
        assert bridge.input_inbox.get(second.key)["state"] == "completed"
    finally:
        release.set()
        if worker and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await bridge.close()


@pytest.mark.asyncio
async def test_prepared_unclaimed_reply_reopens_for_new_unread(tmp_path, monkeypatch):
    bridge, provider = make_bridge(tmp_path, [
        {"content": "先前草稿"},
        {"content": "合并后的最终答复"},
    ])
    disable_background_workers(bridge, monkeypatch)
    first = message("先前问题", message_id="prepared-1")
    second = message("尚未发送时补充", message_id="prepared-2")
    try:
        await bridge.receive(first)
        initial = await mature_batch(bridge)
        assert initial["status"] == "prepared" and initial["id"] == first.key
        assert len(provider.requests) == 1
        with sqlite3.connect(bridge.path) as db:
            assert db.execute("SELECT status,reply FROM deliveries WHERE id=?", (first.key,)).fetchone() == (
                "prepared", "先前草稿")

        arrival = await bridge.receive(second)
        assert arrival["id"] == first.key and arrival["reason"] == "unread_batch"
        with sqlite3.connect(bridge.path) as db:
            assert db.execute("SELECT status FROM deliveries WHERE id=?", (first.key,)).fetchone() == ("received",)
        regenerated = await mature_batch(bridge)

        assert regenerated["status"] == "prepared" and regenerated["id"] == first.key
        assert len(provider.requests) == 2
        assert second.text in json.dumps(provider.requests[-1].messages, ensure_ascii=False)
        client = FakeWeChatClient()
        runner = WeChatRunner(bridge, client)
        runner.scope_targets = {first.scope: "owner"}
        await runner.poll()
        assert client.sent == [("合并后的最终答复", "owner")]
    finally:
        await bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "unknown"])
async def test_unread_arrival_does_not_replay_completed_or_unknown_tool(tmp_path, monkeypatch, outcome):
    tool_call = {"id": "fixture-effect-1", "type": "function", "function": {
        "name": "fixture_effect", "arguments": '{"value":"once"}'}}
    first_response = ModelResponse(provider="fixture", model="fixture-v1", content="",
        tool_calls=[tool_call])
    bridge, provider = make_bridge(tmp_path, [
        first_response,
        {"content": "旧草稿"},
        {"content": "我已看过补充内容，保留原工具回执。"},
    ])
    disable_background_workers(bridge, monkeypatch)
    calls = []
    schema = {"type": "function", "function": {"name": "fixture_effect", "description": "offline fixture",
        "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                       "required": ["value"], "additionalProperties": False}}}

    async def fixture_handlers(_raw):
        async def execute_call(call_id, args):
            calls.append((call_id, args))
            if outcome == "unknown":
                return {"error": "outcome_unknown", "state": "unknown"}
            return {"status": "completed", "value": args["value"]}

        async def placeholder(**_kwargs):
            raise AssertionError("host calls must use execute_call")

        placeholder.execute_call = execute_call
        return {"fixture_effect": (schema, placeholder)}

    bridge._chat_handlers = fixture_handlers
    started, release = asyncio.Event(), asyncio.Event()
    original_generate = provider.generate
    call_number = 0

    async def controlled_generate(request):
        nonlocal call_number
        call_number += 1
        if call_number == 2:
            started.set()
            await release.wait()
        return await original_generate(request)

    provider.generate = controlled_generate
    first = message("请执行一次操作", message_id=f"tool-{outcome}-1")
    second = message("操作完成后补充", message_id=f"tool-{outcome}-2")
    worker = None
    try:
        await bridge.receive(first)
        await asyncio.sleep(0.02)
        worker = asyncio.create_task(bridge._process_next_input())
        await asyncio.wait_for(started.wait(), 5)
        assert calls == [("fixture-effect-1", {"value": "once"})]
        arrival = await bridge.receive(second)
        assert arrival["id"] == first.key and arrival["reason"] == "unread_batch"
        release.set()
        result = await asyncio.wait_for(worker, 8)

        assert result["status"] == "prepared" and result["id"] == first.key
        assert calls == [("fixture-effect-1", {"value": "once"})]
        assert len(provider.requests) == 3
        protocol = bridge.apps[first.scope].queue.get_protocol(first.key)
        receipt = next(item for item in protocol["messages"]
                       if item.get("role") == "tool" and item.get("tool_call_id") == "fixture-effect-1")
        receipt_body = json.loads(receipt["content"])
        if outcome == "unknown":
            assert receipt_body["state"] == "unknown" or receipt_body["error"] == "outcome_unknown"
        assert second.text in json.dumps(provider.requests[-1].messages, ensure_ascii=False)
    finally:
        release.set()
        if worker and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await bridge.close()
