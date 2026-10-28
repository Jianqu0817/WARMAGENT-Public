"""Durable coordinator for Xiaowennuan's single autonomous idle activity.

The coordinator owns scheduling and the activity snapshot. ``run_step`` owns
one bounded model/tool step and must return only after that step's tool receipts
are durable. Unknown in-flight outcomes are never replayed automatically.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime
import inspect
import json
from pathlib import Path
import sqlite3
import time
import uuid
import math
from contextvars import ContextVar, Token
from typing import Any, Callable
from zoneinfo import ZoneInfo


_ACTIVE = {"active", "running", "paused", "unknown", "rest", "resting",
           "waiting_external", "blocked"}
_TERMINAL = {"completed", "cancelled"}
_STEP_REVISION: ContextVar[int | None] = ContextVar("activity_step_revision", default=None)


def step_revision_context(revision: int):
    """Bind the control revision captured when the current runner step began."""
    return _STEP_REVISION.set(int(revision))


def reset_step_revision_context(token: Token) -> None:
    _STEP_REVISION.reset(token)


class ActivityCoordinator:
    """Coordinate one activity through caller-supplied availability and runner.

    Args:
        db_path: SQLite file shared across process restarts.
        run_step: ``(activity, *, prompt, reconcile_only)`` callback. It may be
            synchronous or async and performs at most one tool/effect boundary.
            With ``activity is None`` it may return ``{"action":"create", ...}``
            to start an optional activity, or ``None``/``{"action":"rest"}``.
        available: synchronous or async bool, or mapping with online,
            authorized, busy and budget_available booleans. False/missing
            required capability prevents any model call.
        notify_owner: optional synchronous or async callback for a returned
            ``notify_owner`` text. Integrations should use a controlled outbox.
    """

    def __init__(
        self,
        db_path: str | Path,
        run_step: Callable[..., Any],
        available: Callable[[], Any],
        *,
        notify_owner: Callable[[str], Any] | None = None,
        interval: float = 600,
        quiet_period: float = 180,
        clock: Callable[[], float] = time.time,
        prompt_path: str | Path | None = None,
        cost_quiet_hours: list[str] | None = None,
        cost_quiet_timezone: str = 'Asia/Shanghai',
    ):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_step = run_step
        self.available = available
        self.notify_owner = notify_owner
        self.interval = float(interval)
        self.quiet_period = float(quiet_period)
        self.clock = clock
        self.cost_quiet_timezone = ZoneInfo(cost_quiet_timezone)
        self.cost_quiet_hours = []
        for window in cost_quiet_hours or []:
            start, end = (datetime.strptime(value.strip(), '%H:%M').time() for value in window.split('-'))
            if start >= end:
                raise ValueError('cost_quiet_hours requires same-day start < end')
            self.cost_quiet_hours.append((start, end))
        self.prompt_path = Path(prompt_path) if prompt_path else Path(__file__).with_name("prompts") / "idle.md"
        self._lock = asyncio.Lock()
        with self._db() as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS idle_activity (
                id INTEGER PRIMARY KEY CHECK(id=1), goal TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '', next_step TEXT NOT NULL DEFAULT '',
                device TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'empty',
                protocol TEXT NOT NULL DEFAULT '{}', receipts TEXT NOT NULL DEFAULT '[]',
                background INTEGER NOT NULL DEFAULT 0, last_chat REAL,
                last_check REAL, pause_requested INTEGER NOT NULL DEFAULT 0,
                resume_requested INTEGER NOT NULL DEFAULT 0,
                interruption_notified INTEGER NOT NULL DEFAULT 0,
                rest_until REAL, updated REAL NOT NULL DEFAULT 0,
                control_revision INTEGER NOT NULL DEFAULT 0,
                focus_policy TEXT NOT NULL DEFAULT 'chat_first', focus_until REAL,
                focus_reason TEXT NOT NULL DEFAULT ''
            )""")
            columns = {r[1] for r in db.execute("PRAGMA table_info(idle_activity)")}
            for name, declaration in (("control_revision", "INTEGER NOT NULL DEFAULT 0"),
                                      ("focus_policy", "TEXT NOT NULL DEFAULT 'chat_first'"),
                                      ("focus_until", "REAL"), ("focus_reason", "TEXT NOT NULL DEFAULT ''")):
                if name not in columns:
                    db.execute(f"ALTER TABLE idle_activity ADD COLUMN {name} {declaration}")
            db.execute("""CREATE TABLE IF NOT EXISTS activity_wakeups (
                wake_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, call_id TEXT NOT NULL,
                due_at REAL NOT NULL, delay_seconds REAL NOT NULL DEFAULT 0, resume_note TEXT NOT NULL, expected_revision INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'scheduled', created REAL NOT NULL,
                UNIQUE(task_id, call_id))""")
            wake_columns = {r[1] for r in db.execute("PRAGMA table_info(activity_wakeups)")}
            if "delay_seconds" not in wake_columns:
                db.execute("ALTER TABLE activity_wakeups ADD COLUMN delay_seconds REAL NOT NULL DEFAULT 0")
            db.execute("INSERT OR IGNORE INTO idle_activity(id) VALUES(1)")
            # Older builds could persist a resting status without its wake time.
            # Give only that state a bounded deadline; paused/unknown/cancelled
            # snapshots remain untouched and retain their explicit controls.
            db.execute("""UPDATE idle_activity SET rest_until=updated+?
                WHERE id=1 AND status='resting' AND rest_until IS NULL""",
                (self.interval,))
            db.execute("UPDATE idle_activity SET last_check=COALESCE(last_check,?) WHERE id=1",
                       (self.clock(),))
            # A process died during a runner step. Preserve its protocol and
            # receipts, but require explicit reconciliation before any further step.
            db.execute("""UPDATE idle_activity SET status='unknown', pause_requested=0,
                resume_requested=0, updated=? WHERE id=1 AND status='running'""", (self.clock(),))
        from .activity_planning import ActivityPlans
        self.plans = ActivityPlans(self.path)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return closing(db)

    def _row(self) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT * FROM idle_activity WHERE id=1").fetchone()
        value = dict(row)
        value["protocol"] = _json(value["protocol"], {})
        value["receipts"] = _json(value["receipts"], [])
        value["background"] = bool(value["background"])
        value["pause_requested"] = bool(value["pause_requested"])
        value["resume_requested"] = bool(value["resume_requested"])
        value["interruption_notified"] = bool(value["interruption_notified"])
        return value

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any] | None:
        if row["status"] == "empty":
            return None
        return {key: row[key] for key in (
            "goal", "summary", "next_step", "device", "status", "protocol",
            "receipts", "background", "last_chat", "pause_requested",
            "interruption_notified", "rest_until", "updated", "control_revision",
            "focus_policy", "focus_until", "focus_reason",
        )}

    def _next_wakeup(self):
        row = self._row()
        task_id = row.get("protocol", {}).get("activity_id")
        if not task_id:
            return None
        with self._db() as db:
            wake = db.execute("SELECT * FROM activity_wakeups WHERE task_id=? AND status='scheduled' ORDER BY due_at LIMIT 1", (str(task_id),)).fetchone()
        if not wake:
            return None
        return {"wake_id": wake["wake_id"], "due_at": wake["due_at"], "resume_note": wake["resume_note"],
                "status": wake["status"], "task_id": wake["task_id"]}

    def _overview(self):
        result = self._public(self._row())
        if result is not None:
            result["next_wakeup"] = self._next_wakeup()
        return result

    async def _call(self, callback, *args, **kwargs):
        value = callback(*args, **kwargs)
        return await value if inspect.isawaitable(value) else value

    async def tick(self, now: float | None = None) -> dict[str, Any]:
        """Check scheduling and perform no more than one bounded runner step."""
        stamp = self.clock() if now is None else float(now)
        async with self._lock:
            row = self._row()
            pending_notification = row["protocol"].get("notification_pending")
            if pending_notification and pending_notification.get("text") and self.notify_owner:
                try:
                    notification_result = await self._call(self.notify_owner, pending_notification["text"])
                except Exception as exc:
                    return {"ran": False, "reason": "notification_retry_pending",
                            "error": type(exc).__name__, "activity": self._public(row)}
                self._ack_notification(pending_notification["text"], stamp, notification_result)
                return {"ran": False, "reason": "notification_deferred" if isinstance(notification_result, dict)
                        and notification_result.get('status') == 'deferred' else "notification_queued",
                        "activity": self.overview()}
            quiet_until = self.cost_quiet_until(stamp)
            if quiet_until is not None:
                # Do not consume wakeups, resume requests or owner plans while
                # waiting. The original activity resumes at a later safe tick.
                return {'ran': False, 'reason': 'cost_quiet_hours',
                        'next_check_at': quiet_until, 'activity': self._public(row)}
            status = row["status"]
            owner_plan_pending = self.plans.pending()
            had_activity = status not in {"empty", *_TERMINAL}
            if status in _TERMINAL:
                # Terminal snapshot remains inspectable; a later opportunity can
                # propose a fresh activity without creating parallel authority.
                activity = None
            else:
                activity = self._public(row)

            # Resume a chat-paused activity only after both sides have been quiet.
            if status == "paused":
                quiet = stamp - float(row["last_chat"]) if row['last_chat'] is not None else 0
                if row["resume_requested"] or row['last_chat'] is not None and quiet >= self.quiet_period:
                    with self._db() as db, db:
                        db.execute("""UPDATE idle_activity SET status='active',
                            pause_requested=0, resume_requested=0, last_check=NULL,
                            updated=? WHERE id=1 AND status='paused'""", (stamp,))
                    row = self._row()
                    status = row["status"]
                    activity = self._public(row)
            wake = self._due_wakeup(row, stamp)
            if wake == "woke":
                row = self._row()
                status, activity = row["status"], self._public(row)
            if status in {"rest", "resting"}:
                if not owner_plan_pending and not row["resume_requested"] and (row["rest_until"] is None or stamp < float(row["rest_until"])):
                    if wake != "woke":
                        return {"ran": False, "reason": "rest", "activity": activity}
                if wake != "woke":
                    protocol = dict(row['protocol'])
                    protocol['wakeup_context'] = {
                        'event_id': 'rest:' + str(row['rest_until']), 'origin': 'host',
                        'reason': ('owner_plan_pending' if owner_plan_pending else
                            'rest_elapsed' if not row['resume_requested'] else 'resume_requested'),
                        'due_at': row['rest_until'], 'resumed_at': stamp,
                        'next_step': row['next_step']}
                    with self._db() as db, db:
                        db.execute("""UPDATE idle_activity SET status='active',rest_until=NULL,protocol=?,updated=?
                            WHERE id=1 AND status IN ('rest','resting')""",
                            (json.dumps(protocol, ensure_ascii=False), stamp))
                    row = self._row()
                    status, activity = row["status"], self._public(row)
            if status in {"paused", "unknown", "waiting_external", "blocked"} and not row["resume_requested"]:
                return {"ran": False, "reason": status, "activity": activity}

            # A fresh idle opportunity needs 10 minutes since its previous
            # check. Existing activities are checked at that same cadence.
            if not owner_plan_pending and not had_activity and row["last_check"] is not None and stamp - float(row["last_check"]) < self.interval:
                return {"ran": False, "reason": "interval", "activity": activity}
            if (not row["resume_requested"] and not row["background"] and row["last_chat"] is not None
                    and stamp - float(row["last_chat"]) < self.quiet_period):
                return {"ran": False, "reason": "chat_quiet_period", "activity": activity}

            capability = await self._call(self.available)
            if not _available(capability):
                return {"ran": False, "reason": "unavailable", "activity": activity}

            # Availability may await an integration callback. Revalidate the
            # control snapshot before claiming so a concurrent pause/stop wins.
            with self._db() as db, db:
                db.execute("BEGIN IMMEDIATE")
                latest = db.execute("SELECT * FROM idle_activity WHERE id=1").fetchone()
                if latest["control_revision"] != row["control_revision"] or latest["status"] != status:
                    return {"ran": False, "reason": "control_changed", "activity": self._overview()}
                changed = db.execute("""UPDATE idle_activity SET status='running',last_check=?,
                    pause_requested=0, updated=? WHERE id=1 AND control_revision=? AND status=?""",
                                     (stamp, stamp, row["control_revision"], status)).rowcount
                if changed != 1:
                    return {"ran": False, "reason": "control_changed", "activity": self._overview()}
            uncertain_calls = row["protocol"].get("uncertain_calls")
            reconcile_only = status == "unknown" or bool(uncertain_calls)
            # Claim the step durably before invoking model/tools. On crash this
            # transitions to unknown at the next coordinator construction.
            step_revision = self._row()["control_revision"]
            state = self._public(self._row())
            if not had_activity:
                state = None
            elif reconcile_only and state is not None:
                # Tell the runner the persisted step may have had an effect;
                # it must inspect receipts/state before deciding what to do.
                state["status"] = "unknown"
                state["outcome_unknown"] = True
            before = row
            prompt = self.prompt_path.read_text(encoding="utf-8")
            token = step_revision_context(step_revision)
            try:
                result = await self._call(self.run_step, state, prompt=prompt,
                                          reconcile_only=reconcile_only)
                step_revision = _STEP_REVISION.get() or step_revision
            except Exception as exc:
                step_revision = _STEP_REVISION.get() or step_revision
                with self._db() as db, db:
                    db.execute("""UPDATE idle_activity SET status='unknown',resume_requested=0,updated=?
                        WHERE id=1 AND control_revision=? AND status='running'""",
                               (self.clock(), step_revision))
                    still_current = db.execute("SELECT control_revision,status FROM idle_activity WHERE id=1").fetchone()
                    if still_current["control_revision"] != step_revision:
                        return {"ran": True, "reason": "control_changed", "activity": self._overview()}
                return {"ran": True, "reason": "runner_error", "error": type(exc).__name__,
                        "activity": self._public(self._row())}
            finally:
                reset_step_revision_context(token)

            current = self._row()
            if current["control_revision"] != step_revision:
                self._merge_late_result(current, result, stamp)
                return {"ran": True, "reason": "control_changed", "activity": self._overview()}
            owner_created = (not had_activity and
                current['protocol'].get('owner_instruction', {}).get('status') == 'applied')
            merge_base = current if had_activity or owner_created else _fresh_checkpoint(before, current)
            saved = _merge_result(merge_base, result, now=stamp, had_activity=had_activity or owner_created,
                                  interval=self.interval)
            if self._next_wakeup() is not None and saved["status"] == "resting":
                # A runner-created explicit wake replaces the generic rest timer.
                saved["rest_until"] = None
            if current["status"] == "cancelled":
                saved["status"] = "cancelled"
                saved["pause_requested"] = False
            if current["pause_requested"] and saved["status"] in {"active", "running", "resting"}:
                saved["status"] = "paused"
                saved["pause_requested"] = True
            # Unknown effects are displayable and never automatically replayed.
            if current['status'] != 'cancelled' and result and (result.get("unknown") is True or result.get("status") == "unknown"):
                saved["status"] = "unknown"
                saved["resume_requested"] = False
            with self._db() as db, db:
                db.execute("""UPDATE idle_activity SET goal=?,summary=?,next_step=?,device=?,
                    status=?,protocol=?,receipts=?,background=?,pause_requested=?,
                    resume_requested=0,interruption_notified=?,rest_until=?,updated=? WHERE id=1""", (
                    saved["goal"], saved["summary"], saved["next_step"], saved["device"],
                    saved["status"], json.dumps(saved["protocol"], ensure_ascii=False),
                    json.dumps(saved["receipts"], ensure_ascii=False), int(saved["background"]),
                    int(saved["pause_requested"]), int(saved["interruption_notified"]),
                    saved["rest_until"], stamp))
            notification = result.get("notify_owner") if isinstance(result, dict) else None
            if notification and self.notify_owner:
                try:
                    notification_result = await self._call(self.notify_owner, str(notification))
                except Exception as exc:
                    return {"ran": True, "reason": "notification_retry_pending",
                            "error": type(exc).__name__, "activity": self._public(self._row())}
                self._ack_notification(str(notification), stamp, notification_result)
            return {"ran": True, "reason": "step", "activity": self._public(self._row())}

    def _ack_notification(self, text: str, stamp: float, result=None) -> None:
        row = self._row()
        protocol = row["protocol"]
        pending = protocol.get("notification_pending")
        if not pending or pending.get("text") != text:
            return
        protocol.pop("notification_pending", None)
        protocol['last_notification'] = {
            **(result if isinstance(result, dict) else {'status': 'queued'}), 'at': stamp}
        with self._db() as db, db:
            db.execute("UPDATE idle_activity SET protocol=?,updated=? WHERE id=1",
                       (json.dumps(protocol, ensure_ascii=False), stamp))

    def _due_wakeup(self, row: dict[str, Any], stamp: float):
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            latest = db.execute("SELECT * FROM idle_activity WHERE id=1").fetchone()
            protocol = _json(latest["protocol"], {})
            task_id = str(protocol.get("activity_id", ""))
            if not task_id:
                return None
            wakes = db.execute("SELECT * FROM activity_wakeups WHERE task_id=? AND status='scheduled' AND due_at<=? ORDER BY due_at", (task_id, stamp)).fetchall()
            for wake in wakes:
                if latest["status"] == "paused" and wake["expected_revision"] == latest["control_revision"]:
                    continue
                if latest["status"] not in {"active", "running", "rest", "resting"} or wake["expected_revision"] != latest["control_revision"]:
                    db.execute("UPDATE activity_wakeups SET status='stale' WHERE wake_id=?", (wake["wake_id"],))
                    continue
                protocol["wakeup_context"] = {"wake_id": wake["wake_id"], "resume_note": wake["resume_note"], "due_at": wake["due_at"]}
                changed = db.execute("""UPDATE idle_activity SET status='active',rest_until=NULL,protocol=?,updated=?
                    WHERE id=1 AND control_revision=? AND status=?""",
                                     (json.dumps(protocol, ensure_ascii=False), stamp, latest["control_revision"], latest["status"])).rowcount
                if changed == 1:
                    fired = db.execute("UPDATE activity_wakeups SET status='fired' WHERE wake_id=? AND status='scheduled'", (wake["wake_id"],)).rowcount
                    if fired == 1:
                        return "woke"
        return None

    def _merge_late_result(self, current, result, stamp):
        data = result if isinstance(result, dict) else {}
        rest_until = None
        if data.get("action") == "rest" and current.get("pause_requested"):
            rest_until = data.get("rest_until", stamp + self.interval)
        with self._db() as db, db:
            row = db.execute("SELECT protocol,receipts FROM idle_activity WHERE id=1").fetchone()
            protocol = _json(row["protocol"], {})
            if isinstance(data.get("protocol"), dict):
                protocol.update(data["protocol"])
            receipts = _merge_receipts(_json(row["receipts"], []), data.get("receipts", []))
            db.execute("""UPDATE idle_activity SET protocol=?,receipts=?,
                rest_until=COALESCE(?,rest_until),
                status=CASE WHEN status='running' AND pause_requested=1 THEN 'paused' ELSE status END,
                updated=? WHERE id=1""",
                       (json.dumps(protocol, ensure_ascii=False), json.dumps(receipts, ensure_ascii=False), rest_until, stamp))

    def on_owner_message(self, now: float | None = None) -> dict[str, Any]:
        """Record a private-owner message and request a boundary pause once."""
        stamp = self.clock() if now is None else float(now)
        with self._db() as db, db:
            row = db.execute("SELECT * FROM idle_activity WHERE id=1").fetchone()
            is_activity = row["status"] in _ACTIVE
            background = bool(row["background"])
            manual_paused = row["status"] == "paused" and row["last_chat"] is None and bool(row["pause_requested"])
            pause = is_activity and not background and row["status"] in {"active", "running"}
            notify = is_activity and not bool(row["interruption_notified"])
            db.execute("""UPDATE idle_activity SET last_chat=?, pause_requested=?,
                status=CASE WHEN status='active' AND ? THEN 'paused' ELSE status END,
                interruption_notified=CASE WHEN ? THEN 1 ELSE interruption_notified END,
                control_revision=control_revision+1, updated=? WHERE id=1""", (None if manual_paused else stamp,
                                             int(manual_paused or pause), int(pause), int(notify), stamp))
            if is_activity and not manual_paused:
                # Chat interruption preserves a wake plan; quiet-time resume
                # will be the authority that lets it run again.
                db.execute("UPDATE activity_wakeups SET expected_revision=expected_revision+1 WHERE task_id=? AND status='scheduled'",
                           (str(_json(row["protocol"], {}).get("activity_id", "")),))
            activity = dict(row)
        summary = ""
        if notify:
            summary = activity["summary"] or activity["goal"] or "我刚才在做一项自主活动。"
        return {"activity_summary": summary, "pause_requested": pause,
                "background_continues": is_activity and background,
                "activity": self.overview()}

    def on_chat_delivered(self, now: float | None = None) -> None:
        stamp = self.clock() if now is None else float(now)
        with self._db() as db, db:
            db.execute("""UPDATE idle_activity SET last_chat=CASE
                WHEN status='paused' AND pause_requested=1 AND last_chat IS NULL THEN NULL
                ELSE ? END,updated=? WHERE id=1""", (stamp, stamp))

    def claim_resume(self, now: float | None = None) -> dict[str, Any]:
        """Queue an explicit continue; unknown state is reconciled, never replayed blindly."""
        stamp = self.clock() if now is None else float(now)
        with self._db() as db, db:
            row = db.execute("SELECT status FROM idle_activity WHERE id=1").fetchone()
            if row["status"] not in {"paused", "unknown", "rest", "resting", "blocked"}:
                return {"accepted": False, "reason": "not_resumable", "activity": self.overview()}
            db.execute("UPDATE idle_activity SET resume_requested=1,control_revision=control_revision+1,updated=? WHERE id=1", (stamp,))
            db.execute("UPDATE idle_activity SET last_check=NULL WHERE id=1")
        return {"accepted": True, "activity": self.overview()}

    def pause(self, now: float | None = None) -> dict[str, Any] | None:
        """Pause an activity by owner/admin command without chat auto-resume."""
        stamp = self.clock() if now is None else float(now)
        with self._db() as db, db:
            db.execute("""UPDATE idle_activity SET status='paused',pause_requested=1,
                resume_requested=0,last_chat=NULL,control_revision=control_revision+1,updated=? WHERE id=1
                AND status NOT IN ('empty','completed','cancelled')""", (stamp,))
        return self.overview()

    def stop(self, now: float | None = None) -> dict[str, Any]:
        stamp = self.clock() if now is None else float(now)
        with self._db() as db, db:
            db.execute("""UPDATE idle_activity SET status='cancelled',pause_requested=0,
                resume_requested=0,control_revision=control_revision+1,updated=? WHERE id=1 AND status NOT IN ('empty','completed','cancelled')""", (stamp,))
            db.execute("UPDATE activity_wakeups SET status='cancelled' WHERE status='scheduled' AND task_id=?",
                       (str(_json(db.execute("SELECT protocol FROM idle_activity WHERE id=1").fetchone()[0], {}).get("activity_id", "")),))
            db.execute("UPDATE activity_plans SET status='cancelled' WHERE status IN ('pending','applied')")
        return self.overview()

    def overview(self) -> dict[str, Any] | None:
        return self._overview()

    def cost_quiet_until(self, now=None):
        if not self.cost_quiet_hours:
            return None
        stamp = self.clock() if now is None else float(now)
        local = datetime.fromtimestamp(stamp, self.cost_quiet_timezone)
        ends = [local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0).timestamp()
                for start, end in self.cost_quiet_hours if start <= local.time() < end]
        return max(ends) if ends else None

    def schedule_view(self, now=None):
        """Earliest eligibility from saved controls, not a promise of execution."""
        stamp = self.clock() if now is None else float(now)
        row = self._row()
        status = row['status']
        due, reason = None, status
        if status == 'running':
            reason = 'step_running'
        elif status in {'paused', 'unknown', 'blocked', 'waiting_external'} and not row['resume_requested']:
            if status == 'paused' and row['last_chat'] is not None:
                due, reason = row['last_chat'] + self.quiet_period, 'chat_quiet_period'
        elif status in {'rest', 'resting'} and not row['resume_requested']:
            due, reason = row['rest_until'], 'rest'
            wake = self._next_wakeup()
            if wake and (due is None or wake['due_at'] < due):
                due, reason = wake['due_at'], 'scheduled_wakeup'
        elif status == 'empty' or status in _TERMINAL:
            due = (row['last_check'] + self.interval) if row['last_check'] is not None else stamp
            reason = 'idle_scan'
        else:
            due, reason = stamp, 'resume_requested' if row['resume_requested'] else 'ready'
        if self.plans.pending() and status in {'empty', *_TERMINAL, 'rest', 'resting', 'active'}:
            due, reason = stamp, 'owner_plan_pending'
        if due is not None and not row['background'] and not row['resume_requested'] and row['last_chat'] is not None:
            quiet_due = row['last_chat'] + self.quiet_period
            if quiet_due > due:
                due, reason = quiet_due, 'chat_quiet_period'
        if due is not None:
            deferred_until = self.cost_quiet_until(max(due, stamp))
            if deferred_until is not None:
                due, reason = deferred_until, 'cost_quiet_hours'
        return {'status': status, 'reason': reason, 'next_check_at': due,
            'cost_quiet_until': self.cost_quiet_until(stamp),
            'cost_quiet_timezone': str(self.cost_quiet_timezone),
            'last_step_started_at': row['last_check'], 'last_state_updated_at': row['updated'],
            'rest_until': row['rest_until'], 'receipt_count': len(row['receipts']),
            'scan_interval_seconds': self.interval, 'quiet_period_seconds': self.quiet_period}

    def set_focus(self, policy: str, *, duration_seconds: float = 900,
                  reason: str = "", now: float | None = None):
        if policy not in {"chat_first", "focus_first", "adaptive"}:
            raise ValueError("unsupported_focus_policy")
        if not isinstance(reason, str) or len(reason) > 500:
            raise ValueError("invalid_focus_reason")
        duration = None
        if policy != "chat_first":
            duration = float(duration_seconds)
            if not math.isfinite(duration) or not 1 <= duration <= 3600:
                raise ValueError("invalid_focus_duration")
        stamp = self.clock() if now is None else float(now)
        until = None if duration is None else stamp + duration
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT control_revision FROM idle_activity WHERE id=1").fetchone()[0]
            expected = _STEP_REVISION.get()
            if expected is not None and current != expected:
                raise ValueError("stale_activity_control")
            db.execute("UPDATE idle_activity SET focus_policy=?,focus_until=?,focus_reason=?,control_revision=control_revision+1,updated=? WHERE id=1 AND control_revision=?",
                       (policy, until, reason, stamp, current))
            revision = current + 1
        if _STEP_REVISION.get() is not None:
            _STEP_REVISION.set(revision)
        return self.overview()

    def should_defer_owner(self, *, urgent: bool = False, now: float | None = None) -> bool:
        if urgent:
            return False
        with self._db() as db:
            row = db.execute("SELECT status,focus_policy,focus_until FROM idle_activity WHERE id=1").fetchone()
        if row["status"] not in {"active", "running", "resting"}:
            return False
        stamp = self.clock() if now is None else float(now)
        if row["focus_until"] is None or stamp >= row["focus_until"]:
            return False
        if row["focus_policy"] == "focus_first":
            return True
        if row["focus_policy"] == "adaptive":
            if row["status"] == "resting":
                return False
            if row["status"] == "running":
                return True
            with self._db() as db:
                pending_count = db.execute("""SELECT CASE WHEN json_valid(protocol)
                    THEN COALESCE(json_array_length(json_extract(protocol,'$.pending_calls')),0)
                    ELSE 0 END FROM idle_activity WHERE id=1""").fetchone()[0]
            return pending_count > 0
        return False

    def schedule_wakeup(self, *, delay_seconds: float, resume_note: str, call_id: str,
                        now: float | None = None) -> dict[str, Any]:
        delay = float(delay_seconds)
        if not math.isfinite(delay) or not 1 <= delay <= 604800:
            raise ValueError("invalid_wakeup_delay")
        if not isinstance(resume_note, str) or len(resume_note) > 1500:
            raise ValueError("invalid_resume_note")
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("invalid_call_id")
        row = self._row()
        expected = _STEP_REVISION.get()
        if expected is not None and expected != row["control_revision"]:
            raise ValueError("stale_activity_control")
        task_id = str(row["protocol"].get("activity_id", ""))
        if not task_id or row["status"] in {"empty", "completed", "cancelled"}:
            raise ValueError("no_active_activity")
        stamp = self.clock() if now is None else float(now)
        due = stamp + delay
        with self._db() as db, db:
            old = db.execute("SELECT * FROM activity_wakeups WHERE task_id=? AND call_id=?", (task_id, call_id)).fetchone()
            if old:
                if old["delay_seconds"] != delay or old["resume_note"] != resume_note:
                    raise ValueError("wakeup_call_id_conflict")
                return _wake_public(old)
            if _STEP_REVISION.get() is not None:
                rev = db.execute("SELECT control_revision FROM idle_activity WHERE id=1").fetchone()[0]
                if rev != expected:
                    raise ValueError("stale_activity_control")
            wake_id = uuid.uuid4().hex
            db.execute("INSERT INTO activity_wakeups(wake_id,task_id,call_id,due_at,delay_seconds,resume_note,expected_revision,status,created) VALUES(?,?,?,?,?,?,?, 'scheduled',?)",
                       (wake_id, task_id, call_id, due, delay, resume_note, row["control_revision"], stamp))
            if row["status"] in {"rest", "resting"}:
                db.execute("UPDATE idle_activity SET rest_until=NULL WHERE id=1")
        if _STEP_REVISION.get() is not None:
            _STEP_REVISION.set(row["control_revision"])
        return {"wake_id": wake_id, "due_at": due, "resume_note": resume_note, "status": "scheduled", "task_id": task_id}

    def cancel_wakeup(self, *, wake_id: str | None = None, now: float | None = None) -> dict[str, Any]:
        stamp = self.clock() if now is None else float(now)
        row = self._row()
        task_id = str(row["protocol"].get("activity_id", ""))
        with self._db() as db, db:
            if wake_id is None:
                cur = db.execute("UPDATE activity_wakeups SET status='cancelled' WHERE task_id=? AND status='scheduled'", (task_id,))
            else:
                cur = db.execute("UPDATE activity_wakeups SET status='cancelled' WHERE wake_id=? AND task_id=? AND status='scheduled'", (wake_id, task_id))
            return {"cancelled": cur.rowcount > 0, "wake_id": wake_id, "task_id": task_id, "updated": stamp}

    def checkpoint(self, *, expected_revision: int | None = None, **changes) -> dict[str, Any] | None:
        """Durably checkpoint protocol/receipts before a tool boundary.

        Accepted fields: goal, summary, next_step, device, protocol, receipts,
        background, status. This is intended for the root's per-tool wrapper.
        """
        allowed = {"goal", "summary", "next_step", "device", "protocol", "receipts", "background", "status"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError("unsupported_activity_checkpoint_field")
        if "status" in changes and changes["status"] not in {
            "active", "running", "paused", "waiting_external", "rest", "resting",
            "completed", "unknown", "blocked", "cancelled",
        }:
            raise ValueError("unsupported_activity_status")
        values = {}
        for key, value in changes.items():
            if key in {"protocol", "receipts"}:
                value = json.dumps(value, ensure_ascii=False)
            elif key == "background":
                value = int(bool(value))
            values[key] = value
        if values:
            expected = _STEP_REVISION.get() if expected_revision is None else expected_revision
            with self._db() as db, db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT * FROM idle_activity WHERE id=1").fetchone()
                if expected is not None and current["control_revision"] != expected:
                    values = {k: v for k, v in values.items() if k in {"protocol", "receipts"}}
                    # Protocol checkpoints still merge from the newest durable
                    # protocol, preserving owner-written context keys.
                    if "protocol" in values:
                        protocol = _json(current["protocol"], {})
                        protocol.update(_json(values["protocol"], {}))
                        values["protocol"] = json.dumps(protocol, ensure_ascii=False)
                    if "receipts" in values:
                        values["receipts"] = json.dumps(_merge_receipts(_json(current["receipts"], []), _json(values["receipts"], [])), ensure_ascii=False)
                if "status" in values:
                    if current["status"] in _TERMINAL:
                        values["status"] = current["status"]
                    elif current["status"] == "paused" and current["pause_requested"]:
                        values["status"] = "paused"
                if values:
                    assignments = ",".join(f"{key}=?" for key in values)
                    db.execute(f"UPDATE idle_activity SET {assignments},updated=? WHERE id=1",
                               (*values.values(), self.clock()))
        return self.overview()

    def update_protocol(self, protocol: dict[str, Any], *, receipts=None,
                        next_step: str | None = None, summary: str | None = None):
        """Persist protocol progress and merge any newly confirmed receipts."""
        changes: dict[str, Any] = {"protocol": protocol}
        if next_step is not None:
            changes["next_step"] = next_step
        if summary is not None:
            changes["summary"] = summary
        if receipts is not None:
            existing = (self.overview() or {}).get("receipts", [])
            changes["receipts"] = _merge_receipts(existing, receipts)
        return self.checkpoint(**changes)

    def record_receipt(self, receipt: dict[str, Any]):
        """Append one confirmed host receipt idempotently."""
        if not isinstance(receipt, dict):
            raise TypeError("receipt_must_be_mapping")
        existing = (self.overview() or {}).get("receipts", [])
        return self.checkpoint(receipts=_merge_receipts(existing, [receipt]))

    save = checkpoint

    def should_pause(self) -> bool:
        """Cheap tool-boundary check for the root runner."""
        row = self._row()
        return row["pause_requested"] or row["status"] in {"paused", "cancelled", "unknown"}


def _json(value: str, fallback):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _wake_public(row):
    return {"wake_id": row["wake_id"], "due_at": row["due_at"],
            "resume_note": row["resume_note"], "status": row["status"],
            "task_id": row["task_id"]}


def _available(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if not isinstance(value, dict):
        return False
    return all(value.get(key) is True for key in ("online", "authorized", "budget_available")) and not value.get("busy", True)


def _fresh_checkpoint(before: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Carry fields checkpointed during a first proposal without reviving the
    previous completed singleton snapshot as a second activity."""
    fresh = {"goal": "", "summary": "", "next_step": "", "device": "",
             "status": "empty", "protocol": {}, "receipts": [], "background": False,
             "pause_requested": current["pause_requested"], "resume_requested": False,
             "rest_until": None}
    for key in ("goal", "summary", "next_step", "device", "protocol", "receipts", "background"):
        if current.get(key) != before.get(key):
            fresh[key] = current[key]
    return fresh


def _merge_receipts(existing, incoming):
    result = list(existing or [])
    known = {str(x.get("id", x.get("call_id", ""))) for x in result if isinstance(x, dict)}
    for item in incoming or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("id", item.get("call_id", "")))
        if not key or key not in known:
            result.append(item)
            if key:
                known.add(key)
    return result


def _merge_result(current: dict[str, Any], result: Any, *, now: float,
                  had_activity: bool | None = None, interval: float = 600) -> dict[str, Any]:
    data = result if isinstance(result, dict) else {}
    had_activity = current["status"] not in {"empty", *_TERMINAL} if had_activity is None else had_activity
    action = data.get("action", "continue" if had_activity else ("create" if data.get("goal") else "none"))
    if action == "create" and not had_activity:
        base = {"goal": "", "summary": "", "next_step": "", "device": "",
                "status": "active", "protocol": {}, "receipts": [],
                "background": False, "pause_requested": False, "resume_requested": False,
                "interruption_notified": False, "rest_until": None}
    elif not had_activity:
        base = {"goal": "", "summary": "", "next_step": "", "device": "",
                "status": "empty", "protocol": {}, "receipts": [],
                "background": False, "pause_requested": False, "resume_requested": False,
                "interruption_notified": current.get("interruption_notified", False), "rest_until": None}
    else:
        base = current.copy()
        base["status"] = "active"
    if action == "rest":
        base["status"] = "resting" if had_activity else "empty"
        requested_until = data.get("rest_until")
        base["rest_until"] = (now + interval) if had_activity and requested_until is None else requested_until
    elif action in {"complete", "completed"} or data.get("status") == "completed":
        base["status"] = "completed"
    elif action in {"cancel", "cancelled"} or data.get("status") == "cancelled":
        base["status"] = "cancelled"
    elif action == "blocked" or data.get("status") == "blocked":
        base["status"] = "blocked"
    elif action in {"none", "continue", "create"} and data.get("status") in {
        "active", "running", "paused", "waiting_external", "rest", "resting", "blocked", "unknown",
    }:
        base["status"] = data["status"]
    elif action == "none" and not had_activity:
        base["status"] = "empty"
    for key in ("goal", "summary", "next_step", "device", "protocol", "background"):
        if key in data and data[key] is not None:
            base[key] = data[key]
    if base["status"] == "resting" and base.get("rest_until") is None:
        base["rest_until"] = now + interval
    receipts = base["receipts"]
    known = {str(x.get("id", x.get("call_id", ""))) for x in receipts if isinstance(x, dict)}
    for receipt in data.get("receipts", []) if isinstance(data.get("receipts", []), list) else []:
        if not isinstance(receipt, dict):
            continue
        key = str(receipt.get("id", receipt.get("call_id", "")))
        if key and key not in known:
            receipts.append(receipt)
            known.add(key)
        elif not key:
            receipts.append(receipt)
    return {**base, "receipts": receipts, "updated": now}
