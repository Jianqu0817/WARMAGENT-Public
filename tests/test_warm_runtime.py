"""Synthetic Bridge integration checks; these never connect to a live model or WeChat."""
import asyncio
import json
import sqlite3
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from myatri.contracts import ModelResponse, Record
from myatri.providers.gateway import FixtureProvider
from warmagent.bridge import Bridge, Incoming
from warmagent.server import pending_deliveries
from warmagent.wechat import WeChatRunner


ROOT = Path(__file__).resolve().parents[1]


def incoming(kind="group", *, sender="member", text="hello", message_id="m1"):
    target = "owner" if kind == "private" else "room"
    return Incoming(platform_id="fixture", bot_id="bot", group_id=target,
        sender_id=sender, message_id=message_id, sent_at="2026-10-24T10:00:00+08:00",
        text=text, mentioned=True, conversation_kind=kind)


def make_bridge(tmp_path, responses=(), *, mode="offline", autonomous=False, direct=False):
    config = {
        "project_root": str(tmp_path), "mode": mode,
        "allowed_groups": ["fixture|room", "fixture|owner"],
        "owner_id": "owner", "owner_name": "用户", "shared_memory": True,
        "random_reply_probability": 0, "quality_guard_enabled": False,
        "autonomy": {"enabled": autonomous, "scan_interval_seconds": 600}, "proactive": {"enabled": False},
        "computer": {"direct_enabled": direct},
    }
    provider = FixtureProvider(list(responses))
    engine = tomllib.loads((ROOT / "config/engine.toml").read_text(encoding="utf-8"))
    bridge = Bridge(tmp_path / "groups", config, engine,
        ROOT / "config/persona/persona.md", provider_factory=lambda _scope: provider)
    return bridge, provider


@pytest.mark.asyncio
async def test_idle_opportunity_uses_real_self_direction_availability(tmp_path):
    bridge, provider = make_bridge(tmp_path, mode='live', autonomous=True, direct=True)
    calls = []
    try:
        bridge.register_route(incoming('private', sender='owner'))
        bridge.direct.settings.update(enabled=True, owner_id='owner',
            permission_profile='full', autonomy_granted=True)
        async def step(activity, **kwargs):
            calls.append(activity)
            return {'action': 'create', 'goal': '离线自主活动夹具', 'summary': '已经推进'}
        bridge.activities.run_step = step
        result = await bridge.activities.tick(time.time() + 601)
        assert result['ran'] and len(calls) == 1
        assert bridge.activities.overview()['goal'] == '离线自主活动夹具'
        assert not provider.requests
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_actual_activity_loop_reaches_runner_through_task_slots(tmp_path):
    bridge, provider = make_bridge(tmp_path, mode='live', autonomous=True, direct=True)
    reached = asyncio.Event()
    task = None
    try:
        bridge.register_route(incoming('private', sender='owner'))
        bridge.direct.settings.update(enabled=True, owner_id='owner',
            permission_profile='full', autonomy_granted=True)
        bridge.activities.interval = 0
        async def step(activity, **kwargs):
            reached.set()
            return {'action': 'create', 'goal': '完整调度链夹具', 'summary': '推进成功'}
        bridge.activities.run_step = step
        task = asyncio.create_task(bridge._activity_loop())
        await asyncio.wait_for(reached.wait(), 2)
        assert bridge.direct_error is None
        assert bridge.activities.overview()['goal'] == '完整调度链夹具'
        assert not provider.requests
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await bridge.close()


@pytest.mark.asyncio
async def test_autonomy_respects_sleep_and_checks_expired_deadline(tmp_path, monkeypatch):
    bridge, _ = make_bridge(tmp_path, mode='live', autonomous=True, direct=True)
    try:
        route = incoming('private', sender='owner')
        bridge.register_route(route)
        bridge.direct.settings.update(enabled=True, owner_id='owner',
            permission_profile='full', autonomy_granted=True)
        stamp = time.time()
        bridge.self_direction.set_mode(route.scope, 'sleeping', until=stamp + 60)
        assert bridge._activity_available() is False
        monkeypatch.setattr('warmagent.bridge.time.time', lambda: stamp + 61)
        assert bridge._activity_available()['authorized']
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_old_received_receipts_do_not_block_idle_but_live_work_does(tmp_path):
    bridge, _ = make_bridge(tmp_path, mode='live', autonomous=True, direct=True)
    try:
        route = incoming('private', sender='owner')
        bridge.register_route(route)
        bridge.direct.settings.update(enabled=True, owner_id='owner',
            permission_profile='full', autonomy_granted=True)
        with sqlite3.connect(bridge.path) as db:
            db.execute("INSERT INTO deliveries(id,scope,fingerprint,status,updated) "
                "VALUES('old-input',?,'fixture','received',0)", (route.scope,))
        assert not bridge._activity_available()['busy']
        bridge.generations['current-input'] = asyncio.current_task()
        assert bridge._activity_available()['busy']
        bridge.generations.clear()
        with sqlite3.connect(bridge.path) as db:
            db.execute("UPDATE deliveries SET status='prepared' WHERE id='old-input'")
        assert bridge._activity_available()['busy']
        with sqlite3.connect(bridge.path) as db:
            db.execute("UPDATE deliveries SET status='received' WHERE id='old-input'")
        with sqlite3.connect(bridge.path) as db:
            assert db.execute("SELECT status FROM deliveries WHERE id='old-input'").fetchone() == ('received',)
    finally:
        bridge.generations.clear()
        await bridge.close()


@pytest.mark.asyncio
async def test_completed_activity_is_indexed_self_experience_without_human_message(tmp_path):
    bridge, _ = make_bridge(tmp_path)
    try:
        bridge.register_route(incoming('private', sender='owner'))
        state = {'status': 'completed', 'goal': '整理', 'summary': '检查完目录',
            'protocol': {'activity_id': 'activity-completed'},
            'receipts': [{'call_id': 'checked', 'state': 'completed', 'result': {'files': 2}}]}
        bridge._archive_activity(state)
        bridge._archive_activity(state)
        app = bridge._owner_application()
        assert app.store.list_records('raw_message') == []
        runs = [r for r in app.store.list_records('cognitive_run') if r.payload.get('role') == 'autonomous_activity']
        assert len(runs) == 1
        assert runs[0].payload['human_evidence'] is False
        memory = app.store.get_record('memory_activity-completed')
        assert memory.source_refs == [runs[0].ref]
        with app.queue.db() as db:
            assert db.execute("SELECT COUNT(*) FROM jobs WHERE role='refresh_index'").fetchone()[0] == 1
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_shared_library_ui_control_targets_latest_conversation_not_library_id(tmp_path):
    from warmagent.context_viewer import control_scope
    bridge, _ = make_bridge(tmp_path, [{'content': '我在这里。'}])
    try:
        private = incoming('private', sender='owner', message_id='ui-private')
        bridge.register_route(private)
        await bridge.receive(private)
        library = bridge.root / bridge.library_scope
        scope = control_scope(tmp_path, library, {'shared_memory': True, 'state_root': 'groups'})
        assert scope == private.scope and scope != library.name
        assert (await bridge.clear_runtime_context(scope))['ok']
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_shared_library_keeps_channel_windows_and_single_worker(tmp_path):
    bridge, _ = make_bridge(tmp_path)
    try:
        group = bridge._application(incoming(), start_workers=True)
        private = bridge._application(incoming("private", sender="owner"), start_workers=True)
        assert group.store is private.store
        assert group.index is private.index
        assert group.persona is private.persona
        assert group.conversation is not private.conversation
        assert group.conversation.context is not private.conversation.context
        assert group.conversation.context.window is not private.conversation.context.window
        assert len(bridge.tasks) == len(bridge.index_tasks) == 1
        group.conversation.context.window.clear()
        assert group.queue.get_meta("context_window_boundary")
        assert private.queue.get_meta("context_window_boundary") is None
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_group_owner_cannot_recall_or_read_private_raw_or_derived_records(tmp_path):
    bridge, _ = make_bridge(tmp_path)
    try:
        private = bridge._application(incoming("private", sender="owner"), start_workers=False)
        group = bridge._application(incoming(), start_workers=False)
        group.recall.current_sender = "owner"
        owner_fields = {"agent_id": private.store.agent_id, "user_id": private.store.user_id}
        raw = Record(record_id="private_source", record_kind="raw_message", author_kind="human",
            **owner_fields,
            body_markdown="zxq_private_original_token", payload={"role": "user", "channel_source": {
                "platform_id": "fixture", "bot_id": "bot", "group_id": "owner",
                "conversation_kind": "private", "sender_id": "owner", "message_id": "origin"}})
        derived = Record(record_id="private_interpretation", record_kind="interpretation",
            author_kind="model", source_refs=[raw.ref], body_markdown="zxq_derived_private_token", **owner_fields,
            generator={"provider": "fixture", "model": "fixture-v1", "prompt_version": "test", "run_id": "private-run"},
            payload={"private_activity": True})
        private.store.commit([raw, derived], idempotency_key="seed-private", expected={raw.record_id: 0, derived.record_id: 0})
        result = group.recall.recall("zxq_private_original_token zxq_derived_private_token", budget=6000)
        assert raw.ref not in {record.ref for record in result.records}
        assert derived.ref not in {record.ref for record in result.records}
        from warmagent.tool_reading import read_page
        for ref in (raw.ref, derived.ref):
            with pytest.raises(ValueError, match="record_not_available_to_sender"):
                read_page(group.store, ref, group.store.snapshot(), {}, record_allowed=group.recall.record_allowed)
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_owner_direct_tool_turn_uses_reading_conversation_gateway(tmp_path):
    call = {"id": "fixture-call-1", "type": "function", "function": {
        "name": "harness__write", "arguments": '{"value":"hello"}'}}
    provider_response = [
        ModelResponse(provider="fixture", model="fixture-v1", content="",
            tool_calls=[call], provider_fields={"reasoning_content": "fixture reasoning"}),
        ModelResponse(provider="fixture", model="fixture-v1", content="已经处理好了。"),
    ]
    bridge, provider = make_bridge(tmp_path, provider_response, direct=True)
    async def handlers(_raw, *, context):
        async def execute_call(call_id, args):
            assert call_id == "fixture-call-1"
            return {"ok": True, "value": args["value"]}
        async def placeholder(**kwargs):
            raise AssertionError("expected execute_call")
        placeholder.execute_call = execute_call
        schema = {"type": "function", "function": {"name": "harness__write",
            "description": "fixture", "parameters": {"type": "object", "properties": {
                "value": {"type": "string"}}, "required": ["value"]}}}
        return {"harness__write": (schema, placeholder)}
    bridge.direct.handlers = handlers
    try:
        result = await bridge.receive(incoming("private", sender="owner", text="帮我写", message_id="direct1"))
        app = bridge.apps[incoming("private", sender="owner").scope]
        assert result["status"] == "prepared"
        assert app.conversation.__class__.__name__ == "ReadingConversation"
        assert app.conversation.gateway is app.gateway
        assert len(provider.requests) == 2
        assert provider.requests[1].messages[-1]["role"] == "tool"
        assert provider.requests[1].messages[-1]["tool_call_id"] == "fixture-call-1"
        assistant_call = next(message for message in provider.requests[1].messages if message.get("tool_calls"))
        assert assistant_call["reasoning_content"] == "fixture reasoning"
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_registered_owner_route_queues_and_claims_notification_only_to_private_target(tmp_path):
    bridge, _ = make_bridge(tmp_path)
    client = SimpleNamespace(settings=SimpleNamespace(group_enabled=True, private_enabled=True),
        group_id="room", private_id="owner", bot_id="bot", sent=[])
    async def send(text, target_id=None):
        client.sent.append((text, target_id))
    client.send = send
    client.close = lambda: asyncio.sleep(0)
    runner = WeChatRunner(bridge, client)
    try:
        route = incoming("private", sender="owner")
        bridge.register_route(route)
        bridge.activities.checkpoint(goal="整理", summary="整理到一半", protocol={"activity_id": "act-1"}, status="active")
        await bridge._activity_notify("整理有了进展")
        delivery = pending_deliveries(bridge.path, route.scope, kind="autonomy")[0]
        with sqlite3.connect(bridge.path) as db:
            envelope = json.loads(db.execute("SELECT envelope FROM deliveries WHERE id=?", (delivery["id"],)).fetchone()[0])
        assert envelope["conversation_kind"] == "private"
        assert envelope["group_id"] == "owner"
        assert envelope["sender_id"] == "owner"
        runner.scope_targets = {route.scope: "owner"}
        await runner.deliver(delivery, target_id="owner")
        assert client.sent == [("整理有了进展", "owner")]
        with sqlite3.connect(bridge.path) as db:
            status = db.execute("SELECT status FROM deliveries WHERE id=?", (delivery["id"],)).fetchone()[0]
        assert status == "sent"
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_owner_chat_pauses_activity_and_ack_starts_quiet_period_without_idle_call(tmp_path):
    bridge, provider = make_bridge(tmp_path, [ModelResponse(provider="fixture", model="fixture-v1", content="我在这里。")])
    try:
        stamp = time.time() + 601
        unavailable = await bridge.activities.tick(stamp)
        assert unavailable["reason"] == "unavailable"
        assert provider.requests == []
        bridge.activities.checkpoint(goal="整理资料", summary="已经整理两份", next_step="看第三份",
            protocol={"activity_id": "activity-chat"}, status="active")
        result = await bridge.receive(incoming("private", sender="owner", text="我回来了", message_id="owner-chat"))
        assert result["status"] == "prepared"
        assert bridge.activities.overview()["status"] == "paused"
        with sqlite3.connect(bridge.path) as db:
            envelope = json.loads(db.execute("SELECT envelope FROM deliveries WHERE id=?", (result["id"],)).fetchone()[0])
        assert envelope["activity_interruption"] == "已经整理两份"
        assert (await bridge.claim(result["id"]))["claimed"]
        await bridge.acknowledge(result["id"], "sent")
        last_chat = bridge.activities.overview()["last_chat"]
        assert last_chat is not None
        assert (await bridge.activities.tick(last_chat + 179))["reason"] == "paused"
        # It becomes eligible at 180 seconds, then offline capability gating prevents a model call.
        tick = await bridge.activities.tick(last_chat + 180)
        assert tick["reason"] == "unavailable"
        assert bridge.activities.overview()["status"] == "active"
        assert len(provider.requests) == 1
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_completed_activity_retains_history_but_next_opportunity_gets_new_id(tmp_path):
    bridge, _ = make_bridge(tmp_path)
    try:
        route = incoming("private", sender="owner")
        bridge.register_route(route)
        bridge.activities.checkpoint(goal="旧活动", summary="已完成", protocol={"activity_id": "old-id"}, status="completed")
        old = bridge.activities.overview()
        assert old["status"] == "completed"
        await bridge.activity_runner.step(None, prompt="fixture idle prompt")
        current = bridge.activities.overview()
        assert current["protocol"]["activity_id"] != old["protocol"]["activity_id"]
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_cancel_during_inflight_activity_step_is_not_overwritten_on_return(tmp_path):
    bridge, _ = make_bridge(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    async def runner(activity, *, prompt, reconcile_only):
        started.set()
        await release.wait()
        return {"status": "active", "summary": "step returned"}
    try:
        bridge.activities.available = lambda: {"online": True, "authorized": True,
            "busy": False, "budget_available": True}
        bridge.activities.run_step = runner
        bridge.activities.checkpoint(goal="进行中的活动", summary="执行中", status="active")
        step = asyncio.create_task(bridge.activities.tick(time.time() + 1))
        await started.wait()
        bridge.activities.stop()
        release.set()
        await step
        assert bridge.activities.overview()["status"] == "cancelled"
    finally:
        if not release.is_set():
            release.set()
        await bridge.close()
