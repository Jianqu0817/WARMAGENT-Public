"""Durable storage for original runtime inputs and their processing leases."""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import secrets
import sqlite3
import time
from typing import Any


class InboxFull(RuntimeError):
    """Raised when accepting a new input would exceed the pending limit."""


class RuntimeInbox:
    URGENT_RESERVE = 8

    def __init__(self, path: str | Path, *, pending_limit: int = 512,
                 max_auto_attempts: int = 3):
        self.path = Path(path)
        self.pending_limit = int(pending_limit)
        self.max_auto_attempts = int(max_auto_attempts)
        if self.pending_limit < 1:
            raise ValueError("pending_limit must be positive")
        if self.max_auto_attempts < 1:
            raise ValueError("max_auto_attempts must be positive")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                db.execute("""CREATE TABLE IF NOT EXISTS runtime_inputs (
                    key TEXT PRIMARY KEY, scope TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN
                        ('pending','processing','completed','ignored')),
                    deferred INTEGER NOT NULL, created REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    lease_token TEXT, lease_until REAL, next_attempt REAL NOT NULL DEFAULT 0,
                    last_error TEXT, result TEXT
                )""")
                db.execute("CREATE INDEX IF NOT EXISTS runtime_inputs_claim ON runtime_inputs(state,next_attempt,created)")
                db.execute("""CREATE TABLE IF NOT EXISTS unread_windows (
                    root_key TEXT PRIMARY KEY, scope TEXT NOT NULL, sender TEXT NOT NULL,
                    first_at REAL NOT NULL, last_at REAL NOT NULL, closed INTEGER NOT NULL DEFAULT 0
                )""")
                db.execute("CREATE TABLE IF NOT EXISTS unread_members (input_key TEXT PRIMARY KEY, root_key TEXT NOT NULL)")
                db.execute("CREATE INDEX IF NOT EXISTS unread_members_root ON unread_members(root_key)")

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return closing(db)

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["deferred"] = bool(result["deferred"])
        result["result"] = json.loads(result["result"]) if result["result"] is not None else None
        return result

    def enqueue(self, key: str, scope: str, fingerprint: str, payload: Any,
                *, deferred: bool = False, urgent: bool = False,
                now: float | None = None, batch: bool = False,
                quiet_seconds: float = 1.5, max_wait_seconds: float = 6) -> dict:
        stamp = time.time() if now is None else float(now)
        encoded = self._json(payload)
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM runtime_inputs WHERE key=?", (key,)).fetchone()
            if previous is not None:
                if previous["fingerprint"] != fingerprint or previous["scope"] != scope:
                    raise ValueError("input key already exists with a different fingerprint or scope")
                return self._row(previous)
            count = db.execute("SELECT COUNT(*) FROM runtime_inputs WHERE state IN ('pending','processing')").fetchone()[0]
            capacity = self.pending_limit + (self.URGENT_RESERVE if urgent else 0)
            if count >= capacity:
                raise InboxFull("runtime inbox pending limit reached")
            db.execute("""INSERT INTO runtime_inputs
                (key,scope,fingerprint,payload,state,deferred,created,next_attempt)
                VALUES(?,?,?,?,'pending',?,?,?)""",
                (key, scope, fingerprint, encoded, int(bool(deferred)), stamp, stamp))
            if batch:
                sender = payload['sender_id']
                window = db.execute("""SELECT w.*,i.state,i.result FROM unread_windows w
                    JOIN runtime_inputs i ON i.key=w.root_key
                    WHERE w.scope=? AND w.sender=? AND w.closed=0 AND i.attempts<?
                    AND (i.state IN ('pending','processing') OR
                         (i.state='completed' AND json_extract(i.result,'$.status')='prepared'))
                    ORDER BY w.first_at DESC LIMIT 1""",
                    (scope, sender, self.max_auto_attempts)).fetchone()
                root = window['root_key'] if window else key
                if window is None:
                    db.execute('INSERT INTO unread_windows VALUES(?,?,?,?,?,0)', (key, scope, sender, stamp, stamp))
                else:
                    db.execute('UPDATE unread_windows SET last_at=? WHERE root_key=?', (stamp, root))
                db.execute('INSERT INTO unread_members VALUES(?,?)', (key, root))
                first = window['first_at'] if window else stamp
                # A completed but unsent draft can still absorb new unread input.
                if window and window['state'] == 'completed':
                    db.execute("UPDATE runtime_inputs SET state='pending',attempts=0,result=NULL WHERE key=?", (root,))
                db.execute("UPDATE runtime_inputs SET next_attempt=? WHERE key=? AND state='pending'",
                    (min(first + max_wait_seconds, stamp + quiet_seconds), root))
            row = db.execute("SELECT * FROM runtime_inputs WHERE key=?", (key,)).fetchone()
        return self._row(row)

    def claim(self, *, key: str | None = None, scope: str | None = None,
              allow_deferred: bool = False, now: float | None = None,
              lease_seconds: float = 90, platforms=None) -> dict | None:
        stamp = time.time() if now is None else float(now)
        token = secrets.token_urlsafe(24)
        enabled = json.dumps(list(platforms)) if platforms is not None else None
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT * FROM runtime_inputs
                WHERE (? IS NULL OR key=?) AND (? IS NULL OR scope=?)
                  AND (? IS NULL OR json_extract(payload,'$.platform_id') IN (SELECT value FROM json_each(?)))
                  AND (deferred=0 OR ?)
                  AND (? IS NOT NULL OR (attempts<? AND next_attempt<=?))
                  AND (state='pending' OR (state='processing' AND lease_until<=?))
                  AND NOT EXISTS (SELECT 1 FROM unread_members m WHERE m.input_key=runtime_inputs.key AND m.root_key!=m.input_key)
                  AND (NOT EXISTS(SELECT 1 FROM unread_windows w WHERE w.root_key=runtime_inputs.key) OR next_attempt<=?)
                ORDER BY created,rowid LIMIT 1""",
                (key, key, scope, scope, enabled, enabled, int(allow_deferred), key,
                 self.max_auto_attempts, stamp, stamp, stamp)).fetchone()
            if row is None:
                return None
            db.execute("""UPDATE runtime_inputs SET state='processing',lease_token=?,lease_until=?,
                attempts=attempts+1 WHERE key=?""",
                (token, stamp + float(lease_seconds), row["key"]))
            row = db.execute("SELECT * FROM runtime_inputs WHERE key=?", (row["key"],)).fetchone()
        return self._row(row)

    def renew(self, key: str, token: str, *, now: float | None = None,
              lease_seconds: float = 90) -> bool:
        stamp = time.time() if now is None else float(now)
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("""UPDATE runtime_inputs SET lease_until=? WHERE key=?
                AND state='processing' AND lease_token=? AND lease_until>?""",
                (stamp + float(lease_seconds), key, token, stamp)).rowcount
        return bool(changed)

    def complete(self, key: str, token: str, result: dict, *, now: float | None = None,
                 consumed_keys=None) -> bool:
        stamp = time.time() if now is None else float(now)
        if not isinstance(result, dict) or set(result) - {"id", "status", "reason"}:
            raise ValueError("result may contain only id, status, and reason")
        encoded = self._json(result)
        if len(encoded.encode("utf-8")) > 4096:
            raise ValueError("result exceeds 4096 bytes")
        state = "ignored" if result.get("status") == "ignored" else "completed"
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("""UPDATE runtime_inputs SET state=?,result=?,lease_token=NULL,
                lease_until=NULL,last_error=NULL WHERE key=? AND state='processing'
                AND lease_token=? AND lease_until>?""",
                (state, encoded, key, token, stamp)).rowcount
            if changed:
                if consumed_keys is not None:
                    leftovers = db.execute('SELECT i.* FROM runtime_inputs i JOIN unread_members m ON m.input_key=i.key '
                        'WHERE m.root_key=? AND i.key NOT IN (SELECT value FROM json_each(?)) ORDER BY i.rowid',
                        (key, json.dumps(list(consumed_keys)))).fetchall()
                    if leftovers:
                        new_root = leftovers[0]['key']
                        db.execute('UPDATE unread_windows SET closed=1 WHERE root_key=?', (key,))
                        db.executemany('UPDATE unread_members SET root_key=? WHERE input_key=?',
                            [(new_root, row['key']) for row in leftovers])
                        sender = json.loads(leftovers[0]['payload'])['sender_id']
                        db.execute('INSERT INTO unread_windows VALUES(?,?,?,?,?,0)',
                            (new_root, leftovers[0]['scope'], sender, leftovers[0]['created'], leftovers[-1]['created']))
                        db.execute('UPDATE runtime_inputs SET next_attempt=? WHERE key=?', (stamp, new_root))
                db.execute("""UPDATE runtime_inputs SET state=?,result=?,last_error=NULL
                    WHERE key IN(SELECT input_key FROM unread_members WHERE root_key=? AND input_key!=?)""",
                    (state, encoded, key, key))
        return bool(changed)

    def retry(self, key: str, token: str, error: str, *, now: float | None = None,
              delay: float = 30) -> bool:
        stamp = time.time() if now is None else float(now)
        category = (type(error).__name__ if isinstance(error, BaseException) else str(error).split(":", 1)[0])[:80] or "error"
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("""UPDATE runtime_inputs SET state='pending',lease_token=NULL,
                lease_until=NULL,next_attempt=?,last_error=? WHERE key=? AND state='processing'
                AND lease_token=? AND lease_until>?""",
                (stamp + max(0.0, float(delay)), category, key, token, stamp)).rowcount
        return bool(changed)

    def get(self, key: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM runtime_inputs WHERE key=?", (key,)).fetchone()
        return self._row(row)

    def batch_root(self, key):
        with self._db() as db:
            row = db.execute('SELECT root_key FROM unread_members WHERE input_key=?', (key,)).fetchone()
        return row[0] if row else None

    def batch_members(self, key):
        with self._db() as db:
            rows = db.execute('SELECT i.* FROM runtime_inputs i JOIN unread_members m ON m.input_key=i.key '
                'WHERE m.root_key=? ORDER BY i.rowid', (key,)).fetchall()
        return [self._row(row) for row in rows]

    def seal_batch(self, key):
        """Stop extending a reply only when its transport claims it for sending."""
        with self._db() as db, db:
            row = db.execute('SELECT i.state FROM unread_windows w JOIN runtime_inputs i ON i.key=w.root_key '
                'WHERE w.root_key=?', (key,)).fetchone()
            if row and row[0] != 'completed':
                return False
            db.execute('UPDATE unread_windows SET closed=1 WHERE root_key=?', (key,))
        return True

    def close_batch(self, key):
        with self._db() as db, db:
            db.execute('UPDATE unread_windows SET closed=1 WHERE root_key=?', (key,))

    def foreground_waiting(self, platforms=None):
        enabled = json.dumps(list(platforms)) if platforms is not None else None
        with self._db() as db:
            return db.execute("SELECT 1 FROM runtime_inputs i WHERE state IN ('pending','processing') AND deferred=0 AND attempts<? "
                "AND (? IS NULL OR json_extract(i.payload,'$.platform_id') IN (SELECT value FROM json_each(?))) "
                "AND NOT EXISTS(SELECT 1 FROM unread_members m JOIN runtime_inputs r ON r.key=m.root_key "
                "WHERE m.input_key=i.key AND r.attempts>=?) LIMIT 1",
                (self.max_auto_attempts, enabled, enabled, self.max_auto_attempts)).fetchone() is not None

    def owner_waiting(self, owner_id, scope, *, exclude):
        with self._db() as db:
            # Arrival order, not wall-clock time: an older in-flight request must
            # never interrupt a newer one. Missing legacy anchors see all inputs.
            return db.execute("SELECT 1 FROM runtime_inputs WHERE scope=? AND key!=? "
                "AND rowid>COALESCE((SELECT rowid FROM runtime_inputs WHERE key=?),0) "
                "AND json_extract(payload,'$.sender_id')=? AND (state='processing' OR "
                "(state='pending' AND attempts<?)) AND NOT EXISTS(SELECT 1 FROM unread_members m "
                "WHERE m.input_key=runtime_inputs.key AND m.root_key=?) LIMIT 1",
                (scope, exclude, exclude, owner_id, self.max_auto_attempts, exclude)).fetchone() is not None

    def summary(self, scope: str | None = None, *, now: float | None = None) -> dict[str, int]:
        stamp = time.time() if now is None else float(now)
        with self._db() as db:
            row = db.execute("""SELECT
                SUM(state='pending') AS pending, SUM(state='processing') AS processing,
                SUM(state='completed') AS completed, SUM(state='ignored') AS ignored,
                SUM(deferred=1 AND state IN ('pending','processing')) AS deferred,
                SUM(attempts>=? AND (state='pending' OR
                    (state='processing' AND lease_until<=?))) AS needs_attention
                FROM runtime_inputs WHERE (? IS NULL OR scope=?)""",
                (self.max_auto_attempts, stamp, scope, scope)).fetchone()
        return {name: int(row[name] or 0) for name in
                ("pending", "processing", "completed", "ignored", "deferred", "needs_attention")}
