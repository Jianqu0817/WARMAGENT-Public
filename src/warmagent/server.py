from __future__ import annotations

import argparse
import asyncio
from contextlib import closing, contextmanager
import hmac
import json
import os
from pathlib import Path
import sqlite3
import tomllib
import time

from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from typing import Literal

from myatri.app import load_config
from myatri.providers import ProviderError
from .bridge import Bridge, Conflict, Incoming
from .health import DELIVERY_REASONS


class DeliveryAck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["sent", "unknown"]
    part_index: int | None = Field(default=None, ge=0, le=2)
    reason_code: str | None = Field(default=None, max_length=40)
    elapsed_seconds: float | None = Field(default=None, ge=0, le=3600, allow_inf_nan=False)

    @field_validator("reason_code")
    @classmethod
    def known_reason(cls, value):
        if value is not None and value not in DELIVERY_REASONS:
            raise ValueError("unsupported delivery reason")
        return value


class RuntimeControl(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    scope: str = Field(pattern=r"^[0-9a-f]{32}$")


class RuntimeSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: int = Field(ge=0)
    random_reply_probability: float = Field(ge=0, le=1, allow_inf_nan=False)
    recovery_enabled: bool


class ActivityControl(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action: Literal['pause', 'resume', 'cancel', 'set_focus', 'cancel_wakeup']
    policy: Literal['chat_first', 'focus_first', 'adaptive'] | None = None
    duration_seconds: float = Field(default=900, ge=1, le=3600, allow_inf_nan=False)
    reason: str = Field(default='', max_length=500)
    wake_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    task_id: str | None = Field(default=None, min_length=1, max_length=160)
    revision: int | None = Field(default=None, ge=0)

    @model_validator(mode='after')
    def action_fields(self):
        allowed = {'action', 'policy', 'duration_seconds', 'reason'} if self.action == 'set_focus' else (
            {'action', 'wake_id'} if self.action == 'cancel_wakeup' else {'action', 'task_id', 'revision'})
        if self.model_fields_set - allowed or self.action == 'set_focus' and self.policy is None:
            raise ValueError('invalid activity control fields')
        if (self.task_id is None) != (self.revision is None):
            raise ValueError('task control requires task_id and revision')
        return self


class WorkspaceControl(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    scope: str = Field(min_length=1, max_length=256)
    workspace: str = Field(min_length=1, max_length=160)
    action: Literal['list', 'pin', 'unpin', 'fold', 'unfold', 'note', 'fork', 'mode', 'feedback'] = 'list'
    ref: str = Field(default='', max_length=512)
    revision: int | None = Field(default=None, ge=0)
    text: str = Field(default='', max_length=1200)
    mode: Literal['auto', 'fold', 'expand'] = 'auto'
    accepted: bool | None = None


class ComputerTaskControl(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    task_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    action: Literal['pause','resume','cancel']
    command_id: str = Field(pattern=r'^[0-9a-f]{32}$')


class PendingQuery(BaseModel):
    """``GET /v1/deliveries/pending`` 的查询参数，只允许 scope。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    scope: str = Field(pattern=r"^[0-9a-f]{32}$")
    kind: Literal['computer_report', 'non_proactive'] | None = None


def pending_deliveries(path, scope, limit=5, *, kind=None):
    """只读查询该 scope 下 ``status='prepared'`` 的投递。

    刻意使用只读连接，与 health 一致；这里绝不改动投递状态机，
    claim 仍由 ``POST /v1/deliveries/{key}/claim`` 负责。
    """
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=0.3)) as db:
        db.execute("PRAGMA query_only=ON")
        condition = "AND trigger_kind!='proactive' " if kind == 'non_proactive' else 'AND trigger_kind=? ' if kind else ''
        parameters = (scope, kind, limit) if kind and kind != 'non_proactive' else (scope, limit)
        rows = db.execute(
            "SELECT id,reply,reply_parts,sent_parts,trigger_kind FROM deliveries "
            "WHERE scope=? AND status='prepared' " + condition + "ORDER BY rowid LIMIT ?",
            parameters,
        ).fetchall()
    result = []
    for key, reply, reply_parts, sent_parts, trigger_kind in rows:
        parts = json.loads(reply_parts) if reply_parts else ([reply] if reply else [])
        result.append({
            "id": key,
            "reply_parts": parts,
            "sent_parts": sent_parts,
            "trigger_kind": trigger_kind,
        })
    return result



def create_app(bridge, token):
    if len(token) < 32:
        raise ValueError("bridge token must be at least 32 characters")

    @web.middleware
    async def authenticate(request, handler):
        expected = "Bearer " + token
        if not hmac.compare_digest(request.headers.get("Authorization", ""), expected):
            raise web.HTTPUnauthorized()
        try:
            return await handler(request)
        except Conflict:
            return web.json_response({"error": "state_conflict"}, status=409)
        except (ValueError, ValidationError, json.JSONDecodeError):
            return web.json_response({"error": "invalid_request"}, status=400)
        except KeyError:
            return web.json_response({"error": "not_found"}, status=404)
        except (ProviderError, RuntimeError):
            return web.json_response(
                {"error": "engine_unavailable", "retry_same_message_id": True},
                status=503,
            )

    async def health(request):
        return web.json_response(
            {
                "ok": True,
                "mode": bridge.config.get("mode", "offline"),
                "allowed_groups": len(bridge.config.get("allowed_groups", [])),
            }
        )

    async def messages(request):
        return web.json_response(
            await bridge.receive(Incoming.model_validate(await request.json()))
        )

    async def diagnostics(request):
        return web.json_response(await asyncio.to_thread(bridge.diagnostics))

    async def claim(request):
        return web.json_response(await bridge.claim(request.match_info["key"]))

    async def pending(request):
        # 参数校验与 RuntimeControl 同一风格：pydantic 严格模型 + 中间件统一 400。
        query = PendingQuery.model_validate(dict(request.query))
        return web.json_response(
            {"scope": query.scope, "deliveries": pending_deliveries(bridge.path, query.scope, kind=query.kind)}
        )

    async def acknowledge(request):
        body = DeliveryAck.model_validate(await request.json())
        return web.json_response(
            await bridge.acknowledge(request.match_info["key"], body.status, body.part_index,
                reason_code=body.reason_code, elapsed_seconds=body.elapsed_seconds)
        )

    async def reload_runtime(request):
        body = RuntimeControl.model_validate(await request.json())
        return web.json_response(await bridge.reload_runtime(body.scope))

    async def clear_runtime_context(request):
        body = RuntimeControl.model_validate(await request.json())
        return web.json_response(await bridge.clear_runtime_context(body.scope))

    async def retry_summaries(request):
        body = RuntimeControl.model_validate(await request.json())
        return web.json_response(await bridge.retry_summaries(body.scope))

    async def runtime_settings(request):
        if request.method == "GET":
            return web.json_response({"ok": True, **bridge.runtime_settings.read()})
        body = RuntimeSettingsUpdate.model_validate(await request.json())
        return web.json_response(await bridge.update_runtime_settings(body.revision,
            body.random_reply_probability, body.recovery_enabled))

    async def cleanup(app):
        await bridge.close()

    async def computer_tasks(request):
        agent=bridge.computer_agent
        if not agent.enabled:
            return web.json_response({'ok':True,'enabled':False,'tasks':[]})
        if request.method=='POST':
            body=ComputerTaskControl.model_validate(await request.json())
            result=agent.control(body.task_id,body.action,body.command_id)
            return web.json_response({'ok':True,**result})
        task=request.query.get('id')
        if task:
            if len(task)!=64 or any(c not in '0123456789abcdef' for c in task):
                raise ValueError('invalid_task')
            return web.json_response({'ok':True,'task':agent.store.details(task) |
                                     {'chat_report': bridge.computer_reports.status(task)}})
        return web.json_response({'ok':True,'enabled':True,'permission_profile':agent.profile,'tasks':agent.store.list()})

    async def startup(app):
        bridge.start()

    async def activity(request):
        if request.method == 'POST':
            body = ActivityControl.model_validate(await request.json())
            return web.json_response(bridge.control_activity(body.action,
                **body.model_dump(exclude={'action'}, exclude_unset=True)))
        return web.json_response(bridge.activity_view())

    async def workspaces(request):
        if request.method == 'GET':
            with closing(sqlite3.connect(bridge.workspace_view.path)) as db:
                rows = db.execute('SELECT scope,workspace,revision,current_goal,mode FROM workspace_views ORDER BY rowid DESC LIMIT 50').fetchall()
            return web.json_response({'ok': True, 'workspaces': [dict(zip(
                ('scope', 'workspace', 'revision', 'current_goal', 'mode'), row)) for row in rows]})
        body = WorkspaceControl.model_validate(await request.json())
        args = (body.scope, body.workspace)
        if body.action == 'list':
            view = bridge.workspace_view.list(*args)
        else:
            from .workspace_view import WorkspaceConflict
            try:
                if body.action == 'note':
                    bridge.workspace_view.note(*args, body.ref or 'current_goal', body.text, body.revision)
                elif body.action == 'fork':
                    bridge.workspace_view.fork(*args, body.text, body.revision)
                elif body.action == 'mode':
                    bridge.workspace_view.set_mode(*args, body.mode, body.revision)
                elif body.action == 'feedback':
                    if body.accepted is None:
                        raise ValueError('explicit feedback required')
                    bridge.self_direction.explicit_feedback(body.scope, 'receipt:' + body.ref + '@1', body.accepted)
                else:
                    getattr(bridge.workspace_view, body.action)(*args, body.ref, body.revision)
                view = bridge.workspace_view.list(*args)
            except WorkspaceConflict as error:
                raise Conflict('workspace revision changed') from error
        return web.json_response({'ok': True, 'view': view})

    app = web.Application(middlewares=[authenticate], client_max_size=65536)
    app.add_routes(
        [
            web.get("/health", health),
            web.get("/v1/diagnostics", diagnostics),
            web.post("/v1/admin/reload", reload_runtime),
            web.post("/v1/admin/clear-context", clear_runtime_context),
            web.post("/v1/admin/retry-summaries", retry_summaries),
            web.get("/v1/admin/runtime-settings", runtime_settings),
            web.post("/v1/admin/runtime-settings", runtime_settings),
            web.get('/v1/admin/computer-tasks',computer_tasks),
            web.get('/v1/admin/activity', activity),
            web.post('/v1/admin/activity', activity),
            web.post('/v1/admin/computer-tasks',computer_tasks),
            web.post("/v1/messages", messages),
            web.get("/v1/deliveries/pending", pending),
            web.post("/v1/deliveries/{key}/claim", claim),
            web.post("/v1/deliveries/{key}/ack", acknowledge),
        ]
    )
    app.on_cleanup.append(cleanup)
    app.on_startup.append(startup)
    app.router.add_get('/v1/admin/workspaces', workspaces)
    app.router.add_post('/v1/admin/workspaces', workspaces)
    return app


@contextmanager
def service_lock(path):
    """Prevent two service processes from claiming the same delivery ledger."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        import os

        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def main():
    started = time.perf_counter()
    def progress(stage):
        print(f'[bridge startup] {stage} pid={os.getpid()} elapsed={time.perf_counter()-started:.2f}s', flush=True)
    progress('python_entry')
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/bridge.local.toml")
    args = parser.parse_args()
    file = Path(args.config).resolve()
    progress('reading_config')
    root = file.parent.parent
    settings = tomllib.loads(file.read_text(encoding="utf-8"))["bridge"]
    if settings.get("host", "127.0.0.1") != "127.0.0.1":
        raise ValueError("this version supports localhost only")
    if settings.get("mode", "offline") not in {"offline", "live"}:
        raise ValueError("mode must be offline or live")
    engine = load_config(root / settings["engine_config"])
    engine["chat"]["allow_paid"] = settings["mode"] == "live"
    token = (root / settings["token_file"]).read_text(encoding="ascii").strip()
    state_root = (root / settings["state_root"]).resolve()
    if root not in state_root.parents:
        raise ValueError("group state must stay inside the bridge project")
    with service_lock(root / "var/service.lock"):
        progress('initializing_bridge')
        bridge = Bridge(state_root, settings, engine, root / settings["persona_file"])
        progress('binding_http')
        # Do not log tokens, request contents or query strings.
        web.run_app(
            create_app(bridge, token),
            host="127.0.0.1",
            port=settings.get("port", 8766),
            access_log=None,
            print=lambda _message: progress('http_listening'),
        )


if __name__ == "__main__":
    main()
