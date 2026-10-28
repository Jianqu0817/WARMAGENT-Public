import pytest
import sqlite3

from warmagent.artifacts import ArtifactStore, Notifications


def test_artifact_scope_and_changed_hash_are_explicit(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    source = root / "result.txt"
    source.write_text("first", encoding="utf-8")
    store = ArtifactStore(tmp_path / "artifacts.sqlite3", root)
    registered = store.register("call-1", "group-a", source, "text/plain")
    assert store.resolve("group-a", registered["id"])["changed"] is False
    with pytest.raises(KeyError):
        store.resolve("group-b", registered["id"])
    assert store.share("group-a", registered["id"], "group-b")["id"] == registered["id"]
    assert store.resolve("group-b", registered["id"])["resolved_scope"] == "group-b"
    source.write_text("second", encoding="utf-8")
    changed = store.resolve("group-a", registered["id"])
    assert changed["changed"] is True
    updated = store.register("call-1", "group-a", source, "text/plain")
    assert updated["revision"] == 2 and updated["hash"] == changed["current_hash"]


def test_full_host_may_register_external_absolute_path_without_copy(tmp_path):
    root = tmp_path / "managed"
    external = tmp_path / "elsewhere.txt"
    root.mkdir()
    external.write_text("outside root", encoding="utf-8")
    with pytest.raises(ValueError):
        ArtifactStore(tmp_path / "restricted.sqlite3", root).register("c", "g", external, "text/plain")
    store = ArtifactStore(tmp_path / "full.sqlite3", root, allow_external=True)
    registered = store.register("c", "g", external, "text/plain")
    resolved = store.resolve("g", registered["id"])
    assert resolved["path"] == str(external.resolve())
    assert external.read_text(encoding="utf-8") == "outside root"


def test_notification_audience_is_independent_of_scope_and_ack_is_cas(tmp_path):
    path = tmp_path / "events.sqlite3"
    notices = Notifications(path)
    ai_event = notices.emit(scope="g", audience="ai", kind="ready", dedup_key="same", payload_ref="r1")
    assert notices.emit(scope="g", audience="ai", kind="ready", dedup_key="same", payload_ref="r1") == ai_event
    human_event = notices.emit(scope="g", audience="human", kind="ready", dedup_key="same", payload_ref="r1")
    assert human_event != ai_event
    claim = notices.get(scope="g", audience="ai", subscriber_token="ai-token")
    assert claim["event_id"] == ai_event
    # The AI poll inspected and skipped the human event without leasing it.
    assert notices.get(scope="g", audience="human", subscriber_token="human-token")["event_id"] == human_event
    assert notices.ack(scope="g", event_id=ai_event, subscriber_token="ai-token",
                       claim_token="stale-token") is False
    assert notices.ack(scope="g", event_id=ai_event, subscriber_token="ai-token",
                       claim_token=claim["claim_token"]) is True


def test_artifact_connection_closes_and_data_survives_reopen(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    artifact_file = root / "a.txt"
    artifact_file.write_text("persisted", encoding="utf-8")
    store = ArtifactStore(tmp_path / "artifacts.sqlite3", root)
    item = store.register("call", "scope", artifact_file, "text/plain")
    with store._db() as db:
        saved = db
        assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
    with pytest.raises(sqlite3.ProgrammingError):
        saved.execute("SELECT 1")
    assert ArtifactStore(tmp_path / "artifacts.sqlite3", root).resolve("scope", item["id"])["changed"] is False

