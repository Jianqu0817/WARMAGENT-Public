"""Durable parking for activities waiting on host-owned external events.

TaskSlots transfers the singleton activity row inside the shared SQLite file.
It does not make a transaction across the activity runner, Harness, or any
external process; callers must persist each boundary through its own contract.
"""
from __future__ import annotations

from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import time


_TERMINAL = {"cancelled", "completed"}
_NONTERMINAL = {"waiting", "ready", "active", "paused", "unknown"}


class TaskSlots:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS task_slots(
                task_id TEXT PRIMARY KEY, scope TEXT NOT NULL, revision INTEGER NOT NULL,
                status TEXT NOT NULL, filter_job TEXT NOT NULL, deadline REAL,
                resume_note TEXT NOT NULL DEFAULT '', event TEXT, payload TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS task_slots_ready ON task_slots(status,created,task_id)")
            db.execute("CREATE INDEX IF NOT EXISTS task_slots_scope ON task_slots(scope,status,created)")
            db.execute("""CREATE TABLE IF NOT EXISTS task_slot_state(
                id INTEGER PRIMARY KEY CHECK(id=1), last_scope TEXT, last_task_id TEXT)""")
            db.execute("INSERT OR IGNORE INTO task_slot_state(id) VALUES(1)")
            if 'dispatch_paused' not in {row[1] for row in db.execute('PRAGMA table_info(task_slot_state)')}:
                db.execute('ALTER TABLE task_slot_state ADD COLUMN dispatch_paused INTEGER NOT NULL DEFAULT 0')

    def set_dispatch_paused(self, paused):
        with self._db() as db, db:
            db.execute('UPDATE task_slot_state SET dispatch_paused=? WHERE id=1', (int(bool(paused)),))

    def dispatch_paused(self):
        with self._db() as db:
            return bool(db.execute('SELECT dispatch_paused FROM task_slot_state WHERE id=1').fetchone()[0])

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return closing(db)

    def park_current(self, scope, now=None):
        stamp = time.time() if now is None else float(now)
        if not isinstance(scope, str) or not scope:
            return False
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM idle_activity WHERE id=1").fetchone()
            if not row:
                return False
            protocol = _json(row["protocol"], {})
            if row["status"] == "empty":
                # A duplicate call after the transfer is an idempotent success.
                marker = db.execute("SELECT last_scope,last_task_id FROM task_slot_state WHERE id=1").fetchone()
                return bool(marker and marker["last_scope"] == scope and marker["last_task_id"]
                    and db.execute("SELECT 1 FROM task_slots WHERE task_id=? AND scope=? "
                        "AND status IN ('waiting','ready','paused')",
                        (marker["last_task_id"], scope)).fetchone())
            if (row["status"] not in {"waiting_external", "active"} or row["pause_requested"]
                    or not isinstance(protocol, dict) or not isinstance(protocol.get("event_wait"), dict)
                    or protocol.get("pending_calls") not in (None, [])):
                return False
            task_id = protocol.get("activity_id")
            wait = protocol["event_wait"]
            job_id = wait.get("job_id", wait.get("filter_job"))
            if not isinstance(task_id, str) or not task_id or not isinstance(job_id, str) or not job_id:
                return False
            deadline = _deadline(wait)
            resume_note = wait.get("resume_note", wait.get("note", ""))
            if not isinstance(resume_note, str):
                resume_note = ""
            payload = {key: row[key] for key in row.keys() if key != "id"}
            payload["protocol"] = protocol
            old = db.execute("SELECT scope,revision,status FROM task_slots WHERE task_id=?",
                             (task_id,)).fetchone()
            if old and old["scope"] != scope:
                return False
            revision = int(old["revision"]) + 1 if old else 1
            if old and old["status"] == "cancelled":
                return False
            db.execute("""INSERT INTO task_slots(task_id,scope,revision,status,filter_job,deadline,
                resume_note,event,payload,created,updated) VALUES(?,?,?,'waiting',?,?,?,NULL,?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET scope=excluded.scope,revision=excluded.revision,
                status='waiting',filter_job=excluded.filter_job,deadline=excluded.deadline,
                resume_note=excluded.resume_note,event=NULL,payload=excluded.payload,updated=excluded.updated""",
                       (task_id, scope, revision, job_id, deadline, resume_note,
                        json.dumps(payload, ensure_ascii=False), stamp, stamp))
            db.execute("""UPDATE idle_activity SET goal='',summary='',next_step='',device='',
                status='empty',protocol='{}',receipts='[]',background=0,last_check=?,
                resume_requested=0,interruption_notified=0,rest_until=NULL,updated=? WHERE id=1""",
                       (stamp, stamp))
            db.execute("UPDATE task_slot_state SET last_scope=?,last_task_id=? WHERE id=1",
                       (scope, task_id))
            return True

    def notify(self, scope, job_id, event):
        if not isinstance(event, dict) or event.get("job_id") != job_id:
            return False
        if event.get("scope", scope) != scope:
            return False
        stamp = time.time()
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM task_slots WHERE scope=? AND filter_job=? "
                             "AND status IN ('waiting','paused') ORDER BY created LIMIT 1",
                             (scope, job_id)).fetchone()
            if not row or row["event"] is not None:
                return False
            if event.get("event_id") not in {job_id, "hostlost:" + job_id}:
                return False
            if event.get("task") is not None and event.get("task") != row["task_id"]:
                return False
            status = "ready" if row["status"] == "waiting" else "paused"
            changed = db.execute("UPDATE task_slots SET event=?,status=?,revision=revision+1,updated=? "
                                 "WHERE task_id=? AND revision=? AND event IS NULL",
                                 (json.dumps(event, ensure_ascii=False), status, stamp,
                                  row["task_id"], row["revision"])).rowcount
            return changed == 1

    def expire(self, now):
        stamp = float(now)
        if not math.isfinite(stamp):
            raise ValueError("invalid_task_slot_time")
        count = 0
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT task_id,revision,filter_job,deadline FROM task_slots "
                              "WHERE status='waiting' AND deadline IS NOT NULL AND deadline<=? "
                              "ORDER BY deadline,created", (stamp,)).fetchall()
            for row in rows:
                event = {"type": "timeout", "job_id": row["filter_job"],
                         "deadline": row["deadline"], "occurred_at": stamp}
                count += db.execute("UPDATE task_slots SET status='ready',event=?,revision=revision+1,updated=? "
                    "WHERE task_id=? AND status='waiting' AND revision=?",
                    (json.dumps(event, ensure_ascii=False), stamp, row["task_id"], row["revision"])).rowcount
        return count

    def activate_next(self, now, quiet_period=180):
        stamp = float(now)
        quiet = float(quiet_period)
        if not math.isfinite(stamp) or not math.isfinite(quiet) or quiet < 0:
            raise ValueError("invalid_task_slot_time")
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM idle_activity WHERE id=1").fetchone()
            if not current or current["status"] not in {"empty", *_TERMINAL} or current["pause_requested"]:
                return False
            if current["last_chat"] is not None and stamp - float(current["last_chat"]) < quiet:
                return False
            if current["status"] in _TERMINAL:
                current_protocol = _json(current["protocol"], {})
                current_task_id = current_protocol.get("activity_id") if isinstance(current_protocol, dict) else None
                if current_task_id:
                    db.execute("UPDATE task_slots SET status=?,revision=revision+1,updated=? "
                               "WHERE task_id=? AND status='active'",
                               (current["status"], stamp, current_task_id))
            row = db.execute("SELECT * FROM task_slots WHERE status='ready' "
                             "ORDER BY created,task_id LIMIT 1").fetchone()
            if not row:
                return False
            payload = _json(row["payload"], {})
            if not isinstance(payload, dict):
                return False
            protocol = _json(payload.get("protocol"), {})
            if not isinstance(protocol, dict):
                protocol = {}
            protocol.pop("event_wait", None)
            event = _json(row["event"], {})
            protocol["external_event"] = event
            protocol.pop('external_event_observed', None)
            uncertain = (isinstance(event, dict) and
                         (event.get("status") == "unknown" or event.get("type") == "host_lost"))
            next_status = "unknown" if uncertain else "active"
            changed = db.execute("UPDATE task_slots SET status=?,revision=revision+1,updated=? "
                                 "WHERE task_id=? AND status='ready' AND revision=?",
                                 (next_status, stamp, row["task_id"], row["revision"])).rowcount
            if changed != 1:
                return False
            idle_status = "unknown" if uncertain else "active"
            db.execute("""UPDATE idle_activity SET goal=?,summary=?,next_step=?,device=?,status=?,
                protocol=?,receipts=?,background=?,resume_requested=0,interruption_notified=0,
                rest_until=NULL,last_check=?,updated=? WHERE id=1""",
                       (payload.get("goal", ""), payload.get("summary", ""),
                        payload.get("next_step", ""), payload.get("device", ""),
                        idle_status,
                        json.dumps(protocol, ensure_ascii=False),
                        json.dumps(_json(payload.get("receipts"), []), ensure_ascii=False),
                        int(bool(payload.get("background", False))), stamp, stamp))
            return True

    def sync_current(self, now=None):
        """Copy a terminal singleton state to its matching active task slot."""
        stamp = time.time() if now is None else float(now)
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT status,protocol FROM idle_activity WHERE id=1").fetchone()
            if not current or current["status"] not in _TERMINAL:
                return False
            protocol = _json(current["protocol"], {})
            task_id = protocol.get("activity_id") if isinstance(protocol, dict) else None
            if not isinstance(task_id, str) or not task_id:
                return False
            return db.execute("UPDATE task_slots SET status=?,revision=revision+1,updated=? "
                "WHERE task_id=? AND status IN ('active','unknown')",
                (current["status"], stamp, task_id)).rowcount == 1

    def list(self, scope=None):
        with self._db() as db:
            if scope is None:
                rows = db.execute("SELECT * FROM task_slots ORDER BY created DESC,task_id LIMIT 100").fetchall()
            else:
                rows = db.execute("SELECT * FROM task_slots WHERE scope=? ORDER BY created DESC,task_id LIMIT 100",
                                  (scope,)).fetchall()
        result = []
        for row in rows:
            event = _json(row["event"], {})
            result.append({"task_id": row["task_id"], "scope": row["scope"],
                "revision": row["revision"], "version": row["revision"], "status": row["status"],
                "filter_job": row["filter_job"], "deadline": row["deadline"],
                "resume_note": row["resume_note"],
                "event_ref": event.get("event_id", event.get("job_id")) if isinstance(event, dict) else None,
                "updated": row["updated"]})
        return result

    def control(self, task_id, action, expected_revision=None, *, version=None, now=None):
        if action not in {"pause", "resume", "cancel"}:
            raise ValueError("invalid_task_slot_action")
        if version is not None:
            if expected_revision is not None and expected_revision != version:
                raise ValueError("conflicting_task_slot_version")
            expected_revision = version
        stamp = time.time() if now is None else float(now)
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM task_slots WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                return {"accepted": False, "reason": "not_found"}
            if expected_revision is not None and int(expected_revision) != row["revision"]:
                return {"accepted": False, "reason": "stale_revision", "revision": row["revision"]}
            if row["status"] in _TERMINAL:
                return {"accepted": False, "reason": row["status"], "revision": row["revision"]}
            if action == "pause":
                status = "paused"
            elif action == "cancel":
                status = "cancelled"
            elif row["status"] == "paused":
                status = "ready" if row["event"] is not None else "waiting"
            else:
                return {"accepted": False, "reason": "not_paused", "revision": row["revision"]}
            db.execute("UPDATE task_slots SET status=?,revision=revision+1,updated=? WHERE task_id=? AND revision=?",
                       (status, stamp, task_id, row["revision"]))
            return {"accepted": True, "task_id": task_id, "status": status,
                    "revision": row["revision"] + 1, "version": row["revision"] + 1}


def _json(value, fallback):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _deadline(event_wait):
    value = next((event_wait[key] for key in ("deadline", "deadline_at", "expires_at")
                  if event_wait.get(key) is not None), None)
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None
