import asyncio
import json
import sqlite3
from contextlib import closing

import pytest

from warmagent.personal_space import PersonalSpace


def test_notes_survive_restart_and_are_isolated_by_scope(tmp_path):
    path = tmp_path / "personal.sqlite3"
    space = PersonalSpace(path)
    space.put("owner-a", "idea-1", "idea", "聊聊新点子")
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    reopened = PersonalSpace(path)
    assert reopened.read("owner-a", "idea-1")["text"] == "聊聊新点子"
    assert reopened.get("owner-a", "idea-1")["revision"] == 1
    assert reopened.read("owner-b", "idea-1") is None
    assert reopened.list("owner-b") == []
    with pytest.raises(KeyError):
        reopened.get("owner-b", "idea-1")


def test_source_ref_requires_revision_and_scope_visibility(tmp_path):
    seen = []
    space = PersonalSpace(tmp_path / "personal.sqlite3",
                          allowed_source=lambda scope, ref: seen.append((scope, ref)) or ref == "mem-1@2")
    with pytest.raises(ValueError, match="id@positive_revision"):
        space.put("scope-a", "n1", "idea", "note", "mem-1@0")
    with pytest.raises(ValueError, match="not visible"):
        space.put("scope-a", "n1", "idea", "note", "mem-1@3")
    saved = space.put("scope-a", "n1", "idea", "note", "mem-1@2")
    assert saved["source_ref"] == "mem-1@2"
    assert seen == [("scope-a", "mem-1@3"), ("scope-a", "mem-1@2")]
    with pytest.raises(ValueError, match="not visible"):
        PersonalSpace(tmp_path / "without-checker.sqlite3").put(
            "scope-a", "n2", "idea", "note", "mem-1@2"
        )


def test_put_rejects_whitespace_only_text(tmp_path):
    space = PersonalSpace(tmp_path / "personal.sqlite3")
    with pytest.raises(ValueError, match="1..1200"):
        space.put("scope-a", "blank", "idea", "   \n\t")


def test_upsert_content_change_resets_sharing_and_archive(tmp_path):
    space = PersonalSpace(tmp_path / "personal.sqlite3")
    original = space.put("scope-a", "same", "idea", "first")
    first_revision = space.get("scope-a", "same")["revision"]
    assert first_revision == 1
    assert space.mark_shared("scope-a", ["same"], {"same": first_revision}) == ["same"]
    assert space.archive("scope-a", "same") is True
    assert space.archive("scope-a", "same") is True
    assert space.read("scope-a", "same") is None

    unchanged = space.put("scope-a", "same", "idea", "first")
    assert unchanged["archived"] is True
    assert unchanged["shared_at"] is not None
    updated = space.put("scope-a", "same", "question", "second")
    assert updated["created_at"] == original["created_at"]
    assert updated["archived"] is False and updated["shared_at"] is None
    assert updated["kind"] == "question" and updated["text"] == "second"
    assert updated["revision"] == 2


def test_mark_shared_is_idempotent_and_scope_bound(tmp_path):
    space = PersonalSpace(tmp_path / "personal.sqlite3")
    space.put("scope-a", "n1", "later", "bring this up")
    space.put("scope-b", "n1", "later", "separate note")
    assert space.mark_shared("scope-a", ["n1", "n1", "missing"]) == ["n1"]
    shared_at = space.read("scope-a", "n1")["shared_at"]
    assert space.mark_shared("scope-a", ["n1"]) == []
    assert space.read("scope-a", "n1")["shared_at"] == shared_at
    assert space.read("scope-b", "n1")["shared_at"] is None


def test_stale_share_receipt_does_not_mark_updated_note(tmp_path):
    space = PersonalSpace(tmp_path / "personal.sqlite3")
    first = space.put("scope-a", "n1", "idea", "prepared text")
    space.put("scope-a", "n1", "idea", "changed before send receipt")
    assert space.mark_shared("scope-a", ["n1"], {"n1": first["revision"]}) == []
    current = space.get("scope-a", "n1")
    assert current["revision"] == 2 and current["shared_at"] is None
    assert space.mark_shared("scope-a", ["n1"], {"n1": current["revision"]}) == ["n1"]


def test_context_is_subjective_bounded_prioritized_and_omits_archived(tmp_path):
    space = PersonalSpace(tmp_path / "personal.sqlite3")
    space.put("scope-a", "shared", "idea", "已分享的短想法")
    space.mark_shared("scope-a", ["shared"])
    space.put("scope-a", "open", "question", "未分享的问题" + "暖" * 500)
    space.put("scope-a", "old", "later", "归档内容")
    space.archive("scope-a", "old")

    raw = space.context("scope-a", limit=3, max_chars=180)
    assert len(raw) <= 180
    payload = json.loads(raw)
    assert "subjective notes only" in payload["label"]
    assert payload["notes"][0]["note_id"] == "open"
    assert payload["notes"][0]["revision"] == 1
    assert payload["notes"][0]["shared"] is False
    assert all(note["note_id"] != "old" for note in payload["notes"])
    assert payload["notes"][0]["truncated"] is True

    complete = json.loads(space.context("scope-a", limit=3, max_chars=1000))
    by_id = {note["note_id"]: note for note in complete["notes"]}
    assert by_id["shared"]["shared"] is True
    assert by_id["shared"]["revision"] == 1


def test_personal_note_tool_schema_and_actual_calls(tmp_path):
    space = PersonalSpace(tmp_path / "personal.sqlite3")
    tools = space.handlers("scope-a")
    assert list(tools) == ["harness__personal_note"]
    schema, call = tools["harness__personal_note"]
    params = schema["function"]["parameters"]
    assert params["properties"]["action"]["enum"] == ["write", "list", "read", "archive"]
    assert "shared_at" not in params["properties"]
    assert "想留到下次聊" in schema["function"]["description"]
    assert params["properties"]["source_ref"]["anyOf"][0] == {"const": ""}

    saved = asyncio.run(call(action="write", note_id="n1", kind="reflection", text="再聊這件事"))
    assert saved["note_id"] == "n1" and saved["scope"] == "scope-a"
    assert asyncio.run(call(action="read", note_id="n1"))["text"] == "再聊這件事"
    other_call = space.handlers("scope-b")["harness__personal_note"][1]
    assert asyncio.run(other_call(action="read", note_id="n1")) is None
    notes = asyncio.run(call(action="list", limit=1))
    assert notes[0]["text"] == "再聊這件事"
    assert asyncio.run(call(action="archive", note_id="n1")) == {"archived": True}
    assert asyncio.run(call(action="archive", note_id="n1")) == {"archived": True}
