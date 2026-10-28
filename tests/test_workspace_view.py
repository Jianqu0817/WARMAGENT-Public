import asyncio
import sqlite3

import pytest

from warmagent.workspace_view import WorkspaceConflict, WorkspaceView


def test_revision_cas_restart_and_source_refs_are_preserved(tmp_path):
    path = tmp_path / "view.sqlite3"
    view = WorkspaceView(path)
    view.register_context("g1", "chat", "receipt-1", summary="摘要", observed=True)
    assert view.pin("g1", "chat", "receipt-1", 0) == 1
    assert view.describe("g1", "chat")["mode"] == "auto"
    with pytest.raises(WorkspaceConflict):
        view.note("g1", "chat", "current_goal", "stale", 0)
    restarted = WorkspaceView(path)
    assert restarted.describe("g1", "chat")["revision"] == 1
    assert restarted.list("g1", "chat")["items"][0]["ref"] == "receipt-1"
    assert restarted.fold("g1", "chat", "receipt-1", 1) == 2
    assert restarted.describe("g1", "chat")["mode"] == "fold"
    assert "receipt-1" in restarted.compile("g1", "chat")
    assert restarted.unfold("g1", "chat", "receipt-1", 2) == 3
    assert restarted.describe("g1", "chat")["mode"] == "expand"
    assert "receipt-1" in restarted.compile(
        "g1", "chat"
    ) or "摘要" in restarted.compile("g1", "chat")


def test_unobserved_error_and_latest_image_cannot_fold_and_fork_is_copy(tmp_path):
    view = WorkspaceView(tmp_path / "view.sqlite3")
    view.register_context("g", "main", "pending", summary="safe")
    view.register_context(
        "g", "main", "error", summary='{"error":"failed"}', observed=True
    )
    view.register_context(
        "g", "main", "image", summary='{"image_manifest":{}}', observed=True
    )
    view.register_context("g", "main", "latest-image", observed=True, has_image=True)
    with pytest.raises(ValueError):
        view.fold("g", "main", "pending", 0)
    with pytest.raises(ValueError):
        view.fold("g", "main", "error", 0)
    with pytest.raises(ValueError):
        view.fold("g", "main", "image", 0)
    with pytest.raises(ValueError):
        view.fold("g", "main", "latest-image", 0)
    branch = view.fork("g", "main", "draft", 0)
    assert branch["revision"] == 0
    assert {item["ref"] for item in view.list("g", "draft")["items"]} == {
        "pending",
        "error",
        "image",
        "latest-image",
    }


def test_handlers_bind_scope_and_validate_references(tmp_path):
    view = WorkspaceView(tmp_path / "view.sqlite3")
    view.register_context("g", "chat", "ok", observed=True)
    handlers = view.handlers("g", "chat", lambda ref: ref == "ok")
    schema, invoke = handlers["harness__workspace_pin"]
    assert "scope" not in schema["function"]["parameters"]["properties"]
    assert asyncio.run(invoke(ref="other", revision=0)) == {"error": "ref_not_allowed"}
    assert asyncio.run(invoke(ref="ok", revision=0)) == {"revision": 1}


def test_list_is_bounded_and_paginates(tmp_path):
    view = WorkspaceView(tmp_path / "view.sqlite3")
    for index in range(13):
        view.register_context("g", "chat", f"ref-{index}")
    first = view.list("g", "chat")
    second = view.list("g", "chat", offset=10)
    assert len(first["items"]) == 10 and first["next_offset"] == 10
    assert len(second["items"]) == 3 and second["next_offset"] is None


def test_connections_close_and_contains_uses_scoped_suffix_lookup(tmp_path):
    view = WorkspaceView(tmp_path / "view.sqlite3")
    view.register_context("g", "chat", "turn-5:call-id")
    assert view.contains("g", "chat", "turn-5:call-id")
    assert view.contains("g", "chat", "call-id")
    assert not view.contains("other", "chat", "call-id")
    with view._db() as db:
        saved = db
        assert db.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(sqlite3.ProgrammingError):
        saved.execute("SELECT 1")
    assert WorkspaceView(tmp_path / "view.sqlite3").contains("g", "chat", "call-id")
