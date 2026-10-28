"""Small persistent host-side activity mode and procedure-interest store.

This is not persona or fact memory. Interest notes are self-reported candidates
with source pointers; tool receipts do not certify user-goal success.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
import sqlite3
import time
import uuid
import math
import hashlib


_MODES = {"working", "resting", "sleeping"}
_INTEREST_STATUSES = {"active", "paused", "completed", "retired"}
_OUTCOMES = {"tool_completed", "tool_failed", "unknown"}


def _epoch(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("timestamp must be finite")
        return parsed
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    result = (parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
              if isinstance(parsed, datetime) else float(parsed))
    if not math.isfinite(result):
        raise ValueError("timestamp must be finite")
    return result


class SelfDirection:
    def __init__(self, path, allowed_source=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.allowed_source = allowed_source
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self):
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS direction_scope (
                    scope TEXT PRIMARY KEY, mode TEXT NOT NULL, until_epoch REAL,
                    min_share_seconds REAL NOT NULL DEFAULT 3600,
                    last_shared_at REAL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS direction_interest (
                    scope TEXT NOT NULL, interest_id TEXT NOT NULL, description TEXT NOT NULL,
                    source_ref TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    motivation TEXT NOT NULL DEFAULT '', progress TEXT NOT NULL DEFAULT '',
                    next_step TEXT NOT NULL DEFAULT '', PRIMARY KEY(scope, interest_id)
                );
                CREATE TABLE IF NOT EXISTS direction_outcome (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, source_ref TEXT NOT NULL,
                    tool TEXT NOT NULL, status TEXT NOT NULL, error_kind TEXT,
                    observed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS direction_feedback (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, source_ref TEXT NOT NULL,
                    accepted INTEGER NOT NULL, observed_at REAL NOT NULL
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(direction_interest)")}
            for column in ("motivation", "progress", "next_step"):
                if column not in columns:
                    db.execute(f"ALTER TABLE direction_interest ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _scope(scope):
        if not isinstance(scope, str) or not scope.strip() or len(scope) > 256:
            raise ValueError("scope must be a nonempty string")
        return scope

    def _ensure_scope(self, db, scope, now=None):
        now = time.time() if now is None else now
        db.execute("INSERT OR IGNORE INTO direction_scope(scope,mode,until_epoch,updated_at) VALUES(?,?,NULL,?)",
                   (scope, "working", now))

    def overview(self, scope):
        scope = self._scope(scope)
        with closing(self._connect()) as db:
            self._ensure_scope(db, scope)
            state = dict(db.execute("SELECT * FROM direction_scope WHERE scope=?", (scope,)).fetchone())
            interests = [dict(row) for row in db.execute(
                "SELECT interest_id,description,motivation,progress,next_step,source_ref,status,created_at,updated_at "
                "FROM direction_interest "
                "WHERE scope=? ORDER BY updated_at DESC,interest_id LIMIT 20", (scope,))]
            outcomes = [dict(row) for row in db.execute(
                "SELECT source_ref,tool,status,error_kind,observed_at FROM direction_outcome "
                "WHERE scope=? ORDER BY observed_at DESC LIMIT 10", (scope,))]
        return {"scope": scope, "mode": state["mode"], "until_epoch": state["until_epoch"],
            "min_share_seconds": state["min_share_seconds"], "last_shared_at": state["last_shared_at"],
            "interests": [{**row, "kind": "self_reported_interest_candidate",
                           "is_fact": False, "is_persona": False} for row in interests],
            "recent_tool_outcomes": [{**row, "goal_success": False,
                "meaning": "tool outcome only; does not establish user-goal completion"} for row in outcomes]}

    def set_mode(self, scope, mode, until=None):
        scope = self._scope(scope)
        if mode not in _MODES:
            raise ValueError("mode must be working, resting, or sleeping")
        now = time.time()
        deadline = None if mode == "working" else _epoch(until)
        if mode != "working" and (deadline is None or not math.isfinite(deadline) or deadline <= now):
            raise ValueError("resting/sleeping require a finite future deadline")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            self._ensure_scope(db, scope, now)
            db.execute("UPDATE direction_scope SET mode=?,until_epoch=?,updated_at=? WHERE scope=?",
                       (mode, deadline, now, scope))
            db.commit()
        return self.overview(scope)

    def ready(self, scope, now):
        scope = self._scope(scope)
        current = _epoch(now)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            self._ensure_scope(db, scope, current)
            row = db.execute("SELECT mode,until_epoch FROM direction_scope WHERE scope=?", (scope,)).fetchone()
            if row["mode"] == "working":
                db.commit()
                return True
            if row["until_epoch"] is not None and current >= row["until_epoch"]:
                db.execute("UPDATE direction_scope SET mode='working',until_epoch=NULL,updated_at=? WHERE scope=?",
                           (current, scope))
                db.commit()
                return True
            db.commit()
            return False

    def _interest_card(self, scope, interest_id):
        scope = self._scope(scope)
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT interest_id,description,motivation,progress,next_step,source_ref,status,created_at,updated_at "
                "FROM direction_interest WHERE scope=? AND interest_id=?", (scope, interest_id)).fetchone()
        if row is None:
            raise KeyError("interest not found in scope")
        return {**dict(row), "scope": scope, "kind": "self_reported_interest_candidate",
                "is_fact": False, "is_persona": False}

    def add_interest(self, scope, interest_id, description, source_ref, motivation="", next_step=""):
        scope = self._scope(scope)
        if not isinstance(interest_id, str) or not interest_id.strip() or len(interest_id) > 128:
            raise ValueError("interest_id must be a nonempty string")
        if not isinstance(description, str) or not description.strip() or len(description) > 2000:
            raise ValueError("description must be 1-2000 characters")
        motivation = self._interest_text(motivation, "motivation")
        next_step = self._interest_text(next_step, "next_step")
        self._source(scope, source_ref)
        now = time.time()
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("INSERT INTO direction_interest "
                    "(scope,interest_id,description,source_ref,status,created_at,updated_at,motivation,progress,next_step) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (scope, interest_id, description.strip(), source_ref, "active", now, now,
                     motivation, "", next_step))
            except sqlite3.IntegrityError as error:
                db.rollback()
                raise ValueError("interest_id already exists in this scope") from error
            db.commit()
        return self._interest_card(scope, interest_id)

    def set_interest_status(self, scope, interest_id, status):
        scope = self._scope(scope)
        if status not in _INTEREST_STATUSES:
            raise ValueError("invalid interest status")
        with closing(self._connect()) as db:
            cursor = db.execute("UPDATE direction_interest SET status=?,updated_at=? WHERE scope=? AND interest_id=?",
                                (status, time.time(), scope, interest_id))
            if cursor.rowcount != 1:
                raise KeyError("interest not found in scope")
        return self._interest_card(scope, interest_id)

    def update_interest(self, scope, interest_id, progress=None, next_step=None, motivation=None, status=None):
        scope = self._scope(scope)
        if status is not None and status not in _INTEREST_STATUSES:
            raise ValueError("invalid interest status")
        updates = {}
        for field, value in (("progress", progress), ("next_step", next_step), ("motivation", motivation)):
            if value is not None:
                updates[field] = self._interest_text(value, field)
        if status is not None:
            updates["status"] = status
        with closing(self._connect()) as db:
            if updates:
                assignments = ",".join(f"{field}=?" for field in updates)
                cursor = db.execute(
                    f"UPDATE direction_interest SET {assignments},updated_at=? WHERE scope=? AND interest_id=?",
                    (*updates.values(), time.time(), scope, interest_id))
                if cursor.rowcount != 1:
                    raise KeyError("interest not found in scope")
            else:
                row = db.execute("SELECT 1 FROM direction_interest WHERE scope=? AND interest_id=?",
                                 (scope, interest_id)).fetchone()
                if row is None:
                    raise KeyError("interest not found in scope")
        return self._interest_card(scope, interest_id)

    def record_outcome(self, scope, source_ref, tool, status, error_kind=None):
        scope = self._scope(scope)
        self._source(scope, source_ref)
        if status not in _OUTCOMES:
            raise ValueError("status must be tool_completed, tool_failed, or unknown")
        if not isinstance(tool, str) or not tool.strip() or len(tool) > 256:
            raise ValueError("tool must be a nonempty identifier")
        identifier = hashlib.sha256(repr((scope, source_ref, tool, status)).encode()).hexdigest()
        row = {"id": identifier, "scope": scope, "source_ref": source_ref,
               "tool": tool, "status": status, "error_kind": error_kind,
               "observed_at": time.time()}
        with closing(self._connect()) as db:
            db.execute("INSERT OR IGNORE INTO direction_outcome VALUES(?,?,?,?,?,?,?)", tuple(row.values()))
        return {**row, "goal_success": False}

    def explicit_feedback(self, scope, source_ref, accepted):
        """Host-owner API only. No model handler exposes this operation."""
        scope = self._scope(scope)
        self._source(scope, source_ref)
        if not isinstance(accepted, bool):
            raise ValueError("accepted must be an explicit boolean")
        row = {"id": uuid.uuid4().hex, "scope": scope, "source_ref": source_ref,
               "accepted": accepted, "observed_at": time.time()}
        with closing(self._connect()) as db:
            db.execute("INSERT INTO direction_feedback VALUES(?,?,?,?,?)", tuple(row.values()))
        return row

    def set_share_interval(self, scope, min_share_seconds):
        scope = self._scope(scope)
        seconds = float(min_share_seconds)
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("share interval must be finite and nonnegative")
        with closing(self._connect()) as db:
            self._ensure_scope(db, scope)
            db.execute("UPDATE direction_scope SET min_share_seconds=?,updated_at=? WHERE scope=?",
                       (seconds, time.time(), scope))

    def share_ready(self, scope, now):
        scope = self._scope(scope)
        current = _epoch(now)
        with closing(self._connect()) as db:
            self._ensure_scope(db, scope, current)
            row = db.execute("SELECT min_share_seconds,last_shared_at FROM direction_scope WHERE scope=?",
                             (scope,)).fetchone()
        return row["last_shared_at"] is None or current - row["last_shared_at"] >= row["min_share_seconds"]

    def mark_shared(self, scope, now):
        scope = self._scope(scope)
        current = _epoch(now)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            self._ensure_scope(db, scope, current)
            row = db.execute("SELECT min_share_seconds,last_shared_at FROM direction_scope WHERE scope=?",
                             (scope,)).fetchone()
            if row["last_shared_at"] is not None and current - row["last_shared_at"] < row["min_share_seconds"]:
                db.rollback()
                raise ValueError("minimum share interval has not elapsed")
            db.execute("UPDATE direction_scope SET last_shared_at=?,updated_at=? WHERE scope=?",
                       (current, current, scope))
            db.commit()
        return current

    def observe_share_receipt(self, scope, sent_at):
        """Project a confirmed outbox timestamp; retries cannot move it forward."""
        scope = self._scope(scope)
        stamp = _epoch(sent_at)
        with closing(self._connect()) as db:
            self._ensure_scope(db, scope, stamp)
            db.execute('UPDATE direction_scope SET last_shared_at=? WHERE scope=? '
                'AND (last_shared_at IS NULL OR last_shared_at<?)', (stamp, scope, stamp))

    def cards(self, scope, query="", limit=5):
        scope = self._scope(scope)
        if not 1 <= int(limit) <= 50:
            raise ValueError("limit must be between 1 and 50")
        words = [word for word in str(query).casefold().split() if word]
        with closing(self._connect()) as db:
            rows = [dict(row) for row in db.execute(
                "SELECT interest_id,description,motivation,progress,next_step,source_ref,status,created_at,updated_at "
                "FROM direction_interest "
                "WHERE scope=? ORDER BY updated_at DESC,interest_id", (scope,))]
        if words:
            rows = [row for row in rows if all(word in (row["interest_id"] + " " + row["description"]).casefold()
                                                   for word in words)]
        return [{**row, "scope": scope, "kind": "self_reported_interest_candidate",
                 "is_fact": False, "is_persona": False} for row in rows[:int(limit)]]

    def activity_context(self, scope, limit=3):
        """Return a bounded snapshot of self-reported activity candidates, not facts or persona."""
        scope = self._scope(scope)
        if not 1 <= int(limit) <= 10:
            raise ValueError("limit must be between 1 and 10")
        with closing(self._connect()) as db:
            self._ensure_scope(db, scope)
            state = db.execute("SELECT mode,until_epoch FROM direction_scope WHERE scope=?", (scope,)).fetchone()
            rows = [dict(row) for row in db.execute(
                "SELECT interest_id,description,motivation,progress,next_step,status,source_ref "
                "FROM direction_interest WHERE scope=? AND status IN ('active','paused') "
                "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,updated_at DESC,interest_id LIMIT ?",
                (scope, int(limit)))]
        caps = {"description": 160, "motivation": 100, "progress": 100, "next_step": 100}
        interests = []
        for row in rows:
            item = {key: row[key] for key in ("interest_id", "status", "source_ref")}
            for field, cap in caps.items():
                value = row[field]
                item[field] = value if len(value) <= cap else value[:cap - 1] + "…"
            interests.append(item)
        return {"mode": state["mode"], "until_epoch": state["until_epoch"], "interests": interests}

    @staticmethod
    def _interest_text(value, field):
        if not isinstance(value, str) or len(value) > 500:
            raise ValueError(f"{field} must be a string of at most 500 characters")
        return value.strip()

    def _source(self, scope, source_ref):
        if not isinstance(source_ref, str) or "@" not in source_ref:
            raise ValueError("source_ref must be a versioned record reference")
        record_id, revision = source_ref.rsplit("@", 1)
        if not record_id or not revision.isdigit() or int(revision) < 1:
            raise ValueError("source_ref must contain a positive revision")
        if self.allowed_source is not None and not self.allowed_source(scope, source_ref):
            raise ValueError("source_ref is not allowed in this scope")

    def handlers(self, scope):
        """Model-callable scoped tools. Explicit owner feedback is intentionally absent."""
        scope = self._scope(scope)
        result = {}

        def add(name, description, properties, required, fn):
            schema = {"type": "function", "function": {"name": name, "description": description,
                "parameters": {"type": "object", "properties": properties,
                    "required": required, "additionalProperties": False}}}
            result[name] = (schema, fn)

        async def overview():
            return self.overview(scope)

        async def set_mode(mode, until_epoch=None):
            return self.set_mode(scope, mode, until_epoch)

        async def add_interest(interest_id, description, source_ref, motivation="", next_step=""):
            return self.add_interest(scope, interest_id, description, source_ref, motivation, next_step)

        async def update_interest(interest_id, progress=None, next_step=None, motivation=None, status=None):
            return self.update_interest(scope, interest_id, progress, next_step, motivation, status)

        async def status(interest_id, status):
            return self.set_interest_status(scope, interest_id, status)

        async def procedure_cards(query="", limit=5):
            return self.cards(scope, query, limit)

        add("harness__direction_overview", "查看当前范围的工作/休息状态与自述兴趣候选。",
            {}, [], overview)
        add("harness__direction_set_mode", "设置当前范围的工作或有限期休息/睡眠状态。",
            {"mode": {"type": "string", "enum": ["working", "resting", "sleeping"]},
             "until_epoch": {"type": "number", "description": "resting/sleeping必需，未来Unix时间秒；working忽略"}},
            ["mode"], set_mode)
        result["harness__direction_set_mode"][0]["function"]["parameters"]["anyOf"] = [
            {"properties": {"mode": {"const": "working"}}},
            {"required": ["until_epoch"], "properties": {"mode": {"enum": ["resting", "sleeping"]}}}]
        add("harness__direction_add_interest", "登记带来源引用的自述兴趣候选；不会变成事实或人格。",
            {"interest_id": {"type": "string", "minLength": 1, "maxLength": 128},
             "description": {"type": "string", "minLength": 1, "maxLength": 2000},
             "source_ref": {"type": "string", "minLength": 3},
             "motivation": {"type": "string", "maxLength": 500},
             "next_step": {"type": "string", "maxLength": 500}},
            ["interest_id", "description", "source_ref"], add_interest)
        add("harness__direction_update_interest", "更新自述兴趣候选的动机、进展、下一步或状态；不会变成事实或人格。",
            {"interest_id": {"type": "string", "minLength": 1, "maxLength": 128},
             "progress": {"type": "string", "maxLength": 500},
             "next_step": {"type": "string", "maxLength": 500},
             "motivation": {"type": "string", "maxLength": 500},
             "status": {"type": "string", "enum": sorted(_INTEREST_STATUSES)}},
            ["interest_id"], update_interest)
        add("harness__direction_interest_status", "更新兴趣候选状态。",
            {"interest_id": {"type": "string"},
             "status": {"type": "string", "enum": sorted(_INTEREST_STATUSES)}},
            ["interest_id", "status"], status)
        add("harness__procedure_cards", "检索带原始来源引用的兴趣/程序卡候选。",
            {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
            [], procedure_cards)
        return result
