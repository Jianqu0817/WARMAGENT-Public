from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from warmagent.runtime_inbox import InboxFull, RuntimeInbox


def test_restart_recovers_only_expired_lease(tmp_path):
    path = tmp_path / "events.sqlite3"
    inbox = RuntimeInbox(path)
    inbox.enqueue("a", "group", "fp", {"text": "hello"}, now=1)
    first = inbox.claim(key="a", now=10, lease_seconds=5)
    assert first["attempts"] == 1
    restarted = RuntimeInbox(path)
    assert restarted.claim(key="a", now=14) is None
    second = restarted.claim(key="a", now=15)
    assert second["lease_token"] != first["lease_token"]
    assert second["attempts"] == 2
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0


def test_concurrent_claims_are_unique(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    RuntimeInbox(path).enqueue("a", "g", "fp", {"v": 1}, now=1)

    def claim():
        return RuntimeInbox(path).claim(now=2)

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: claim(), range(8)))
    assert sum(item is not None for item in claims) == 1


def test_expired_or_replaced_token_cannot_mutate(tmp_path):
    inbox = RuntimeInbox(tmp_path / "inbox.sqlite3")
    inbox.enqueue("a", "g", "fp", {"v": 1}, now=1)
    old = inbox.claim(key="a", now=2, lease_seconds=2)
    new = inbox.claim(key="a", now=4, lease_seconds=2)
    assert not inbox.complete("a", old["lease_token"], {"status": "ok"}, now=4)
    assert not inbox.renew("a", old["lease_token"], now=4)
    assert not inbox.renew("a", new["lease_token"], now=7)
    assert not inbox.complete("a", new["lease_token"], {"status": "ok"}, now=7)


def test_scope_and_deferred_filtering(tmp_path):
    inbox = RuntimeInbox(tmp_path / "inbox.sqlite3")
    inbox.enqueue("normal", "a", "1", {}, now=1)
    inbox.enqueue("later", "b", "2", {}, deferred=True, now=2)
    assert inbox.claim(scope="b", now=3) is None
    assert inbox.claim(scope="a", now=3)["key"] == "normal"
    assert inbox.claim(scope="a", allow_deferred=True, now=3) is None
    assert inbox.claim(scope="b", allow_deferred=True, now=3)["key"] == "later"


def test_fingerprint_conflict_backpressure_and_old_key_retry(tmp_path):
    inbox = RuntimeInbox(tmp_path / "inbox.sqlite3", pending_limit=1)
    original = inbox.enqueue("a", "g", "fp", {"text": "first"}, now=1)
    with pytest.raises(ValueError):
        inbox.enqueue("a", "g", "different", {"text": "other"}, now=2)
    with pytest.raises(ValueError):
        inbox.enqueue("a", "other-scope", "fp", {"text": "other"}, now=2)
    with pytest.raises(InboxFull):
        inbox.enqueue("b", "g", "fp2", {}, now=2)
    assert inbox.enqueue("a", "g", "fp", {"text": "changed"}, deferred=True, now=3) == original
    assert inbox.claim(key="a", now=4)["payload"] == {"text": "first"}
    with pytest.raises(InboxFull):
        inbox.enqueue("b", "g", "fp2", {}, now=5)


def test_processing_counts_toward_limit_and_summary_scope(tmp_path):
    inbox = RuntimeInbox(tmp_path / "inbox.sqlite3", pending_limit=1)
    inbox.enqueue("deferred", "a", "1", {}, deferred=True, now=1)
    claim = inbox.claim(key="deferred", allow_deferred=True, now=2)
    with pytest.raises(InboxFull):
        inbox.enqueue("other", "b", "2", {}, now=3)
    assert inbox.summary("a", now=3) == {"pending": 0, "processing": 1, "completed": 0, "ignored": 0, "deferred": 1, "needs_attention": 0}
    assert inbox.complete("deferred", claim["lease_token"], {"status": "ok"}, now=4)
    assert inbox.summary("a")["deferred"] == 0
    assert inbox.summary("b", now=5) == {"pending": 0, "processing": 0, "completed": 0, "ignored": 0, "deferred": 0, "needs_attention": 0}


def test_auto_attempt_limit_and_explicit_retry(tmp_path):
    inbox = RuntimeInbox(tmp_path / "inbox.sqlite3", max_auto_attempts=3)
    payload = {"original": "preserved"}
    inbox.enqueue("a", "g", "fp", payload, now=1)

    for attempt in range(3):
        claim = inbox.claim(now=2 + attempt, lease_seconds=10)
        assert claim is not None
        assert inbox.retry("a", claim["lease_token"], "TemporaryError", now=2 + attempt, delay=0)

    assert inbox.claim(now=10) is None
    assert inbox.summary("g", now=10)["needs_attention"] == 1
    manual = inbox.claim(key="a", now=10)
    assert manual is not None
    assert manual["attempts"] == 4
    assert manual["payload"] == payload
    assert inbox.summary("g", now=10)["needs_attention"] == 0


def test_urgent_reserve_is_eight_slots_after_regular_capacity(tmp_path):
    inbox = RuntimeInbox(tmp_path / "inbox.sqlite3", pending_limit=1)
    inbox.enqueue("normal", "g", "normal", {}, now=1)
    with pytest.raises(InboxFull):
        inbox.enqueue("normal-2", "g", "normal-2", {}, now=2)

    for index in range(8):
        inbox.enqueue(f"urgent-{index}", "g", f"urgent-{index}", {}, urgent=True, now=2 + index)
    with pytest.raises(InboxFull):
        inbox.enqueue("urgent-overflow", "g", "overflow", {}, urgent=True, now=20)
    # A duplicate remains idempotent and consumes no additional capacity.
    assert inbox.enqueue("normal", "g", "normal", {"changed": True}, urgent=True, now=21)["payload"] == {}


def test_retry_keeps_payload_and_stores_only_error_category(tmp_path):
    inbox = RuntimeInbox(tmp_path / "inbox.sqlite3")
    payload = {"text": "keep exactly", "nested": [1, 2]}
    inbox.enqueue("a", "g", "fp", payload, now=1)
    claim = inbox.claim(key="a", now=2)
    assert inbox.retry("a", claim["lease_token"], "NetworkError: sensitive detail", now=3, delay=2)
    item = inbox.get("a")
    assert item["payload"] == payload
    assert item["last_error"] == "NetworkError"
    assert inbox.claim(key="a", now=4)["payload"] == payload


def test_complete_ignored_and_compact_summary(tmp_path):
    inbox = RuntimeInbox(tmp_path / "inbox.sqlite3")
    inbox.enqueue("a", "g", "fp", {"secret": "not in summary"}, now=1)
    claim = inbox.claim(key="a", now=2)
    assert inbox.complete("a", claim["lease_token"], {"status": "ignored", "reason": "policy"}, now=3)
    assert inbox.get("a")["state"] == "ignored"
    assert inbox.summary() == {"pending": 0, "processing": 0, "completed": 0, "ignored": 1, "deferred": 0, "needs_attention": 0}
    with pytest.raises(ValueError):
        inbox.complete("a", claim["lease_token"], {"payload": "not allowed"}, now=4)
