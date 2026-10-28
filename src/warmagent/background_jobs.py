"""Persistent, scoped receipts for native DSH background jobs."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time


class BackgroundJobs:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS background_jobs(
                job_id TEXT PRIMARY KEY, scope TEXT NOT NULL, task TEXT NOT NULL,
                turn TEXT NOT NULL, session_id TEXT NOT NULL, status TEXT NOT NULL,
                receipt_ref TEXT NOT NULL, updated REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS background_events(
                event_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, scope TEXT NOT NULL,
                task TEXT NOT NULL, payload TEXT NOT NULL, acked INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL)""")
            columns = {row[1] for row in db.execute('PRAGMA table_info(background_events)')}
            for name, declaration in (('report_attempts', 'INTEGER NOT NULL DEFAULT 0'),
                                      ('next_attempt', 'REAL NOT NULL DEFAULT 0'), ('report_error', 'TEXT')):
                if name not in columns:
                    db.execute(f'ALTER TABLE background_events ADD COLUMN {name} {declaration}')
            self._mark_running_unknown(db, time.time())

    def record(self, scope, task, job_id, *, turn, session_id, receipt_ref=None):
        if not all(isinstance(value, str) and value for value in
                   (scope, task, job_id, turn, session_id)):
            raise ValueError("background_job_identity_required")
        encoded = json.dumps(receipt_ref or {}, ensure_ascii=False, separators=(",", ":"))
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            row = db.execute("SELECT scope,task,turn,session_id FROM background_jobs WHERE job_id=?",
                             (job_id,)).fetchone()
            if row and row != (scope, task, turn, session_id):
                raise ValueError("background_job_identity_conflict")
            db.execute("""INSERT INTO background_jobs(job_id,scope,task,turn,session_id,status,receipt_ref,updated)
                VALUES(?,?,?,?,?,'running',?,?) ON CONFLICT(job_id) DO NOTHING""",
                       (job_id, scope, task, turn, session_id, encoded, time.time()))
        return {"job_id": job_id, "scope": scope, "task": task, "turn": turn,
                "status": "running", "receipt_ref": json.loads(encoded)}

    def get(self, scope, job_id):
        """Read one host-owned job record, fenced by its registered scope."""
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            row = db.execute("SELECT job_id,scope,task,turn,session_id,status,receipt_ref,updated "
                             "FROM background_jobs WHERE scope=? AND job_id=?",
                             (scope, job_id)).fetchone()
        if not row:
            return None
        return {"job_id": row[0], "scope": row[1], "task": row[2], "turn": row[3],
                "session_id": row[4], "status": row[5], "receipt_ref": json.loads(row[6]),
                "updated": row[7]}

    def list_jobs(self, scope, task):
        """List task-owned job metadata without returning captured output."""
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            rows = db.execute("SELECT job_id,status,turn,receipt_ref,updated FROM background_jobs "
                              "WHERE scope=? AND task=? ORDER BY updated,job_id",
                              (scope, task)).fetchall()
        return [{"job_id": row[0], "status": row[1], "turn": row[2],
                 "receipt_ref": json.loads(row[3]), "updated": row[4]} for row in rows]

    def list_pending(self, limit=50):
        """Return bounded raw host events for delivery orchestration."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("invalid_background_event_limit")
        limit = min(limit, 1000)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            rows = db.execute("SELECT payload FROM background_events WHERE acked=0 "
                              "AND report_attempts<3 AND next_attempt<=? ORDER BY created,event_id LIMIT ?",
                              (time.time(), limit)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def defer_report(self, event_id, error):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute('UPDATE background_events SET report_attempts=report_attempts+1,next_attempt=?,report_error=? '
                'WHERE event_id=? AND acked=0', (time.time() + 60, str(error)[:80], event_id))

    def retry_report(self, scope, job_id):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            count = db.execute('UPDATE background_events SET report_attempts=0,next_attempt=0,report_error=NULL '
                'WHERE scope=? AND job_id=? AND acked=0', (scope, job_id)).rowcount
        return {'accepted': bool(count), 'job_id': job_id}

    def page(self, scope, job_id, offset=0, limit=4000):
        """Read a bounded page from a completed job's validated output reference."""
        if (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or
                isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 4000):
            raise ValueError("invalid_background_output_page")
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            row = db.execute("SELECT e.payload FROM background_events e JOIN background_jobs j "
                             "ON j.job_id=e.job_id WHERE j.scope=? AND j.job_id=? "
                             "AND e.scope=? ORDER BY e.created DESC LIMIT 1",
                             (scope, job_id, scope)).fetchone()
        if not row:
            return None
        event = json.loads(row[0])
        ref = event.get("output_ref") if isinstance(event.get("output_ref"), dict) else {}
        text, complete = self._event_output(event, ref)
        end = min(offset + limit, len(text))
        next_offset = end if end < len(text) else None
        return {"job_id": job_id, "scope": scope, "offset": offset,
                "text": text[offset:end], "next_offset": next_offset,
                "total_chars": len(text) if complete else None,
                "complete": complete, "has_more": next_offset is not None or not complete,
                "receipt_ref": {"event_id": event.get("event_id"), "output_ref": ref}}

    @staticmethod
    def _event_output(event, output_ref):
        paths = output_ref.get("spill_paths", [])
        if isinstance(paths, list) and paths and all(isinstance(path, str) and path for path in paths):
            try:
                streams = [Path(path).read_text(encoding="utf-8", errors="replace") for path in paths]
                if len(streams) > 1:
                    text = streams[0]
                    if streams[1]:
                        text += ("" if not text or text.endswith("\n") else "\n") + "[stderr]\n" + streams[1]
                    text += "".join(streams[2:])
                else:
                    text = streams[0]
                return text, True
            except OSError:
                pass
        preview = event.get("output_preview")
        if not isinstance(preview, str):
            preview = ""
        complete = event.get("output_complete") is True
        return preview, complete

    def observe_completed(self, scope, task, event):
        if not isinstance(event, dict):
            return False
        job_id, event_id = event.get("job_id"), event.get("event_id")
        if (not isinstance(job_id, str) or not job_id or
                event_id not in {job_id, "hostlost:" + job_id}):
            return False
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            row = db.execute("SELECT scope,task,turn,session_id FROM background_jobs WHERE job_id=?",
                             (job_id,)).fetchone()
            if not row or row != (scope, task, event.get("turn"), event.get("session_id")):
                return False
            if event.get("scope") != scope or event.get("task") != task:
                return False
            payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            inserted = db.execute("INSERT OR IGNORE INTO background_events(event_id,job_id,scope,task,payload,created) "
                                  "VALUES(?,?,?,?,?,?)",
                                  (event_id, job_id, scope, task, payload, time.time()))
            reference = json.dumps({"event_id": event_id,
                                    "output_ref": event.get("output_ref", {})},
                                   ensure_ascii=False, separators=(",", ":"))
            db.execute("UPDATE background_jobs SET status=?,receipt_ref=?,updated=? WHERE job_id=?",
                       (str(event.get("status", "unknown")), reference,
                        time.time(), job_id))
            return inserted.rowcount == 1

    def pending_events(self, scope, task):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            rows = db.execute("SELECT payload FROM background_events WHERE scope=? AND task=? "
                              "AND acked=0 ORDER BY created,event_id", (scope, task)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def ack(self, event_id):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            cursor = db.execute("UPDATE background_events SET acked=1 WHERE event_id=? AND acked=0",
                                (event_id,))
            return cursor.rowcount == 1

    def mark_running_unknown(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            self._mark_running_unknown(db, time.time())

    @staticmethod
    def _mark_running_unknown(db, stamp):
        rows = db.execute("SELECT job_id,scope,task,turn,session_id FROM background_jobs "
                          "WHERE status='running'").fetchall()
        for job_id, scope, task, turn, session_id in rows:
            event_id = "hostlost:" + job_id
            event = {"event_id": event_id, "job_id": job_id, "scope": scope,
                     "task": task, "turn": turn, "session_id": session_id,
                     "status": "unknown", "type": "host_lost",
                     "note": "宿主退出后结果未知；不要重复执行该动作。"}
            db.execute("INSERT OR IGNORE INTO background_events "
                "(event_id,job_id,scope,task,payload,created) VALUES(?,?,?,?,?,?)",
                (event_id, job_id, scope, task, json.dumps(event, ensure_ascii=False), stamp))
            db.execute("UPDATE background_jobs SET status='unknown',receipt_ref=?,updated=? "
                       "WHERE job_id=? AND status='running'",
                       (json.dumps({"event_id": event_id}), stamp, job_id))
