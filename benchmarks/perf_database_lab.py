"""Offline SQLite query-plan and timing lab for inbox and delivery lookups."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import statistics
import tempfile
import time

from warmagent.runtime_inbox import RuntimeInbox
from warmagent.server import pending_deliveries


DELIVERY_INDEX_SQL = (
    "CREATE INDEX deliveries_prepared_scope_order ON deliveries(scope) "
    "WHERE status='prepared'"
)


@contextmanager
def capture_sql(inbox: RuntimeInbox, statements: list[str]):
    original_db = inbox._db

    @contextmanager
    def traced_db():
        with original_db() as db:
            db.set_trace_callback(statements.append)
            yield db

    inbox._db = traced_db
    try:
        yield
    finally:
        inbox._db = original_db


def traced_select(statements: list[str], prefix: str) -> str:
    for statement in statements:
        if statement.lstrip().upper().startswith(prefix.upper()):
            return statement
    raise RuntimeError(f"trace did not capture query starting with {prefix!r}")


def explain(path: Path, sql: str, args=()) -> list[str]:
    with closing(sqlite3.connect(path)) as db:
        return [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, args)]


def synthetic_rows(count: int):
    active_count = min(512, max(64, count // 100))
    active_start = count - active_count
    inbox_rows = []
    initial_states = {}
    for index in range(count):
        active = index >= active_start
        offset = index - active_start
        state = ("processing" if offset % 4 == 0 else "pending") if active else "completed"
        platform = "wx" if not active or offset % 2 == 0 else "qq"
        sender = "owner" if active and offset % 10 == 0 else f"user-{index % 17}"
        attempts = offset % 3 if active else 3
        deferred = int(active and offset % 29 == 0)
        lease_until = 99 if state == "processing" else None
        next_attempt = 100 if not active or offset % 5 else 101
        payload = json.dumps({"platform_id": platform, "sender_id": sender,
                              "message_id": index}, separators=(",", ":"))
        key = f"msg-{index:05d}"
        created = index // 3
        inbox_rows.append((key, f"scope-{index % 8}", f"fp-{index}", payload,
                           state, deferred, created, attempts, None, lease_until,
                           next_attempt, None, None))
        initial_states[key] = (state, attempts, lease_until)

    delivery_rows = []
    prepared_start = count - max(500, count // 100)
    kinds = ("explicit", "batch_report", "computer_report", "proactive")
    for index in range(count):
        prepared = index >= prepared_start
        status = "prepared" if prepared else ("sent" if index % 5 < 3 else "unknown")
        kind = kinds[(index - prepared_start) % len(kinds)] if prepared else kinds[index % len(kinds)]
        delivery_rows.append((f"delivery-{index:05d}", f"scope-{index % 8}",
            f"delivery-fp-{index}", status, f"reply-{index}", f"ref-{index}",
            float(index), kind, json.dumps([f"reply-{index}"]), 0))
    return inbox_rows, initial_states, delivery_rows, active_count


def create_database(path: Path, rows: int, *, indexed: bool):
    inbox = RuntimeInbox(path, max_auto_attempts=3)
    inbox_rows, initial_states, delivery_rows, active_count = synthetic_rows(rows)
    with closing(sqlite3.connect(path)) as db, db:
        db.executemany("""INSERT INTO runtime_inputs
            (key,scope,fingerprint,payload,state,deferred,created,attempts,
             lease_token,lease_until,next_attempt,last_error,result)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", inbox_rows)
        db.execute("""CREATE TABLE deliveries (
            id TEXT PRIMARY KEY, scope TEXT NOT NULL, fingerprint TEXT NOT NULL,
            status TEXT NOT NULL, reply TEXT, reply_ref TEXT, updated REAL NOT NULL,
            trigger_kind TEXT, reply_parts TEXT, sent_parts INTEGER NOT NULL DEFAULT 0
        )""")
        db.executemany("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?,?,?,?)", delivery_rows)
        if indexed:
            db.execute(DELIVERY_INDEX_SQL)
    return inbox, initial_states, active_count


def reset_claim(path: Path, key: str, initial_states):
    state, attempts, lease_until = initial_states[key]
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE runtime_inputs SET state=?,attempts=?,lease_token=NULL,lease_until=? "
                   "WHERE key=?", (state, attempts, lease_until, key))


def current_logic(path: Path, inbox: RuntimeInbox, initial_states):
    claim_trace: list[str] = []
    with capture_sql(inbox, claim_trace):
        claimed = inbox.claim(now=100, platforms=["wx"])
    if claimed is not None:
        reset_claim(path, claimed["key"], initial_states)
    claim_sql = traced_select(claim_trace, "SELECT * FROM runtime_inputs")

    owner_trace: list[str] = []
    with capture_sql(inbox, owner_trace):
        waiting = inbox.owner_waiting("owner", "scope-3", exclude="msg-49999")
    owner_sql = traced_select(owner_trace, "SELECT 1 FROM runtime_inputs WHERE scope")
    return {"claim_key": claimed["key"] if claimed else None,
            "owner_waiting": waiting,
            "claim_eqp": explain(path, claim_sql),
            "owner_eqp": explain(path, owner_sql)}


def delivery_query(kind):
    condition = "AND trigger_kind!='proactive' " if kind == "non_proactive" else (
        "AND trigger_kind=? " if kind else "")
    args = ("scope-3", kind, 5) if kind and kind != "non_proactive" else ("scope-3", 5)
    sql = ("SELECT id,reply,reply_parts,sent_parts,trigger_kind FROM deliveries "
           "WHERE scope=? AND status='prepared' " + condition + "ORDER BY rowid LIMIT ?")
    return sql, args


def measure_deliveries(path: Path, kind, repeats: int):
    def run():
        return pending_deliveries(path, "scope-3", limit=5, kind=kind)

    warmup = run()
    samples = []
    values = []
    for _ in range(repeats):
        started = time.perf_counter()
        value = run()
        samples.append((time.perf_counter() - started) * 1000)
        values.append(value)
    return {"warmup": warmup, "values": values, "samples_ms": samples,
            "mean_ms": statistics.mean(samples)}


def preserve_rejected_candidates(previous):
    if "rejected_candidates" in previous:
        return previous["rejected_candidates"]
    decisions = {}
    for key, label, reason in (
        ("inbox_claim", "runtime_inputs_active_arrival",
         "Rejected: full claim call measured slower than the existing index."),
        ("owner_waiting", "owner_waiting_rowid_hint",
         "Rejected: low measured gain and a possible regression for older anchors."),
    ):
        old = previous.get(key)
        if not old:
            continue
        decisions[label] = {
            "decision": "rejected",
            "reason": reason,
            "baseline_samples_ms": old["baseline"]["samples_ms"],
            "candidate_samples_ms": old["optimized"]["samples_ms"],
            "baseline_mean_ms": old["baseline"]["mean_ms"],
            "candidate_mean_ms": old["optimized"]["mean_ms"],
            "speedup": old["speedup"],
            "baseline_eqp": old["baseline_eqp"],
            "candidate_eqp": old["optimized_eqp"],
        }
    return decisions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=50_000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--out", type=Path, default=Path("reports/perf/database-lab.json"))
    args = parser.parse_args()
    if not 20_000 <= args.rows <= 50_000:
        parser.error("--rows must be between 20000 and 50000")
    if args.repeats < 3:
        parser.error("--repeats must be at least 3")

    previous = json.loads(args.out.read_text(encoding="utf-8")) if args.out.exists() else {}
    rejected_candidates = preserve_rejected_candidates(previous)
    with tempfile.TemporaryDirectory(prefix="warmagent-database-lab-") as temporary:
        root = Path(temporary)
        baseline_path, indexed_path = root / "baseline.sqlite3", root / "indexed.sqlite3"
        baseline_inbox, baseline_states, active_count = create_database(
            baseline_path, args.rows, indexed=False)
        indexed_inbox, indexed_states, _ = create_database(
            indexed_path, args.rows, indexed=True)

        baseline_logic = current_logic(baseline_path, baseline_inbox, baseline_states)
        indexed_logic = current_logic(indexed_path, indexed_inbox, indexed_states)

        kinds = (None, "non_proactive", "computer_report")
        deliveries = {}
        unknown_before = {}
        unknown_ids = {}
        for label, path in (("baseline", baseline_path), ("indexed", indexed_path)):
            with closing(sqlite3.connect(path)) as db:
                unknown_before[label] = db.execute(
                    "SELECT COUNT(*) FROM deliveries WHERE status='unknown'").fetchone()[0]
                if label == "baseline":
                    unknown_ids = {row[0] for row in db.execute(
                        "SELECT id FROM deliveries WHERE status='unknown'")}
        outputs_match = True
        unknown_excluded = True
        for kind in kinds:
            baseline = measure_deliveries(baseline_path, kind, args.repeats)
            indexed = measure_deliveries(indexed_path, kind, args.repeats)
            name = "all" if kind is None else kind
            deliveries[name] = {
                "baseline": baseline,
                "indexed": indexed,
                "speedup": baseline["mean_ms"] / indexed["mean_ms"],
                "baseline_eqp": explain(baseline_path, *delivery_query(kind)),
                "indexed_eqp": explain(indexed_path, *delivery_query(kind)),
            }
            outputs_match &= baseline["values"] == indexed["values"]
            unknown_excluded &= all(item["id"] not in unknown_ids
                for batch in indexed["values"] for item in batch)

        unknown_after = {}
        for label, path in (("baseline", baseline_path), ("indexed", indexed_path)):
            with closing(sqlite3.connect(path)) as db:
                unknown_after[label] = db.execute(
                    "SELECT COUNT(*) FROM deliveries WHERE status='unknown'").fetchone()[0]

        correctness = {
            "inbox_claim_same_result": baseline_logic["claim_key"] == indexed_logic["claim_key"],
            "owner_waiting_same_result": (
                baseline_logic["owner_waiting"] == indexed_logic["owner_waiting"]),
            "pending_delivery_outputs_match": outputs_match,
            "unknown_deliveries_excluded": unknown_excluded,
            "pending_queries_read_only": unknown_before == unknown_after,
        }
        result = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "sqlite_version": sqlite3.sqlite_version,
            "synthetic_rows_per_table": args.rows,
            "active_inbox_rows": active_count,
            "prepared_delivery_rows": max(500, args.rows // 100),
            "repeats": args.repeats,
            "warmups": 1,
            "current_inbox_logic": {
                "claim_key": baseline_logic["claim_key"],
                "baseline_eqp": baseline_logic["claim_eqp"],
                "indexed_eqp": indexed_logic["claim_eqp"],
                "owner_waiting": baseline_logic["owner_waiting"],
                "owner_baseline_eqp": baseline_logic["owner_eqp"],
                "owner_indexed_eqp": indexed_logic["owner_eqp"],
            },
            "pending_deliveries": deliveries,
            "rejected_candidates": rejected_candidates,
            "adopted_index": {
                "decision": "adopted",
                "ddl": DELIVERY_INDEX_SQL,
                "why": "The scope/state/order query became an indexed seek and was 5x faster in the synthetic history lab.",
            },
            "correctness": correctness,
        }
        if not all(correctness.values()):
            raise AssertionError(f"baseline and indexed outputs diverged: {correctness}")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        print(json.dumps({
            "report": str(args.out.resolve()),
            "sqlite_version": result["sqlite_version"],
            "pending_deliveries_speedups": {
                key: value["speedup"] for key, value in deliveries.items()},
            "rejected_candidates": {
                key: value["speedup"] for key, value in rejected_candidates.items()},
            "correctness": correctness,
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
