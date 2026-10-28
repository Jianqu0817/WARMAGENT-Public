import sqlite3

from warmagent.runtime_events import EventJournal


def _publish(journal, *, scope="group-a", source="chat", dedup_key="m1", occurred_at=1):
    return journal.publish(kind="message", scope=scope, source=source,
                           dedup_key=dedup_key, occurred_at=occurred_at)


def test_publish_deduplicates_and_survives_restart(tmp_path):
    path = tmp_path / "events.sqlite3"
    first = EventJournal(path)
    event_id = _publish(first)
    assert _publish(first) == event_id
    second = EventJournal(path)
    assert _publish(second) == event_id
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM runtime_events").fetchone()[0] == 1


def test_claim_is_per_consumer_and_scope_isolated(tmp_path):
    journal = EventJournal(tmp_path / "events.sqlite3")
    event_id = _publish(journal)
    _publish(journal, scope="group-b", dedup_key="m2", occurred_at=2)
    first = journal.claim("worker-a", "group-a", now=10)
    assert first["event_id"] == event_id
    assert journal.claim("worker-a", "group-b", now=10)["scope"] == "group-b"
    assert journal.claim("worker-a", "group-a", now=10) is None
    assert journal.claim("worker-b", "group-a", now=10)["event_id"] == event_id


def test_expired_claim_cannot_ack_new_claim(tmp_path):
    journal = EventJournal(tmp_path / "events.sqlite3")
    event_id = _publish(journal)
    old = journal.claim("worker", "group-a", now=10, lease_seconds=5)
    current = journal.claim("worker", "group-a", now=15, lease_seconds=5)
    assert current["event_id"] == event_id
    assert current["claim_token"] != old["claim_token"]
    assert journal.ack(event_id, "worker", old["claim_token"], now=16) is False
    assert journal.ack(event_id, "worker", current["claim_token"], now=16) is True
    assert journal.claim("worker", "group-a", now=16) is None
    assert journal.summary("group-a", consumer="worker") == {
        "message": {"pending": 0, "acknowledged": 1}}


def test_summary_counts_unclaimed_events_as_pending(tmp_path):
    journal = EventJournal(tmp_path / "events.sqlite3")
    _publish(journal)
    assert journal.summary("group-a", consumer="worker") == {
        "message": {"pending": 1, "acknowledged": 0}}


def test_claim_can_target_event_but_never_cross_scope(tmp_path):
    journal = EventJournal(tmp_path / "events.sqlite3")
    wanted = _publish(journal, scope="group-a", dedup_key="a", occurred_at=1)
    _publish(journal, scope="group-a", dedup_key="b", occurred_at=2)
    other_scope = _publish(journal, scope="group-b", dedup_key="c", occurred_at=3)
    claimed = journal.claim("worker", "group-a", event_id=wanted, now=10)
    assert claimed["event_id"] == wanted
    assert journal.claim("worker", "group-a", event_id=other_scope, now=10) is None
