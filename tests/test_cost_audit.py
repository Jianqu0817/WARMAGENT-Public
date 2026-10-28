import json
import sqlite3

import warmagent.cost_audit as cost_audit
from warmagent.cost_audit import build_report, purpose_group


def _db(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE usage (id INTEGER PRIMARY KEY, payload TEXT)")
    db.execute("CREATE TABLE budget_reservations (id TEXT PRIMARY KEY, scope TEXT, day TEXT, "
               "reserved_tokens INTEGER, charged_tokens INTEGER, status TEXT, expires_at REAL, created_at REAL)")
    db.commit()
    db.close()


def _usage(trace, stamp, purpose="conversation", status="received", reservation="r1"):
    return {"trace_id": trace, "recorded_at": stamp, "purpose": purpose, "status": status,
            "model": "m", "budget_reservation_id": reservation,
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "prompt_cache_hit_tokens": 3}}


def _insert_usage(path, row):
    db = sqlite3.connect(path)
    db.execute("INSERT INTO usage(payload) VALUES (?)", (json.dumps(row),))
    db.commit()
    db.close()


def _insert_reservation(path, *, rid="r1", status="settled", amount=12, reserved=30, created=1792512000):
    db = sqlite3.connect(path)
    db.execute("INSERT INTO budget_reservations VALUES (?,?,?,?,?,?,?,?)",
               (rid, "scope", "2026-10-20", reserved, amount, status, created + 60, created))
    db.commit()
    db.close()


def test_cross_utc_day_is_grouped_by_beijing_day_hour(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    runtime = snapshot / "runtime.sqlite3"
    budget = snapshot / "budget.sqlite3"
    _db(runtime)
    _db(budget)
    _insert_usage(runtime, _usage("trace", "2026-10-20T16:30:00+00:00"))
    _insert_reservation(budget, created=1792512000)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"snapshots": [{"relative": "runtime.sqlite3"},
                                                   {"relative": "budget.sqlite3"}]}), encoding="utf-8")

    report = build_report(manifest=manifest)

    assert report["by_beijing_day"]["2026-10-21"]["requests"] == 1
    assert report["by_beijing_hour"]["2026-10-21 00:00"]["requests"] == 1


def test_trace_and_reservation_duplicates_are_counted_once_and_unknown_separate(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    runtime_a, runtime_b, budget_a, budget_b = [snapshot / f"{n}.sqlite3" for n in "abcd"]
    for path in (runtime_a, runtime_b, budget_a, budget_b):
        _db(path)
    row = _usage("same", "2026-10-20T16:30:00+00:00")
    _insert_usage(runtime_a, row)
    _insert_usage(runtime_b, row)
    _insert_usage(runtime_b, _usage("same", "2026-10-20T16:32:00+00:00", reservation="r3"))
    _insert_usage(runtime_b, _usage("uncertain", "2026-10-20T16:40:00+00:00", status="failed_unknown", reservation="r2"))
    _insert_reservation(budget_a, rid="r1", status="settled", amount=12)
    _insert_reservation(budget_b, rid="r1", status="settled", amount=12)
    _insert_reservation(budget_b, rid="r3", status="settled", amount=12)
    _insert_reservation(budget_b, rid="r2", status="unknown", amount=30)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"snapshots": [{"relative": x.name} for x in (runtime_a, runtime_b, budget_a, budget_b)]}), encoding="utf-8")

    report = build_report(manifest=manifest)

    assert report["usage"]["deduplicated_trace_reservation_events"] == 3
    assert report["usage"]["requests"] == 3
    assert report["budget_ledger"]["settled_charged_tokens"] == 24
    assert report["budget_ledger"]["unknown_tokens"] == 30
    assert report["budget_ledger"]["duplicate_reservation_ids"] == 1
    assert report["budget_ledger"]["by_purpose_model"]["conversation:m"]["settled_charged_tokens"] == 24
    assert report["budget_ledger"]["by_purpose_model"]["conversation:m"]["unknown_tokens"] == 30


def test_purpose_keeps_unknown_and_routes_retries_and_reports():
    assert purpose_group("conversation") == "chat"
    assert purpose_group("proactive_speak") == "proactive_chat"
    assert purpose_group("autonomous_activity") == "autonomous_activity"
    assert purpose_group("emotion_batch") == "memory_maintenance"
    assert purpose_group("conversation_schema_retry") == "retry"
    assert purpose_group("computer_report") == "tool_report"
    assert purpose_group("future_purpose") == "unknown"


def test_readers_close_read_only_connections(tmp_path, monkeypatch):
    path = tmp_path / "runtime.sqlite3"
    _db(path)
    _insert_usage(path, _usage("trace", "2026-10-20T16:30:00+00:00"))
    _insert_reservation(path)
    real_connect = cost_audit._connect
    closed = []

    class TrackedConnection:
        def __init__(self, db):
            self.db = db

        def execute(self, *args, **kwargs):
            return self.db.execute(*args, **kwargs)

        def close(self):
            closed.append(True)
            self.db.close()

    monkeypatch.setattr(cost_audit, "_connect", lambda p: TrackedConnection(real_connect(p)))

    cost_audit._tables(path)
    cost_audit._read_usage(path)
    cost_audit._read_reservations(path)

    assert len(closed) == 5


def test_peak_hour_table_is_left_closed_right_open(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    runtime = snapshot / "runtime.sqlite3"
    _db(runtime)
    for index, (utc_hour, reservation) in enumerate(((3, "r11"), (4, "r12"), (8, "r16"), (9, "r17"))):
        stamp = f"2026-10-20T{utc_hour:02d}:00:00+00:00"
        _insert_usage(runtime, _usage(f"t{index}", stamp, reservation=reservation))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"snapshots": [{"relative": "runtime.sqlite3"}]}), encoding="utf-8")

    report = build_report(manifest=manifest)
    markdown = cost_audit.markdown_report(report)

    assert "2026-10-20 12:00" in report["by_beijing_hour"]
    assert "2026-10-20 17:00" in report["by_beijing_hour"]
    peak_table = markdown.split("## 按小时", 1)[1].split("## 最大单次", 1)[0]
    assert "| 11:00 |" in peak_table
    assert "| 16:00 |" in peak_table
    assert "| 12:00 |" not in peak_table
    assert "| 17:00 |" not in peak_table
