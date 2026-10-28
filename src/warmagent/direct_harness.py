"""Direct, owner-gated access to the mounted DeepSeek Harness tools."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from contextlib import closing, asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

import jsonschema

from .dsh_client import CREATION_FLAGS, INHERITED_ENV
from .background_jobs import BackgroundJobs
from .tool_arguments import normalize_arguments


class _NodeToolHost:
    def __init__(self, settings):
        self.settings = settings
        self.process = None
        self.reader = None
        self.pending = {}
        self.serial = 0
        self.catalog = []
        self._ready = None
        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self.on_job_event = None
        self.on_host_lost = None

    async def start(self):
        async with self._start_lock:
            return await self._start_host()

    async def _start_host(self):
        if self.process and self.process.returncode is None:
            return self.catalog
        node = self.settings.get("node_path")
        root = self.settings.get("runtime_root")
        if not node or not root:
            raise RuntimeError("harness_node_path_or_runtime_root_missing")
        host = Path(__file__).with_name("harness") / "tool-host.mjs"
        native_config = self.settings.get("native_config")
        config_path = Path(native_config) if native_config else None
        if config_path and not config_path.is_absolute():
            config_path = Path(self.settings.get("project_root", Path.cwd())) / config_path
        default_directory = (self.settings.get("default_working_directory")
                             or self.settings.get("project_root") or Path(__file__).resolve().parents[2])
        args = [str(node), str(host), str(root)]
        if config_path and config_path.is_file():
            args.append(str(config_path))
        else:
            args.append("")
        args.append(str(Path(default_directory).resolve()))
        args.append(json.dumps({"enabled": self.settings.get("vision_enabled", False),
            "model": self.settings.get("vision_model"), "endpoint": self.settings.get("vision_endpoint"),
            "root": str(Path(self.settings.get("project_root", Path.cwd())) / "var/harness/vision")}))
        env = {key: os.environ[key] for key in INHERITED_ENV
               if key in os.environ and key != "DSH_HOME"}
        env["NO_COLOR"] = "1"
        self.process = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, cwd=str(root), env=env,
            creationflags=CREATION_FLAGS, limit=4_000_000,
        )
        self._ready = asyncio.get_running_loop().create_future()
        self.reader = asyncio.create_task(self._read_loop())
        try:
            packet = await asyncio.wait_for(self._ready, 45)
        except Exception:
            await self.close()
            raise RuntimeError("harness_unavailable") from None
        self.catalog = packet["tools"]
        return self.catalog

    async def _read_loop(self):
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                packet = json.loads(line)
                if packet.get("kind") == "job_event":
                    if self.on_job_event:
                        try:
                            self.on_job_event(packet)
                        except Exception:
                            pass
                    continue
                if packet.get("kind") == "ready":
                    if self._ready and not self._ready.done():
                        self._ready.set_result(packet)
                    continue
                if packet.get("kind") == "startup_error":
                    if self._ready and not self._ready.done():
                        self._ready.set_exception(RuntimeError("harness_unavailable"))
                    continue
                future = self.pending.pop(packet.get("id"), None)
                if future and not future.done():
                    future.set_result(packet)
        except Exception:
            pass
        finally:
            if self.on_host_lost:
                try:
                    self.on_host_lost()
                except Exception:
                    pass
            if self._ready and not self._ready.done():
                self._ready.set_exception(RuntimeError("harness_unavailable"))
            for future in self.pending.values():
                if not future.done():
                    future.set_result({"kind": "result", "error": "outcome_unknown"})
            self.pending.clear()

    async def execute(self, name, args, call_id, *, session_id, session_slot,
                      scope="", task="", turn=""):
        if not self.process or self.process.returncode is not None:
            await self.start()
        self.serial += 1
        identifier = self.serial
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        packet = {"kind": "execute", "id": identifier, "name": name,
                  "args": args, "call_id": call_id,
                  "session_id": session_id, "session_slot": session_slot,
                  "scope": scope, "task": task, "turn": turn}
        async with self._write_lock:
            self.process.stdin.write((json.dumps(packet, ensure_ascii=False) + "\n").encode())
            await self.process.stdin.drain()
        response = await future
        if response.get("error"):
            raise RuntimeError(response["error"])
        return response.get("result")

    async def close(self):
        process, self.process = self.process, None
        if not process:
            return
        if process.returncode is None:
            try:
                process.stdin.write(b'{"kind":"close"}\n')
                await process.stdin.drain()
                await asyncio.wait_for(process.wait(), 5)
            except Exception:
                process.kill()
                await process.wait()
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
            self.reader = None


class DirectHarness:
    """Mount full local Harness tools and expose them without an execution LLM."""

    def __init__(self, settings, state_root, owner_id, allowed_routes, mode, *, executor=None,
                 on_job_event=None):
        self.settings = dict(settings or {})
        self.state_root = Path(state_root)
        self.owner_id = owner_id
        self.owner_check = None
        self.allowed_routes = frozenset(allowed_routes or ())
        self.mode = mode
        self.executor = executor or _NodeToolHost(self.settings)
        self.background_jobs = BackgroundJobs(self.state_root / "background-jobs.sqlite3")
        self.on_job_event = on_job_event
        self._unmatched_job_events = {}
        if isinstance(self.executor, _NodeToolHost):
            self.executor.on_job_event = self._handle_job_event
            self.executor.on_host_lost = self.background_jobs.mark_running_unknown
        self._catalog = {}
        self._started = False
        self._start_lock = asyncio.Lock()
        self._execution_lock = asyncio.Lock()
        self._pending_actions = 0
        self.action_queue_limit = int(self.settings.get('action_queue_limit', 64))
        if self.action_queue_limit < 1:
            raise ValueError('action_queue_limit must be positive')
        self.activity_paused = None
        self.db_path = self.state_root / "direct-harness.sqlite3"
        self._init_store()

    def _init_store(self):
        self.state_root.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db_path, timeout=5)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS calls(
                activity TEXT NOT NULL, turn TEXT NOT NULL, call_id TEXT NOT NULL,
                owner TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
                result TEXT, updated REAL NOT NULL,
                PRIMARY KEY(activity,turn,call_id))""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(calls)")}
            for column, declaration in (("scope", "TEXT"),
                                        ("original_arguments", "TEXT"),
                                        ("normalized_arguments", "TEXT")):
                if column not in columns:
                    db.execute(f"ALTER TABLE calls ADD COLUMN {column} {declaration}")

    async def start(self):
        async with self._start_lock:
            return await self._start_catalog()

    async def _start_catalog(self):
        if self._started:
            return
        raw = await self.executor.start()
        if isinstance(raw, dict):
            raw = raw.get("tools", raw)
        if not isinstance(raw, (list, tuple, dict)):
            raise RuntimeError("harness_catalog_invalid")
        rows = raw.values() if isinstance(raw, dict) else raw
        catalog = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = row.get("name")
            parameters = row.get("parameters", row.get("inputSchema"))
            if not isinstance(name, str) or not name or not isinstance(parameters, dict):
                continue
            catalog[name] = {"name": name,
                             "description": str(row.get("description", "")),
                             "parameters": parameters}
        self._catalog = catalog
        self._started = True

    async def close(self):
        self._started = False
        self.background_jobs.mark_running_unknown()
        await self.executor.close()

    def _handle_job_event(self, event):
        if not isinstance(event, dict):
            return
        scope, task = event.get("scope"), event.get("task")
        if not isinstance(scope, str) or not isinstance(task, str):
            return
        if self.background_jobs.observe_completed(scope, task, event):
            if self.on_job_event:
                self.on_job_event(event)
        elif isinstance(event.get("job_id"), str) and len(self._unmatched_job_events) < 256:
            self._unmatched_job_events[event["job_id"]] = event

    def _drain_job_event(self, job_id, scope, task):
        event = self._unmatched_job_events.pop(job_id, None)
        if event and self.background_jobs.observe_completed(scope, task, event) and self.on_job_event:
            self.on_job_event(event)

    async def catalog(self):
        if not self._live():
            return {}
        await self.start()
        return {"harness__" + name: self._schema(definition)
                for name, definition in self._catalog.items()}

    @staticmethod
    def _schema(definition):
        return {"type": "function", "function": {
            "name": "harness__" + definition["name"],
            "description": definition["description"],
            "parameters": deepcopy(definition["parameters"]),
        }}

    def _live(self):
        if isinstance(self.mode, dict):
            live = self.mode.get("live") is True or self.mode.get("mode") == "live"
            full = self.mode.get("permission_profile", "full") == "full"
        else:
            live, full = self.mode == "live", True
        profile = self.settings.get("permission_profile", self.settings.get("profile", "full"))
        return live and full and profile == "full" and self.settings.get("enabled", False) is True

    @staticmethod
    def _principal(raw):
        if raw is None:
            return None, None
        if isinstance(raw, dict):
            source = raw.get("channel_source", raw.get("source", raw))
            kind = raw.get("author_kind")
        else:
            payload = getattr(raw, "payload", {})
            source = payload.get("channel_source", payload.get("source")) if isinstance(payload, dict) else None
            kind = getattr(raw, "author_kind", None)
        return (dict(source) if isinstance(source, dict) else None), kind

    def _owner_authorized(self, raw):
        source, author_kind = self._principal(raw)
        return bool(self.owner_id and self.settings.get("owner_id") == self.owner_id
                    and author_kind == "human" and isinstance(source, dict)
                    and (self.owner_check(source) if self.owner_check else
                         source.get('transport') != 'onebot' and source.get("sender_id") == self.owner_id)
                    and source.get("sender_id") != source.get("bot_id")
                    and all(isinstance(source.get(key), str) and source[key].strip()
                            for key in ("platform_id", "bot_id", "group_id", "message_id"))
                    and not source.get("self_initiated", False)
                    and f"{source['platform_id']}|{source['group_id']}" in self.allowed_routes)

    def _autonomy_authorized(self):
        autonomy = self.settings.get("autonomy", {})
        granted = (self.settings.get("autonomy_granted") is True
                   or isinstance(autonomy, dict) and autonomy.get("granted") is True)
        return bool(self.owner_id and self.settings.get("owner_id") == self.owner_id and granted)

    @staticmethod
    def _context_key(context):
        if not isinstance(context, dict):
            raise ValueError("direct_harness_context_required")
        activity = context.get("activity_id", "conversation")
        turn = context.get("turn_id")
        if not isinstance(activity, str) or not activity or not isinstance(turn, (str, int)):
            raise ValueError("direct_harness_turn_identity_required")
        turn = str(turn)
        if not turn:
            raise ValueError("direct_harness_turn_identity_required")
        return activity, turn

    async def handlers(self, raw, *, context, autonomous_grant=False):
        context = deepcopy(context)
        user_authorized = self._owner_authorized(raw)
        auto_authorized = autonomous_grant is True and self._autonomy_authorized()
        if not self._live() or not (user_authorized or auto_authorized):
            return {}
        await self.start()
        result = {}
        receipt_schema = {"type": "function", "function": {"name": "harness_read_receipt",
            "description": "Read a stored Harness receipt from the current activity and turn.",
            "parameters": {"type": "object", "properties": {
                "call_id": {"type": "string"}, "activity": {"type": "string"},
                "turn": {"type": "string"}, "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 0, "maximum": 4000, "default": 3000}},
                "required": ["call_id"], "additionalProperties": False}}}
        async def receipt_invoke(**kwargs):
            raise ValueError("direct_harness_call_requires_host_call_id")
        async def receipt_call(call_id, args, *, _context=context):
            if not isinstance(args, dict):
                return {"error": "invalid_tool_arguments"}
            try:
                jsonschema.validate(args, receipt_schema["function"]["parameters"])
            except Exception:
                return {"error": "invalid_tool_arguments"}
            current_activity, current_turn = self._context_key(_context)
            activity = args.get("activity", current_activity)
            turn = str(args.get("turn", current_turn))
            return self._read_receipt(_context, activity, turn, args["call_id"],
                                      args.get("offset", 0), args.get("limit", 3000))
        receipt_invoke.execute_call = receipt_call
        result["harness_read_receipt"] = (receipt_schema, receipt_invoke)
        for native_name, definition in self._catalog.items():
            alias = "harness__" + native_name

            async def invoke(**kwargs):
                raise ValueError("direct_harness_call_requires_host_call_id")

            async def execute_call(call_id, args, *, _native_name=native_name,
                                   _context=context, _allowed=(user_authorized or auto_authorized)):
                return await self._execute(_native_name, args, _context, call_id,
                                           authorized=_allowed)

            invoke.execute_call = execute_call
            result[alias] = (self._schema(definition), invoke)
        return result

    async def execute(self, name, args, context, call_id):
        raw = context.get("principal") if isinstance(context, dict) else None
        authorized = self._owner_authorized(raw)
        auto = isinstance(context, dict) and context.get("autonomous_grant") is True
        authorized = authorized or (auto and self._autonomy_authorized())
        if not authorized:
            return {"error": "computer_owner_required"}
        if not self._live():
            return {"error": "computer_offline_or_disabled"}
        if isinstance(context, dict) and context.get("device", "host") != "host":
            return {"error": "computer_device_unavailable"}
        await self.start()
        native_name = name[len("harness__"):] if name.startswith("harness__") else name
        return await self._execute(native_name, args, context, call_id, authorized=authorized)

    async def _execute(self, name, args, context, call_id, *, authorized):
        # Native tools share a working directory and physical host. A chat can
        # think while an activity is running, but their tool effects serialize.
        async with self._action_slot() as accepted:
            if not accepted:
                return {'error': 'device_queue_full', 'not_executed': True}
            if context.get('activity_id') and self.activity_paused and self.activity_paused():
                return {'error': 'activity_paused_before_action', 'not_executed': True}
            return await self._execute_one(name, args, context, call_id, authorized=authorized)

    @asynccontextmanager
    async def _action_slot(self):
        if self._pending_actions >= self.action_queue_limit:
            yield False
            return
        self._pending_actions += 1
        try:
            async with self._execution_lock:
                yield True
        finally:
            self._pending_actions -= 1

    async def execute_local(self, name, args, context, call_id, perform, *, authorized, normalized_args=None):
        """Run a local adapter through the same authority, lock and durable effect ledger."""
        if not authorized or not self._live():
            return {'error': 'computer_owner_required_or_offline'}
        if not isinstance(call_id, str) or not call_id or context.get('device', 'host') != 'host':
            return {'error': 'invalid_host_call'}
        async with self._action_slot() as accepted:
            if not accepted:
                return {'error': 'device_queue_full', 'not_executed': True}
            if context.get('activity_id') and self.activity_paused and self.activity_paused():
                return {'error': 'activity_paused_before_action', 'not_executed': True}
            activity, turn = self._context_key(context)
            canonical_args = args if normalized_args is None else normalized_args
            fingerprint = hashlib.sha256(json.dumps([name, canonical_args], sort_keys=True,
                ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
            claimed, cached = self._claim(activity, turn, call_id, fingerprint,
                context.get('scope', ''), args, canonical_args)
            if not claimed:
                return self._model_receipt(cached, activity, turn, call_id)
            try:
                outcome = await perform()
                result = {'host_receipt_id': call_id, 'result': outcome}
                if isinstance(outcome, dict) and outcome.get('image_manifest'):
                    result['image_manifest'] = outcome['image_manifest']
                state = 'completed'
            except Exception:
                result, state = {'host_receipt_id': call_id, 'error': 'outcome_unknown'}, 'unknown'
            self._receipt(activity, turn, call_id, result, state)
            return self._model_receipt(result, activity, turn, call_id)

    async def _execute_one(self, name, args, context, call_id, *, authorized):
        if not authorized:
            return {"error": "computer_owner_required"}
        if not self._live():
            return {"error": "computer_offline_or_disabled"}
        if context.get("device", "host") != "host":
            return {"error": "computer_device_unavailable"}
        if name not in self._catalog:
            return {"error": "unknown_harness_tool"}
        if not isinstance(call_id, str) or not call_id.strip():
            return {"error": "host_call_id_required"}
        if not isinstance(args, dict):
            return {"error": "invalid_tool_arguments"}
        try:
            normalized_args = normalize_arguments(args, self._catalog[name]["parameters"])
            jsonschema.validate(normalized_args, self._catalog[name]["parameters"])
            activity, turn = self._context_key(context)
        except Exception:
            return {"error": "invalid_tool_arguments"}
        fingerprint = hashlib.sha256(json.dumps(
            [name, normalized_args], sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        scope = context.get("scope", "")
        if name == "pwsh" and normalized_args.get("run_in_background") is True and not scope:
            return {"error": "direct_harness_scope_required"}
        claim, cached = self._claim(activity, turn, call_id, fingerprint, scope,
                                    args, normalized_args)
        if not claim:
            return self._model_receipt(cached, activity, turn, call_id)
        try:
            session_id, session_slot = self._session_identity(context, activity, turn)
            if isinstance(self.executor, _NodeToolHost):
                outcome = await self.executor.execute(name, normalized_args, call_id,
                    session_id=session_id, session_slot=session_slot, scope=scope,
                    task=activity, turn=turn)
            else:
                outcome = await self.executor.execute(name, normalized_args, call_id)
            if not isinstance(outcome, dict):
                outcome = {"content": outcome}
            if (isinstance(self.executor, _NodeToolHost) and name == "pwsh"
                    and normalized_args.get("run_in_background") is True):
                value = outcome.get("value", {})
                if (isinstance(value, dict) and value.get("kind") == "background"
                        and isinstance(value.get("jobId"), str)):
                    self.background_jobs.record(scope, activity, value["jobId"], turn=turn,
                        session_id=session_id, receipt_ref={"activity": activity,
                            "turn": turn, "call_id": call_id, "job_id": value["jobId"]})
                    self._drain_job_event(value["jobId"], scope, activity)
            result = {"host_receipt_id": call_id, "result": outcome}
            if outcome.get("image_manifest"):
                result["image_manifest"] = outcome["image_manifest"]
            self._receipt(activity, turn, call_id, result, "completed")
        except Exception:
            result = {"host_receipt_id": call_id, "error": "outcome_unknown"}
            self._receipt(activity, turn, call_id, result, "unknown")
        return self._model_receipt(result, activity, turn, call_id)

    @staticmethod
    def _session_identity(context, activity, turn):
        scope = context.get("scope", "")
        is_activity = activity != "conversation"
        role = "activity" if is_activity else "chat-scope"
        identity = activity if is_activity else (scope or turn)
        slot = hashlib.sha256(json.dumps([scope, role], ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")).hexdigest()[:40]
        session_id = hashlib.sha256(json.dumps([scope, role, identity], ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")).hexdigest()[:48]
        return "warmagent-" + session_id, "warmagent-" + slot

    def _model_receipt(self, result, activity, turn, call_id):
        receipt_ref = {"activity": activity, "turn": turn, "call_id": call_id}
        encoded = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
        if result.get("error") == "call_identity_conflict":
            return result
        nested = result.get('result') if isinstance(result.get('result'), dict) else {}
        status = '结果未知' if result.get('error') == 'outcome_unknown' else '工具报错' if (
            result.get('error') or nested.get('isError') or nested.get('error')) else '工具返回'
        markdown = f'**{status}** · 调用 `{call_id}`；结构化字段和原始回执为准。'
        model_result = {**result, 'receipt_ref': receipt_ref, 'summary_markdown': markdown}
        if len(json.dumps(model_result, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) <= 5800:
            return model_result
        name = hashlib.sha256(json.dumps([activity, turn, call_id]).encode()).hexdigest()
        path = self.state_root / 'direct-receipts' / (name + '.json')
        locator = {'database': str(self.db_path), 'activity': activity, 'turn': turn, 'call_id': call_id}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(encoded, encoding='utf-8')
            locator = {'file': str(path.resolve())}
        except OSError:
            pass  # The authoritative complete receipt is already in SQLite.
        preview = {'host_receipt_id': call_id, 'receipt_ref': receipt_ref, 'result_complete': False,
            'summary_markdown': markdown,
            'result_preview': encoded.encode('utf-8')[:4000].decode('utf-8', errors='ignore'),
            'full_receipt': locator,
            **({'image_manifest': result['image_manifest']} if result.get('image_manifest') else {}),
            'note': '回执较长，仅预览前段；完整结果已持久化，需更多时读取full_receipt，不要重做动作。'}
        while len(json.dumps(preview, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > 5800 and preview['result_preview']:
            preview['result_preview'] = preview['result_preview'][:len(preview['result_preview']) // 2]
        return preview

    def _claim(self, activity, turn, call_id, fingerprint, scope, original_arguments,
               normalized_arguments):
        with closing(sqlite3.connect(self.db_path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT fingerprint,state,result,scope FROM calls "
                             "WHERE activity=? AND turn=? AND call_id=?",
                             (activity, turn, call_id)).fetchone()
            if row:
                if row[3] != scope:
                    return False, {"error": "call_identity_conflict"}
                if row[0] != fingerprint:
                    return False, {"error": "call_identity_conflict"}
                if row[1] == "completed" and row[2]:
                    return False, json.loads(row[2])
                return False, {"host_receipt_id": call_id, "error": "outcome_unknown"}
            db.execute("""INSERT INTO calls(activity,turn,call_id,owner,fingerprint,state,result,updated,
                       scope,original_arguments,normalized_arguments)
                       VALUES(?,?,?,?,?,'running',NULL,?,?,?,?)""",
                       (activity, turn, call_id, self.owner_id or "", fingerprint, time.time(), scope,
                        json.dumps(original_arguments, ensure_ascii=False),
                        json.dumps(normalized_arguments, ensure_ascii=False)))
        return True, None

    def _read_receipt(self, context, activity, turn, call_id, offset, limit):
        scope = context.get("scope", "")
        current_activity, current_turn = self._context_key(context)
        with closing(sqlite3.connect(self.db_path, timeout=5)) as db:
            row = db.execute("SELECT result,scope FROM calls WHERE activity=? AND turn=? AND call_id=?",
                             (activity, turn, call_id)).fetchone()
        if not row or not row[0]:
            return {"error": "receipt_not_found"}
        # Scoped records can be fetched across turns in the same scope. Legacy
        # records have no scope metadata, so restrict them to this exact context.
        if not row[1] and (activity != current_activity or turn != current_turn):
            return {"error": "receipt_scope_denied"}
        if row[1] is not None and row[1] != scope:
            return {"error": "receipt_scope_denied"}
        full_text = row[0]
        start = min(offset, len(full_text))
        end_limit = min(start + limit, len(full_text))
        low, high = start, end_limit
        best = None
        while low <= high:
            end = (low + high) // 2
            next_offset = end if end < len(full_text) else None
            candidate = {"receipt_ref": {"activity": activity, "turn": turn,
                                          "call_id": call_id},
                         "text": full_text[start:end], "total_chars": len(full_text),
                         "next_offset": next_offset}
            encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) <= 5800:
                best = candidate
                low = end + 1
            else:
                high = end - 1
        return best

    def _receipt(self, activity, turn, call_id, result, state):
        with closing(sqlite3.connect(self.db_path, timeout=5)) as db, db:
            db.execute("UPDATE calls SET state=?,result=?,updated=? "
                       "WHERE activity=? AND turn=? AND call_id=?",
                       (state, json.dumps(result, ensure_ascii=False), time.time(),
                        activity, turn, call_id))
