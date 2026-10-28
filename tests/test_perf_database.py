from contextlib import closing
import sqlite3

import pytest

from warmagent.runtime_inbox import RuntimeInbox
from warmagent.server import pending_deliveries
from test_warm_runtime import make_bridge


def test_inbox_claim_preserves_arrival_and_owner_waiting(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    inbox = RuntimeInbox(path)
    first_payload = {"platform_id": "wx", "sender_id": "owner", "text": "first"}
    second_payload = {"platform_id": "wx", "sender_id": "owner", "text": "second"}
    inbox.enqueue("z-arrived-first", "scope", "fp-1", first_payload, now=1)
    inbox.enqueue("a-arrived-second", "scope", "fp-2", second_payload, now=1)

    claimed = inbox.claim(now=2, platforms=["wx"])

    assert claimed["key"] == "z-arrived-first"
    assert claimed["state"] == "processing" and claimed["attempts"] == 1
    assert inbox.owner_waiting("owner", "scope", exclude=claimed["key"])
    assert inbox.owner_waiting("owner", "scope", exclude="missing-anchor")
    with closing(sqlite3.connect(path)) as db:
        names = {row[1] for row in db.execute("PRAGMA index_list(runtime_inputs)")}
    assert "runtime_inputs_claim" in names
    assert "runtime_inputs_active_arrival" not in names


@pytest.mark.asyncio
async def test_bridge_delivery_index_keeps_pending_scope_state_order_and_unknown(tmp_path):
    bridge, _ = make_bridge(tmp_path)
    try:
        path = bridge.path
        with closing(sqlite3.connect(path)) as db:
            names = {row[1] for row in db.execute("PRAGMA index_list(deliveries)")}
            assert "deliveries_prepared_scope_order" in names
        with closing(sqlite3.connect(path)) as db, db:
            db.executemany("""INSERT INTO deliveries
                (id,scope,fingerprint,status,reply,updated,trigger_kind,reply_parts,sent_parts)
                VALUES (?,?,?,?,?,?,?,?,?)""", [
                ("z-first", "scope-a", "fp-1", "prepared", "first", 1,
                 "explicit", '["first"]', 0),
                ("a-second", "scope-a", "fp-2", "prepared", "second", 2,
                 "proactive", '["second"]', 0),
                ("b-third", "scope-a", "fp-3", "prepared", "third", 3,
                 "computer_report", '["third"]', 0),
                ("unknown", "scope-a", "unknown-fp", "unknown", "uncertain", 4,
                 "explicit", '["uncertain"]', 0),
                ("other-scope", "scope-b", "fp-5", "prepared", "other", 5,
                 "explicit", '["other"]', 0),
            ])
            unknown_before = db.execute(
                "SELECT COUNT(*) FROM deliveries WHERE status='unknown'").fetchone()[0]
            plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN "
                "SELECT id,reply,reply_parts,sent_parts,trigger_kind FROM deliveries "
                "WHERE scope=? AND status='prepared' ORDER BY rowid LIMIT ?", ("scope-a", 5))]

        all_pending = pending_deliveries(path, "scope-a", limit=2)
        non_proactive = pending_deliveries(path, "scope-a", limit=5,
                                           kind="non_proactive")
        reports = pending_deliveries(path, "scope-a", limit=5,
                                     kind="computer_report")

        assert [item["id"] for item in all_pending] == ["z-first", "a-second"]
        assert [item["id"] for item in non_proactive] == ["z-first", "b-third"]
        assert [item["id"] for item in reports] == ["b-third"]
        assert all(item["id"] != "unknown" for item in all_pending + non_proactive + reports)
        assert any("deliveries_prepared_scope_order" in step for step in plan)
        with closing(sqlite3.connect(path)) as db:
            unknown_after = db.execute(
                "SELECT COUNT(*) FROM deliveries WHERE status='unknown'").fetchone()[0]
        assert unknown_after == unknown_before
    finally:
        await bridge.close()
