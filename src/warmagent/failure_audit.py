"""Read-only snapshot audit and explicit operator-reviewed cancellation helpers.

The private output intentionally contains local task keys but never message text,
envelopes, arguments, outputs, or account identifiers. The separate apply audit
contains sensitive before-images for recovery and must remain private.
"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time


def _db(path):
    return closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True))


def _count(db, sql, args=()):
    return db.execute(sql, args).fetchone()[0]


def _protocol_rows(root: Path, turn_id: str):
    found = []
    for path in sorted(root.glob("*/runtime.sqlite3")):
        try:
            with _db(path) as db:
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='protocols'").fetchone()
                if not exists:
                    continue
                row = db.execute("SELECT agent,user,payload FROM protocols WHERE turn_id=?", (turn_id,)).fetchone()
                if row:
                    found.append((path, row[0], row[1], row[2]))
        except sqlite3.Error:
            continue
    return found


def audit_snapshot(snapshot: str | Path) -> dict:
    root = Path(snapshot)
    out = {"snapshot": root.name, "categories": [], "inventory": {}}
    add = out["categories"].append

    runtimes = sorted(root.glob("*/runtime.sqlite3"))
    if runtimes:
        statuses, failed, recovered = {}, [], []
        for runtime in runtimes:
            with _db(runtime) as db:
                for role, state, count in db.execute(
                        "SELECT role,status,count(*) FROM jobs GROUP BY role,status"):
                    label = f"{role}:{state}"
                    statuses[label] = statuses.get(label, 0) + count
                failed.extend(r[0] for r in db.execute(
                    "SELECT id FROM jobs WHERE role='maintenance' AND status='failed'"))
                recovered.extend(r[0] for r in db.execute(
                    "SELECT j.id FROM jobs j WHERE j.role='maintenance' AND j.status='succeeded' "
                    "AND EXISTS (SELECT 1 FROM schedule s WHERE s.name LIKE 'job_recovery:'||j.id||':%')"))
        out["inventory"]["maintenance_jobs"] = statuses
        add({"class": "historical_failure_superseded" if recovered else "maintenance_status",
             "private_ids": recovered, "failed_ids": failed,
             "evidence": "遍历所有 runtime.sqlite3，维护任务状态与 job_recovery 标记交叉查询；仅有成功状态可证明恢复后续作业成功。",
             "action": "保留已成功结果；若发现其他 failed job，先核对结果与恢复版本标记，再调用 AutoRecovery.tick 的受限规则，不手工改状态。"})

    events = root / "events.sqlite3"
    if events.exists():
        with _db(events) as db:
            rows = db.execute("SELECT key,state,attempts,last_error,deferred FROM runtime_inputs "
                              "WHERE state IN ('pending','needs_attention','processing')").fetchall()
            counts = {f"{state}:deferred={deferred}": n for state, deferred, n in db.execute(
                "SELECT state,deferred,count(*) FROM runtime_inputs GROUP BY state,deferred")}
            out["inventory"]["runtime_inputs"] = counts
            for key, state, attempts, error, deferred in rows:
                protocols = _protocol_rows(root, key)
                active_protocol = False
                if protocols:
                    try:
                        active_protocol = any(json.loads(protocol[3]).get("status") in
                            {"active", "batch_waiting", "waiting_for_computer"} for protocol in protocols)
                    except (ValueError, TypeError):
                        active_protocol = True
                add({"class": "execution_state_unknown_preserve" if active_protocol else "safe_generation_queue_candidate",
                     "private_id": key,
                     "state": state, "attempts": attempts, "error_type": error,
                     "deferred": bool(deferred),
                     "active_protocol": active_protocol,
                     "evidence": "输入仍为 pending 且无结果；与同 turn_id 的 runtime protocol 状态交叉核对。错误字段仅为类型标签。",
                     "action": "有 active protocol 时先恢复/核对原 conversation 与 tool receipts，不得重建 turn；无对应 protocol 时可在 provider/预算恢复后通过原 inbox 到期领取继续，不复制输入。" if active_protocol else "provider/预算恢复后由原 RuntimeInbox 到期领取继续；先确认该键仍无 active protocol/工具回执，不复制输入。"})

    deliveries = root / "deliveries.sqlite3"
    if deliveries.exists():
        with _db(deliveries) as db:
            inv = {s: n for s, n in db.execute("SELECT status,count(*) FROM deliveries GROUP BY status")}
            out["inventory"]["deliveries"] = inv
            for state in ("unknown", "received", "prepared"):
                rows = db.execute("SELECT id,status,trigger_kind,sent_parts,reply_parts FROM deliveries WHERE status=?", (state,)).fetchall()
                for key, status, trigger, sent, parts in rows:
                    cls = "unknown_effect_preserve" if status == "unknown" else "received_or_prepared_reconcile"
                    add({"class": cls, "private_id": key, "state": status,
                         "evidence": f"投递账本状态={status}; sent_parts={sent}; reply_parts={'present' if parts else 'absent'}; trigger={trigger}。",
                         "action": "unknown 先以 delivery_events/receipts/seen_messages 核对外部发送结果，绝不自动重发；received/prepared 先由原 Bridge 及 chat_batches/outbox 对账，再按原幂等键处理。"})
            batch_rows = db.execute("SELECT key,state,batch_index,last_error FROM chat_batches WHERE state IN ('blocked','pending','running')").fetchall()
            out["inventory"]["chat_batches"] = {s: n for s, n in db.execute("SELECT state,count(*) FROM chat_batches GROUP BY state")}
            for key, state, index, error in batch_rows:
                add({"class": "external_budget_or_authorization_block" if state == "blocked" else "safe_queue_candidate",
                     "private_id": key, "state": state, "batch_index": index, "error_type": error,
                     "evidence": "chat_batches 持久续跑状态；blocked 不代表工具操作未发生。",
                     "action": "blocked 先解决 provider/预算/授权条件，再用原 ChatBatches.contol(scope,'resume')入口并按 delivery 状态核对；不得直接新建批次。"})

    activities = root / "activities.sqlite3"
    if activities.exists():
        with _db(activities) as db:
            rows = db.execute("SELECT id,status,device,background,receipts FROM idle_activity WHERE status IN ('blocked','unknown','active','waiting_external')").fetchall()
            out["inventory"]["activity_status"] = {s: n for s, n in db.execute("SELECT status,count(*) FROM idle_activity GROUP BY status")}
            for key, state, device, background, receipts in rows:
                add({"class": "execution_state_unknown_preserve" if state == "unknown" else "external_budget_or_authorization_block",
                     "private_id": f"activity-{key}", "state": state, "device": device,
                     "background": bool(background), "receipt_count": _json_len(receipts),
                     "evidence": "idle_activity 状态、设备类型、背景标记与回执数量；不读取目标/摘要正文。",
                     "action": "blocked 由用户补齐条件后从已保存断点继续；unknown 先通过宿主回执核实，禁止重做可能有副作用的步骤。"})
            slots = db.execute("SELECT task_id,status,filter_job,event FROM task_slots WHERE status IN ('blocked','unknown','active','waiting','ready','paused')").fetchall()
            out["inventory"]["task_slots"] = {s: n for s, n in db.execute("SELECT status,count(*) FROM task_slots GROUP BY status")}
            for key, state, job, event in slots:
                add({"class": "execution_state_unknown_preserve" if state == "unknown" else "external_wait_or_block",
                     "private_id": key, "state": state, "filter_job": bool(job), "has_event": bool(event),
                     "evidence": "task_slots 状态及 event/job 是否已登记。",
                     "action": "复用 TaskSlots 的原状态恢复；unknown 先对账事件/后台job，不重新提交外部动作。"})

    direct = root / "direct-harness.sqlite3"
    if direct.exists():
        with _db(direct) as db:
            rows = db.execute("SELECT call_id,state FROM calls WHERE state IN ('unknown','running','pending')").fetchall()
            out["inventory"]["direct_harness"] = {s: n for s, n in db.execute("SELECT state,count(*) FROM calls GROUP BY state")}
            for call, state in rows:
                add({"class": "execution_state_unknown_preserve", "private_id": call, "state": state,
                     "evidence": "direct_harness.calls 状态；调用参数与结果内容不输出。",
                     "action": "按原 call_id/receipt 核验宿主结果；unknown 不更换ID重试。"})

    bg = root / "background-jobs.sqlite3"
    if bg.exists():
        with _db(bg) as db:
            rows = db.execute("SELECT job_id,status FROM background_jobs WHERE status IN ('unknown','running','failed')").fetchall()
            out["inventory"]["background_jobs"] = {s: n for s, n in db.execute("SELECT status,count(*) FROM background_jobs GROUP BY status")}
            for job, state in rows:
                add({"class": "execution_state_unknown_preserve" if state in {"unknown", "running"} else "external_block",
                     "private_id": job, "state": state,
                     "evidence": "background_jobs 状态与 background_events 是否有回执关联。",
                     "action": "running/unknown 查询原宿主 event/output_ref 或 host_lost receipt 后再处置；不重复提交原命令。"})
    return out


def plan_stale_inputs(state_root: str | Path) -> dict:
    """Return a read-only, exact-key plan for exhausted inputs superseded by newer work.

    A row qualifies only when it is pending, exhausted (attempts >= 3), has a
    non-empty last_error, and a later rowid in the same scope is completed.
    Payloads are never copied into the plan.
    """
    path = Path(state_root) / "events.sqlite3"
    candidates = []
    if not path.exists():
        return {"version": 1, "inputs": [], "reason": "events database absent"}
    with _db(path) as db:
        rows = db.execute("""SELECT i.rowid,i.key,i.scope,i.fingerprint,i.attempts,i.created,i.last_error
            FROM runtime_inputs i
            WHERE i.state='pending' AND i.attempts>=3 AND COALESCE(i.last_error,'')!=''
              AND EXISTS (SELECT 1 FROM runtime_inputs n WHERE n.scope=i.scope
                    AND n.rowid>i.rowid AND n.state='completed')
            ORDER BY i.rowid""").fetchall()
        for rowid, key, scope, fingerprint, attempts, created, last_error in rows:
            newer = db.execute("""SELECT rowid,key,created FROM runtime_inputs
                WHERE scope=? AND rowid>? AND state='completed' ORDER BY rowid DESC LIMIT 1""",
                (scope, rowid)).fetchone()
            candidates.append({"key": key, "scope": scope, "fingerprint": fingerprint,
                "attempts": attempts, "created": created, "last_error": last_error,
                "rowid": rowid,
                "newer_completed": {"key": newer[1], "rowid": newer[0], "created": newer[2]},
                "expected_state": "pending"})
    return {"version": 1, "inputs": candidates}


def apply_stale_plan(state_root: str | Path, explicit_plan: dict,
                     audit_file: str | Path) -> dict:
    """Apply only explicitly listed stale-input and blocked-batch cancellations.

    `explicit_plan` has `inputs` entries copied from :func:`plan_stale_inputs`
    and optional `chat_batches` entries with `key`, `expected_state` (must be
    blocked), `expected_updated`, and `expected_last_error`. This function is
    intentionally not exposed as a CLI. It writes a flushed before-image audit
    record before each item's changes and checks each exact item with CAS.

    SQLite payloads, envelopes, protocol messages, call results and receipts
    are preserved. A target delivery in `unknown` skips the entire item.
    Cross-database changes are not globally atomic; `partial` results identify
    any item where one database committed before a later CAS failed.
    """
    if not isinstance(explicit_plan, dict) or explicit_plan.get("version") != 1:
        raise ValueError("explicit_plan_version_required")
    if not isinstance(explicit_plan.get("inputs", []), list) or not isinstance(explicit_plan.get("chat_batches", []), list):
        raise ValueError("explicit_plan_lists_required")
    root = Path(state_root)
    audit_path = Path(audit_file)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    results = []
    allowed = set()

    def record(entry):
        with audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            import os
            os.fsync(stream.fileno())

    def get_delivery(key):
        path = root / "deliveries.sqlite3"
        if not path.exists():
            return None
        with _db(path) as db:
            return db.execute("SELECT status,updated FROM deliveries WHERE id=?", (key,)).fetchone()

    def protocol_locations(turn_id):
        return _protocol_rows(root, turn_id)

    def cancel_protocol(turn_id, before):
        changed = []
        for path, agent, user, encoded in before:
            try:
                payload = json.loads(encoded)
            except (ValueError, TypeError):
                continue
            if payload.get("status") != "active":
                continue
            payload["status"] = "cancelled"
            payload["cancellation_reason"] = "operator_cancelled_superseded"
            with closing(sqlite3.connect(path, timeout=10)) as db:
                db.execute("BEGIN IMMEDIATE")
                cur = db.execute("UPDATE protocols SET payload=? WHERE agent=? AND user=? AND turn_id=? AND payload=?",
                    (json.dumps(payload, ensure_ascii=False), agent, user, turn_id, encoded))
                db.commit()
            changed.append(bool(cur.rowcount))
        return changed

    def supersede_received(db, key):
        row = db.execute("SELECT status FROM deliveries WHERE id=?", (key,)).fetchone()
        if row and row[0] == "received":
            cur = db.execute("UPDATE deliveries SET status='superseded' WHERE id=? AND status='received'", (key,))
            return {"before": "received", "changed": bool(cur.rowcount)}
        return {"before": row[0] if row else None, "changed": False}

    # Exact pending runtime input plans.
    events_path = root / "events.sqlite3"
    for item in explicit_plan.get("inputs", []):
        if not isinstance(item, dict) or not all(k in item for k in (
                "key", "scope", "fingerprint", "attempts", "created", "last_error", "rowid", "newer_completed")):
            raise ValueError("input_plan_missing_cas_fields")
        if (isinstance(item["attempts"], bool) or not isinstance(item["attempts"], int)
                or item["attempts"] < 3 or not isinstance(item["last_error"], str)
                or not item["last_error"].strip() or not isinstance(item["newer_completed"], dict)):
            raise ValueError("input_plan_not_stale_candidate")
        key = item["key"]
        if ("input", key) in allowed:
            raise ValueError("duplicate_plan_key")
        allowed.add(("input", key))
        with closing(sqlite3.connect(events_path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT rowid,* FROM runtime_inputs WHERE key=?", (key,)).fetchone()
            if row is None or row["state"] != "pending" or row["scope"] != item["scope"] or row["fingerprint"] != item["fingerprint"] or row["attempts"] != item["attempts"] or row["created"] != item["created"] or row["last_error"] != item["last_error"] or row["rowid"] != item["rowid"]:
                db.rollback()
                results.append({"kind": "input", "key": key, "result": "skipped_cas_changed"})
                continue
            newer = db.execute("SELECT rowid,key FROM runtime_inputs WHERE scope=? AND rowid>? AND state='completed' ORDER BY rowid DESC LIMIT 1",
                               (item["scope"], item["rowid"])).fetchone()
            if not newer or newer[0] != item["newer_completed"].get("rowid") or newer[1] != item["newer_completed"].get("key"):
                db.rollback()
                results.append({"kind": "input", "key": key, "result": "skipped_newer_evidence_changed"})
                continue
            delivery = get_delivery(key)
            if delivery and delivery[0] in {"unknown", "prepared", "sending"}:
                db.rollback()
                reason = "unknown" if delivery[0] == "unknown" else "inflight"
                results.append({"kind": "input", "key": key, "result": f"skipped_{reason}_delivery"})
                continue
            protocols = protocol_locations(key)
            record({"phase": "before", "kind": "input", "key": key, "state": row["state"],
                "scope": row["scope"], "fingerprint": row["fingerprint"], "attempts": row["attempts"],
                "created": row["created"], "last_error": row["last_error"], "rowid": row["rowid"],
                "result": row["result"], "lease_token": row["lease_token"], "lease_until": row["lease_until"],
                "delivery_status": delivery[0] if delivery else None,
                "protocols": [{"path": str(p), "agent": a, "user": u,
                    "status": _protocol_status(v), "original_payload": v} for p, a, u, v in protocols]})
            changed = db.execute("""UPDATE runtime_inputs SET state='ignored',result=?,
                lease_token=NULL,lease_until=NULL WHERE rowid=? AND key=? AND scope=? AND fingerprint=?
                AND attempts=? AND created=? AND last_error=? AND state='pending'""",
                (json.dumps({"status": "ignored", "reason": "operator_cancelled_superseded"}, separators=(",", ":")),
                 item["rowid"], key, item["scope"], item["fingerprint"], item["attempts"], item["created"], item["last_error"])).rowcount
            db.commit()
        if not changed:
            results.append({"kind": "input", "key": key, "result": "skipped_cas_changed"})
            continue
        delivery_change = None
        deliveries_path = root / "deliveries.sqlite3"
        if deliveries_path.exists():
            with closing(sqlite3.connect(deliveries_path, timeout=10)) as db:
                db.execute("BEGIN IMMEDIATE")
                delivery_change = supersede_received(db, key)
                db.commit()
        protocol_changes = cancel_protocol(key, protocols)
        results.append({"kind": "input", "key": key,
            "result": "applied" if all(protocol_changes) else "partial" if protocol_changes else "applied",
            "delivery": delivery_change, "protocol_updates": protocol_changes})
        record({"phase": "after", "kind": "input", "key": key,
            "result": results[-1]["result"], "delivery": delivery_change,
            "protocol_updates": protocol_changes})

    # Exact blocked batches; no scope-wide update is used.
    deliveries_path = root / "deliveries.sqlite3"
    for item in explicit_plan.get("chat_batches", []):
        if not isinstance(item, dict) or not all(k in item for k in (
                "key", "expected_state", "expected_updated", "expected_last_error")):
            raise ValueError("batch_plan_missing_cas_fields")
        key = item["key"]
        if ("batch", key) in allowed:
            raise ValueError("duplicate_plan_key")
        allowed.add(("batch", key))
        if item["expected_state"] != "blocked":
            raise ValueError("batch_expected_state_must_be_blocked")
        if not deliveries_path.exists():
            results.append({"kind": "chat_batch", "key": key, "result": "skipped_database_absent"})
            continue
        protocols = protocol_locations(key)
        with closing(sqlite3.connect(deliveries_path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT key,state,updated,last_error FROM chat_batches WHERE key=?", (key,)).fetchone()
            if row is None or row["state"] != "blocked" or row["updated"] != item["expected_updated"] or row["last_error"] != item["expected_last_error"]:
                db.rollback()
                results.append({"kind": "chat_batch", "key": key, "result": "skipped_cas_changed"})
                continue
            delivery = db.execute("SELECT status,updated FROM deliveries WHERE id=?", (key,)).fetchone()
            if delivery and delivery["status"] in {"unknown", "prepared", "sending"}:
                db.rollback()
                reason = "unknown" if delivery["status"] == "unknown" else "inflight"
                results.append({"kind": "chat_batch", "key": key, "result": f"skipped_{reason}_delivery"})
                continue
            record({"phase": "before", "kind": "chat_batch", "key": key,
                "state": row["state"], "updated": row["updated"], "last_error": row["last_error"],
                "delivery_status": delivery["status"] if delivery else None,
                "protocols": [{"path": str(p), "agent": a, "user": u,
                    "status": _protocol_status(v), "original_payload": v} for p, a, u, v in protocols]})
            cur = db.execute("UPDATE chat_batches SET state='cancelled',updated=? WHERE key=? AND state='blocked' AND updated=? AND last_error=?",
                (time.time(), key, item["expected_updated"], item["expected_last_error"]))
            delivery_change = supersede_received(db, key)
            db.commit()
        if not cur.rowcount:
            results.append({"kind": "chat_batch", "key": key, "result": "skipped_cas_changed"})
            continue
        protocol_changes = cancel_protocol(key, protocols)
        protocol_partial = any(not changed for changed in protocol_changes)
        results.append({"kind": "chat_batch", "key": key,
            "result": "partial" if protocol_partial else "applied",
            "delivery": delivery_change, "protocol_updates": protocol_changes})
        record({"phase": "after", "kind": "chat_batch", "key": key,
            "result": results[-1]["result"], "delivery": delivery_change, "protocol_updates": protocol_changes})
    return {"results": results, "applied": sum(x["result"] in {"applied", "partial"} for x in results),
            "skipped": sum(x["result"].startswith("skipped") for x in results)}


def _protocol_status(encoded):
    try:
        return json.loads(encoded).get("status")
    except (ValueError, TypeError):
        return "invalid"


def _json_len(value):
    try:
        obj = json.loads(value or "[]")
        return len(obj) if isinstance(obj, list) else 0
    except (ValueError, TypeError):
        return None
