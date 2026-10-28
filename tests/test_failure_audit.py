import json
import sqlite3

from warmagent.failure_audit import audit_snapshot, apply_stale_plan, plan_stale_inputs


def db(path, statements):
    conn = sqlite3.connect(path)
    conn.executescript(statements)
    conn.close()


def test_audit_is_read_only_and_keeps_unknown_separate(tmp_path):
    root = tmp_path / "snapshot"
    root.mkdir()
    db(root / "deliveries.sqlite3", """
        CREATE TABLE deliveries(id TEXT,status TEXT,trigger_kind TEXT,sent_parts INT,reply_parts TEXT);
        INSERT INTO deliveries VALUES('private-unknown','unknown','explicit',1,'[]');
        CREATE TABLE chat_batches(key TEXT,state TEXT,batch_index INT,last_error TEXT);
        INSERT INTO chat_batches VALUES('private-batch','blocked',2,'ProviderError');
    """)
    report = audit_snapshot(root)
    assert report["inventory"]["deliveries"]["unknown"] == 1
    unknown = next(x for x in report["categories"] if x.get("private_id") == "private-unknown")
    assert unknown["class"] == "unknown_effect_preserve"
    assert "绝不自动重发" in unknown["action"]
    # The audit opened the database in SQLite read-only mode.
    conn = sqlite3.connect(f"file:{(root / 'deliveries.sqlite3').as_posix()}?mode=ro", uri=True)
    assert conn.execute("SELECT status FROM deliveries").fetchone()[0] == "unknown"
    conn.close()


def test_audit_does_not_copy_payload_text(tmp_path):
    root = tmp_path / "snapshot"
    root.mkdir()
    db(root / "activities.sqlite3", """
        CREATE TABLE idle_activity(id INT,status TEXT,goal TEXT,summary TEXT,next_step TEXT,device TEXT,background INT,receipts TEXT);
        INSERT INTO idle_activity VALUES(1,'blocked','PRIVATE_TEXT','PRIVATE_TEXT','PRIVATE_TEXT','host',0,'[]');
        CREATE TABLE task_slots(task_id TEXT,status TEXT,filter_job TEXT,event TEXT);
    """)
    report = json.dumps(audit_snapshot(root), ensure_ascii=False)
    assert "PRIVATE_TEXT" not in report
    assert "activity-1" in report


def test_snapshot_audit_merges_all_runtime_databases_and_finds_protocol(tmp_path):
    root = tmp_path / "snapshot"
    root.mkdir()
    db(root / "events.sqlite3", """
        CREATE TABLE runtime_inputs(key TEXT,state TEXT,attempts INT,last_error TEXT,deferred INT);
        INSERT INTO runtime_inputs VALUES('turn-b','pending',3,'ProviderError',0);
    """)
    for name, status, with_protocol in (("lib-a", "failed", False), ("lib-b", "succeeded", True)):
        folder = root / name
        folder.mkdir()
        protocol_schema = "CREATE TABLE protocols(agent TEXT,user TEXT,turn_id TEXT,payload TEXT);" if with_protocol else ""
        protocol_insert = "INSERT INTO protocols VALUES('a','u','turn-b','{\"status\":\"active\"}');" if with_protocol else ""
        db(folder / "runtime.sqlite3", f"""
            CREATE TABLE jobs(id TEXT,role TEXT,status TEXT);
            CREATE TABLE schedule(name TEXT,value TEXT);
            INSERT INTO jobs VALUES('{name}-job','maintenance','{status}');
            {protocol_schema}{protocol_insert}
        """)
    report = audit_snapshot(root)
    assert report["inventory"]["maintenance_jobs"] == {"maintenance:failed": 1, "maintenance:succeeded": 1}
    pending = next(x for x in report["categories"] if x.get("private_id") == "turn-b")
    assert pending["active_protocol"] is True


def setup_cancel_state(root):
    root.mkdir(parents=True, exist_ok=True)
    db(root / "events.sqlite3", """
        CREATE TABLE runtime_inputs (
            key TEXT PRIMARY KEY, scope TEXT NOT NULL, fingerprint TEXT NOT NULL,
            payload TEXT NOT NULL, state TEXT NOT NULL, deferred INTEGER NOT NULL,
            created REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            lease_token TEXT, lease_until REAL, next_attempt REAL NOT NULL DEFAULT 0,
            last_error TEXT, result TEXT);
        INSERT INTO runtime_inputs VALUES
            ('old-a','scope-a','fp-a','ORIGINAL-A','pending',0,10,3,'stale-token',9,0,'ProviderError',NULL),
            ('old-b','scope-a','fp-b','ORIGINAL-B','pending',0,11,3,NULL,NULL,0,'ProviderError',NULL),
            ('new-a','scope-a','fp-new','NEW-CONTENT','completed',0,12,1,NULL,NULL,0,NULL,'{}');
    """)
    db(root / "deliveries.sqlite3", """
        CREATE TABLE deliveries(id TEXT PRIMARY KEY,status TEXT,updated REAL);
        INSERT INTO deliveries VALUES('old-a','received',10),('old-b','unknown',11);
        CREATE TABLE chat_batches(key TEXT PRIMARY KEY,state TEXT,updated REAL,last_error TEXT,scope TEXT,batch_index INT);
        INSERT INTO chat_batches VALUES('batch-a','blocked',20,'ProviderError','scope-a',1),
            ('batch-b','blocked',21,'ProviderError','scope-a',2);
        CREATE TABLE delivery_receipts(delivery_id TEXT,part_index INT,message_id TEXT);
        INSERT INTO delivery_receipts VALUES('old-a',0,'receipt-kept');
    """)
    scope = root / "library"
    scope.mkdir()
    db(scope / "runtime.sqlite3", """
        CREATE TABLE protocols(agent TEXT,user TEXT,turn_id TEXT,payload TEXT,PRIMARY KEY(agent,user,turn_id));
        INSERT INTO protocols VALUES('agent','user','old-a','{"status":"active","messages":["KEEP"],"tool_audit":[{"receipt":"KEEP"}]}');
        INSERT INTO protocols VALUES('agent','user','old-b','{"status":"active","messages":["UNKNOWN_KEEP"]}');
        INSERT INTO protocols VALUES('agent','user','batch-a','{"status":"active","messages":["BATCH_KEEP"]}');
    """)


def test_apply_exact_input_key_preserves_payload_and_receipts_and_skips_unknown(tmp_path):
    root = tmp_path / "state"
    setup_cancel_state(root)
    plan = plan_stale_inputs(root)
    assert [x["key"] for x in plan["inputs"]] == ["old-a", "old-b"]
    explicit = {"version": 1, "inputs": plan["inputs"]}
    out = apply_stale_plan(root, explicit, tmp_path / "audit.jsonl")
    assert out["results"][0]["result"] == "applied"
    assert out["results"][1]["result"] == "skipped_unknown_delivery"
    with sqlite3.connect(root / "events.sqlite3") as conn:
        a, b = conn.execute("SELECT state,payload,last_error,result FROM runtime_inputs WHERE key='old-a'").fetchone(), conn.execute(
            "SELECT state,payload,last_error,result FROM runtime_inputs WHERE key='old-b'").fetchone()
    assert a[0] == "ignored" and a[1] == "ORIGINAL-A" and a[2] == "ProviderError"
    assert json.loads(a[3]) == {"status": "ignored", "reason": "operator_cancelled_superseded"}
    assert b[0] == "pending" and b[1] == "ORIGINAL-B"
    with sqlite3.connect(root / "deliveries.sqlite3") as conn:
        assert conn.execute("SELECT status FROM deliveries WHERE id='old-a'").fetchone()[0] == "superseded"
        assert conn.execute("SELECT status FROM deliveries WHERE id='old-b'").fetchone()[0] == "unknown"
        assert conn.execute("SELECT message_id FROM delivery_receipts").fetchone()[0] == "receipt-kept"
    with sqlite3.connect(root / "library" / "runtime.sqlite3") as conn:
        payload = json.loads(conn.execute("SELECT payload FROM protocols WHERE turn_id='old-a'").fetchone()[0])
        untouched = json.loads(conn.execute("SELECT payload FROM protocols WHERE turn_id='old-b'").fetchone()[0])
    assert payload["status"] == "cancelled" and payload["cancellation_reason"] == "operator_cancelled_superseded"
    assert payload["messages"] == ["KEEP"] and payload["tool_audit"] == [{"receipt": "KEEP"}]
    assert untouched["status"] == "active"
    audit_rows = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    before = next(x for x in audit_rows if x["phase"] == "before" and x["kind"] == "input" and x["key"] == "old-a")
    assert json.loads(before["protocols"][0]["original_payload"]) == {
        "status": "active", "messages": ["KEEP"], "tool_audit": [{"receipt": "KEEP"}]}
    assert before["lease_token"] == "stale-token" and before["lease_until"] == 9
    assert "result" in before


def test_apply_cas_changed_skips_and_only_cancels_exact_blocked_batch(tmp_path):
    root = tmp_path / "state"
    setup_cancel_state(root)
    plan = plan_stale_inputs(root)
    with sqlite3.connect(root / "events.sqlite3") as conn:
        conn.execute("UPDATE runtime_inputs SET last_error='changed' WHERE key='old-a'")
    explicit = {"version": 1, "inputs": [plan["inputs"][0]], "chat_batches": [
        {"key": "batch-a", "expected_state": "blocked", "expected_updated": 20,
         "expected_last_error": "ProviderError"}]}
    out = apply_stale_plan(root, explicit, tmp_path / "audit.jsonl")
    assert out["results"][0]["result"] == "skipped_cas_changed"
    assert out["results"][1]["result"] == "applied"
    with sqlite3.connect(root / "deliveries.sqlite3") as conn:
        states = dict(conn.execute("SELECT key,state FROM chat_batches"))
        assert conn.execute("SELECT state FROM chat_batches WHERE key='batch-b'").fetchone()[0] == "blocked"
    assert states == {"batch-a": "cancelled", "batch-b": "blocked"}
    with sqlite3.connect(root / "library" / "runtime.sqlite3") as conn:
        payload = json.loads(conn.execute("SELECT payload FROM protocols WHERE turn_id='batch-a'").fetchone()[0])
    assert payload["status"] == "cancelled" and payload["messages"] == ["BATCH_KEEP"]


def test_unknown_delivery_skips_entire_blocked_batch(tmp_path):
    root = tmp_path / "state"
    setup_cancel_state(root)
    with sqlite3.connect(root / "deliveries.sqlite3") as conn:
        conn.execute("INSERT INTO deliveries VALUES('batch-a','unknown',20)")
    explicit = {"version": 1, "chat_batches": [{"key": "batch-a", "expected_state": "blocked",
        "expected_updated": 20, "expected_last_error": "ProviderError"}]}
    out = apply_stale_plan(root, explicit, tmp_path / "audit.jsonl")
    assert out["results"][0]["result"] == "skipped_unknown_delivery"
    with sqlite3.connect(root / "deliveries.sqlite3") as conn:
        assert conn.execute("SELECT state FROM chat_batches WHERE key='batch-a'").fetchone()[0] == "blocked"
    with sqlite3.connect(root / "library" / "runtime.sqlite3") as conn:
        payload = json.loads(conn.execute("SELECT payload FROM protocols WHERE turn_id='batch-a'").fetchone()[0])
    assert payload["status"] == "active"


def test_inflight_delivery_skips_input_and_protocol(tmp_path):
    root = tmp_path / "state"
    setup_cancel_state(root)
    with sqlite3.connect(root / "deliveries.sqlite3") as conn:
        conn.execute("UPDATE deliveries SET status='prepared' WHERE id='old-a'")
    plan = plan_stale_inputs(root)
    explicit = {"version": 1, "inputs": [plan["inputs"][0]]}
    out = apply_stale_plan(root, explicit, tmp_path / "audit.jsonl")
    assert out["results"][0]["result"] == "skipped_inflight_delivery"
    with sqlite3.connect(root / "events.sqlite3") as conn:
        assert conn.execute("SELECT state FROM runtime_inputs WHERE key='old-a'").fetchone()[0] == "pending"
    with sqlite3.connect(root / "library" / "runtime.sqlite3") as conn:
        payload = json.loads(conn.execute("SELECT payload FROM protocols WHERE turn_id='old-a'").fetchone()[0])
    assert payload["status"] == "active"


def test_batch_protocol_compare_and_set_failure_is_partial(tmp_path):
    root = tmp_path / "state"
    setup_cancel_state(root)
    library = root / "library" / "runtime.sqlite3"
    with sqlite3.connect(library) as conn:
        conn.executescript("""
            CREATE TRIGGER block_protocol_cancel BEFORE UPDATE ON protocols
            WHEN OLD.turn_id='batch-a'
            BEGIN SELECT RAISE(IGNORE); END;
        """)
    explicit = {"version": 1, "chat_batches": [{"key": "batch-a", "expected_state": "blocked",
        "expected_updated": 20, "expected_last_error": "ProviderError"}]}
    out = apply_stale_plan(root, explicit, tmp_path / "audit.jsonl")
    assert out["results"][0]["result"] == "partial"
    with sqlite3.connect(root / "deliveries.sqlite3") as conn:
        assert conn.execute("SELECT state FROM chat_batches WHERE key='batch-a'").fetchone()[0] == "cancelled"
    with sqlite3.connect(library) as conn:
        payload = json.loads(conn.execute("SELECT payload FROM protocols WHERE turn_id='batch-a'").fetchone()[0])
    assert payload["status"] == "active"
