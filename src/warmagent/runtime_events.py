"""Durable runtime events with per-consumer leased inboxes."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import secrets
import sqlite3
import time
import uuid


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    event_id: str
    kind: str
    source: str
    principal_id: str
    scope: str
    task_id: str
    cause_id: str
    occurred_at: float
    revision: int
    dedup_key: str
    payload_ref: str


class EventJournal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise sqlite3.DatabaseError(f"unsupported runtime event schema version: {version}")
            with db:
                db.execute("""CREATE TABLE IF NOT EXISTS runtime_events (
                    revision INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL, source TEXT NOT NULL, principal_id TEXT NOT NULL,
                    scope TEXT NOT NULL, task_id TEXT NOT NULL, cause_id TEXT NOT NULL,
                    occurred_at REAL NOT NULL, dedup_key TEXT NOT NULL, payload_ref TEXT NOT NULL,
                    UNIQUE(scope, source, dedup_key)
                )""")
                db.execute("""CREATE TABLE IF NOT EXISTS runtime_event_inbox (
                    event_id TEXT NOT NULL REFERENCES runtime_events(event_id),
                    consumer TEXT NOT NULL, claim_token TEXT, lease_until REAL,
                    acknowledged_at REAL,
                    PRIMARY KEY(event_id, consumer)
                )""")
                db.execute("CREATE INDEX IF NOT EXISTS runtime_events_scope_time ON runtime_events(scope, occurred_at, revision)")
                db.execute("CREATE INDEX IF NOT EXISTS runtime_inbox_consumer ON runtime_event_inbox(consumer, acknowledged_at, lease_until)")
                db.execute("PRAGMA user_version=1")

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return closing(db)

    @staticmethod
    def _event(row: sqlite3.Row) -> RuntimeEvent:
        return RuntimeEvent(**{name: row[name] for name in RuntimeEvent.__dataclass_fields__})

    def publish(
        self, *, kind: str, scope: str, source: str, cause_id: str = "",
        task_id: str = "", principal_id: str = "", dedup_key: str,
        payload_ref: str = "", occurred_at: float | None = None,
    ) -> str:
        stamp = time.time() if occurred_at is None else float(occurred_at)
        event_id = uuid.uuid4().hex
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT OR IGNORE INTO runtime_events
                (event_id,kind,source,principal_id,scope,task_id,cause_id,occurred_at,dedup_key,payload_ref)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (event_id, kind, source, principal_id, scope, task_id, cause_id, stamp, dedup_key, payload_ref))
            row = db.execute("SELECT event_id FROM runtime_events WHERE scope=? AND source=? AND dedup_key=?",
                             (scope, source, dedup_key)).fetchone()
        return row[0]

    def claim(self, consumer: str, scope: str, *, now: float | None = None,
              lease_seconds: float = 30, event_id: str | None = None) -> dict | None:
        stamp = time.time() if now is None else float(now)
        token = secrets.token_urlsafe(24)
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT e.* FROM runtime_events e
                LEFT JOIN runtime_event_inbox i ON i.event_id=e.event_id AND i.consumer=?
                WHERE e.scope=? AND (? IS NULL OR e.event_id=?) AND (i.event_id IS NULL OR
                    (i.acknowledged_at IS NULL AND (i.lease_until IS NULL OR i.lease_until<=?)))
                ORDER BY e.occurred_at,e.revision LIMIT 1""",
                (consumer, scope, event_id, event_id, stamp)).fetchone()
            if row is None:
                return None
            db.execute("""INSERT INTO runtime_event_inbox(event_id,consumer,claim_token,lease_until)
                VALUES(?,?,?,?) ON CONFLICT(event_id,consumer) DO UPDATE SET
                claim_token=excluded.claim_token, lease_until=excluded.lease_until""",
                (row["event_id"], consumer, token, stamp + float(lease_seconds)))
            result = dict(row)
            result["claim_token"] = token
        return result

    def ack(self, event_id: str, consumer: str, claim_token: str, *, now: float | None = None) -> bool:
        stamp = time.time() if now is None else float(now)
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("""UPDATE runtime_event_inbox SET acknowledged_at=?
                WHERE event_id=? AND consumer=? AND claim_token=? AND acknowledged_at IS NULL
                AND lease_until>?""", (stamp, event_id, consumer, claim_token, stamp)).rowcount
        return bool(changed)

    def summary(self, scope: str | None = None, *, consumer: str | None = None) -> dict[str, dict[str, int]]:
        with self._db() as db:
            if consumer is not None:
                join = "LEFT JOIN runtime_event_inbox i ON i.event_id=e.event_id AND i.consumer=?"
                params = [consumer]
                condition = " WHERE e.scope=?" if scope is not None else ""
                if scope is not None:
                    params.append(scope)
                sql = """SELECT e.kind,
                    SUM(CASE WHEN i.acknowledged_at IS NULL THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN i.acknowledged_at IS NOT NULL THEN 1 ELSE 0 END) AS acknowledged
                    FROM runtime_events e """ + join + condition + " GROUP BY e.kind"
            else:
                condition = " WHERE e.scope=?" if scope is not None else ""
                params = (scope,) if scope is not None else ()
                sql = """SELECT e.kind, COUNT(*) AS total,
                    SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM runtime_event_inbox i
                        WHERE i.event_id=e.event_id AND i.acknowledged_at IS NOT NULL) THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN EXISTS (SELECT 1 FROM runtime_event_inbox i
                        WHERE i.event_id=e.event_id AND i.acknowledged_at IS NOT NULL) THEN 1 ELSE 0 END) AS acknowledged
                    FROM runtime_events e""" + condition + " GROUP BY e.kind"
            rows = db.execute(sql, params).fetchall()
        return {row["kind"]: {"pending": row["pending"], "acknowledged": row["acknowledged"]} for row in rows}
