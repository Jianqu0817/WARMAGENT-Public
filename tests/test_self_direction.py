import asyncio
import sqlite3
import time

import pytest

from warmagent.self_direction import SelfDirection


def test_mode_deadline_survives_restart_then_returns_to_working(tmp_path):
    path = tmp_path / "direction.sqlite3"
    first = SelfDirection(path)
    start = time.time()
    deadline = start + 30
    first.set_mode("group-a", "sleeping", deadline)
    assert first.ready("group-a", start + 10) is False
    restarted = SelfDirection(path)
    assert restarted.overview("group-a")["mode"] == "sleeping"
    assert restarted.ready("group-a", deadline) is True
    state = restarted.overview("group-a")
    assert state["mode"] == "working"
    assert state["until_epoch"] is None
    assert restarted.ready("group-b", start + 10) is True


def test_interests_keep_source_status_and_scope_after_restart(tmp_path):
    path = tmp_path / "direction.sqlite3"
    state = SelfDirection(path, allowed_source=lambda scope, ref: ref.startswith(f"{scope}-"))
    card = state.add_interest("group-a", "language_practice", "聊语言练习", "group-a-event@2")
    assert card["source_ref"] == "group-a-event@2"
    assert card["kind"] == "self_reported_interest_candidate"
    assert card["is_fact"] is False and card["is_persona"] is False
    state.set_interest_status("group-a", "language_practice", "paused")
    with pytest.raises(ValueError):
        state.add_interest("group-a", "bad", "outside", "group-b-event@1")
    restarted = SelfDirection(path)
    recovered = restarted.cards("group-a", "语言", 5)
    assert recovered[0]["source_ref"] == "group-a-event@2"
    assert recovered[0]["status"] == "paused"
    assert restarted.cards("group-b") == []


def test_interest_writes_return_exact_id_when_fuzzy_match_is_newer(tmp_path):
    state = SelfDirection(tmp_path / "direction.sqlite3")
    state.add_interest("g", "other", "alpha note", "g-other@1")
    with sqlite3.connect(state.path) as db:
        db.execute("UPDATE direction_interest SET updated_at=? WHERE scope=? AND interest_id=?",
                   (time.time() + 600, "g", "other"))

    added = state.add_interest("g", "a", "target", "g-a@1")
    assert added["interest_id"] == "a"
    assert state.cards("g", "a", 1)[0]["interest_id"] == "other"
    assert state.set_interest_status("g", "a", "paused")["interest_id"] == "a"
    updated = state.update_interest("g", "a", progress="continued")
    assert updated["interest_id"] == "a"
    assert updated["progress"] == "continued"


def test_legacy_interest_rows_migrate_and_keep_source_through_updates(tmp_path):
    path = tmp_path / "direction.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TABLE direction_interest (
                scope TEXT NOT NULL, interest_id TEXT NOT NULL, description TEXT NOT NULL,
                source_ref TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
                updated_at REAL NOT NULL, PRIMARY KEY(scope, interest_id)
            )
        """)
        db.execute("INSERT INTO direction_interest VALUES(?,?,?,?,?,?,?)",
                   ("group-a", "reading", "读小说", "group-a-event@3", "active", 1, 1))

    state = SelfDirection(path)
    migrated = state.cards("group-a")[0]
    assert migrated["motivation"] == migrated["progress"] == migrated["next_step"] == ""
    assert state.update_interest("group-a", "reading", progress="读到第三章", next_step="周末继续")[
        "source_ref"] == "group-a-event@3"

    restarted = SelfDirection(path)
    result = restarted.update_interest("group-a", "reading", motivation="喜欢人物关系", status="paused")
    assert result["source_ref"] == "group-a-event@3"
    assert result["progress"] == "读到第三章"
    assert result["next_step"] == "周末继续"
    assert result["motivation"] == "喜欢人物关系"
    assert result["status"] == "paused"
    with pytest.raises(KeyError):
        restarted.update_interest("group-b", "reading", progress="不应跨范围修改")


def test_activity_context_is_bounded_ordered_and_keeps_cards_compatibility(tmp_path):
    state = SelfDirection(tmp_path / "direction.sqlite3")
    state.set_mode("group-a", "resting", time.time() + 600)
    state.add_interest("group-a", "active-old", "旧兴趣" * 100, "group-a-old@1",
                       motivation="原因" * 100, next_step="下一步" * 100)
    state.add_interest("group-a", "active-new", "新兴趣", "group-a-new@1")
    state.add_interest("group-a", "paused", "暂停兴趣", "group-a-paused@1")
    state.add_interest("group-a", "completed", "已完成", "group-a-done@1")
    state.set_interest_status("group-a", "paused", "paused")
    state.set_interest_status("group-a", "completed", "completed")
    with sqlite3.connect(state.path) as db:
        db.execute("UPDATE direction_interest SET updated_at=1 WHERE interest_id='active-old'")
        db.execute("UPDATE direction_interest SET updated_at=2 WHERE interest_id='active-new'")
        db.execute("UPDATE direction_interest SET updated_at=3 WHERE interest_id='paused'")

    context = state.activity_context("group-a")
    assert set(context) == {"mode", "until_epoch", "interests"}
    assert context["mode"] == "resting"
    assert [item["interest_id"] for item in context["interests"]] == ["active-new", "active-old", "paused"]
    assert set(context["interests"][0]) == {
        "interest_id", "description", "motivation", "progress", "next_step", "status", "source_ref"
    }
    clipped = context["interests"][1]
    assert len(clipped["description"]) == 160
    assert len(clipped["motivation"]) == 100
    assert len(clipped["next_step"]) == 100
    assert clipped["source_ref"] == "group-a-old@1"
    assert {card["interest_id"] for card in state.cards("group-a", limit=10)} == {
        "active-old", "active-new", "paused", "completed"
    }
    assert "recent_tool_outcomes" not in context


def test_interest_model_tool_schemas_and_update_call(tmp_path):
    state = SelfDirection(tmp_path / "direction.sqlite3")
    handlers = state.handlers("group-a")
    add_schema = handlers["harness__direction_add_interest"][0]["function"]["parameters"]
    assert {"motivation", "next_step"} <= set(add_schema["properties"])
    assert "motivation" not in add_schema["required"]
    update_schema, update_call = handlers["harness__direction_update_interest"]
    parameters = update_schema["function"]["parameters"]
    assert parameters["required"] == ["interest_id"]
    assert parameters["properties"]["progress"]["maxLength"] == 500
    assert parameters["properties"]["status"]["enum"] == ["active", "completed", "paused", "retired"]

    state.add_interest("group-a", "walks", "散步", "group-a-event@1")
    updated = asyncio.run(update_call("walks", progress="本周两次", next_step="周末继续"))
    assert updated["progress"] == "本周两次"
    assert updated["next_step"] == "周末继续"
    assert updated["source_ref"] == "group-a-event@1"


def test_interest_note_fields_enforce_500_character_limit(tmp_path):
    state = SelfDirection(tmp_path / "direction.sqlite3")
    with pytest.raises(ValueError):
        state.add_interest("g", "bad", "测试", "g-event@1", motivation="x" * 501)
    state.add_interest("g", "ok", "测试", "g-event@1", motivation="x" * 500)
    with pytest.raises(ValueError):
        state.update_interest("g", "ok", progress="x" * 501)
    with pytest.raises(ValueError):
        state.update_interest("g", "ok", status="not-a-status")


def test_tool_unknown_is_not_goal_success_and_silence_does_not_create_rejection(tmp_path):
    state = SelfDirection(tmp_path / "direction.sqlite3")
    result = state.record_outcome("private-a", "private-a-tool@1", "read_file", "unknown", "timeout")
    assert result["status"] == "unknown"
    assert result["goal_success"] is False
    overview = state.overview("private-a")
    assert overview["recent_tool_outcomes"][0]["goal_success"] is False
    with sqlite3.connect(state.path) as db:
        assert db.execute("SELECT COUNT(*) FROM direction_feedback").fetchone()[0] == 0
    handlers = state.handlers("private-a")
    assert "explicit_feedback" not in " ".join(handlers)
    assert all("feedback" not in name for name in handlers)


def test_explicit_feedback_is_separate_and_share_cooldown_is_host_gated(tmp_path):
    state = SelfDirection(tmp_path / "direction.sqlite3")
    t0 = time.time()
    state.set_share_interval("g", 60)
    assert state.share_ready("g", t0)
    state.mark_shared("g", t0)
    assert not state.share_ready("g", t0 + 30)
    with pytest.raises(ValueError):
        state.mark_shared("g", t0 + 30)
    assert state.share_ready("g", t0 + 60)
    feedback = state.explicit_feedback("g", "g-event@1", accepted=True)
    assert feedback["accepted"] is True
    assert all(not name.endswith("feedback") for name in state.handlers("g"))


def test_replaying_outbox_timestamps_does_not_extend_share_cooldown(tmp_path):
    state = SelfDirection(tmp_path / 'direction.sqlite3')
    state.set_share_interval('g', 60)
    state.observe_share_receipt('g', 1000)
    state.observe_share_receipt('g', 1000)
    state.observe_share_receipt('g', 900)
    assert state.overview('g')['last_shared_at'] == 1000
    assert state.share_ready('g', 1060)
    assert all('observe_share_receipt' not in name for name in state.handlers('g'))
