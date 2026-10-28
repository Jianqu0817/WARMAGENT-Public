"""Loopback-only control proxy. The browser never receives the bridge token."""
from pathlib import Path
import tomllib

import httpx


async def runtime_settings_proxy(project, payload=None, *, task_query=None, computer=False, activity=False, workspace=False):
    project = Path(project).resolve()
    try:
        settings = tomllib.loads((project / "config/bridge.local.toml").read_text(encoding="utf-8-sig"))["bridge"]
        port = settings.get("port", 8766)
        if settings.get("host", "127.0.0.1") != "127.0.0.1" or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid loopback destination")
        token_path = (project / settings["token_file"]).resolve()
        if not token_path.is_relative_to(project):
            raise ValueError("invalid token location")
        token = token_path.read_text(encoding="ascii").strip()
        if len(token) < 32:
            raise ValueError("invalid token")
        async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
            endpoint = 'workspaces' if workspace else 'computer-tasks' if computer else 'activity' if activity else 'runtime-settings'
            response = await client.request("GET" if payload is None else "POST",
                f"http://127.0.0.1:{port}/v1/admin/{endpoint}",
                headers={"Authorization": "Bearer " + token},
                **({'params':{'id':task_query}} if task_query else {}),
                **({"json": payload} if payload is not None else {}))
        if response.status_code == 409:
            return {"error": "活动状态已变化，请重新读取后操作。" if activity else
                    "设置已被其他窗口修改，请重新读取后保存。"}, 409
        if not response.is_success:
            return {"error": "机器人未提供新版设置接口，请正常重启桥接服务后再试。"}, 503
        data = response.json()
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise ValueError("unconfirmed response")
        if workspace:
            return data, 200
        if activity:
            activity_view = data.get("activity")
            harness = data.get("direct_harness")
            if "activity" not in data or not isinstance(harness, dict):
                raise ValueError("invalid activity response")
            if activity_view is not None:
                if not isinstance(activity_view, dict):
                    raise ValueError("invalid activity state")
                original = activity_view
                activity_view = {key: value[:2000] if isinstance(value, str) else None
                    for key, value in ((name, activity_view.get(name))
                    for name in ("goal", "status", "summary", "next_step"))}
                for key in ('focus_policy', 'focus_reason'):
                    if isinstance(original.get(key), str):
                        activity_view[key] = original[key][:500]
                for key in ('control_revision', 'focus_until'):
                    if type(original.get(key)) in (int, float):
                        activity_view[key] = original[key]
                wake = original.get('next_wakeup')
                if isinstance(wake, dict):
                    activity_view['next_wakeup'] = {key: value[:1500] if isinstance(value, str) else value
                        for key, value in wake.items() if key in ('wake_id', 'due_at', 'resume_note', 'status')
                        and type(value) in (str, int, float)}
                elif 'next_wakeup' in original:
                    activity_view['next_wakeup'] = None
            inbox = data.get('inbox')
            scheduler = data.get('scheduler')
            schedule_view = {key: value for key, value in scheduler.items()
                if key in ('status', 'reason', 'next_check_at', 'last_step_started_at', 'last_state_updated_at',
                    'rest_until', 'receipt_count', 'scan_interval_seconds', 'quiet_period_seconds',
                    'last_poll_at', 'last_result', 'last_error')
                and (value is None or type(value) in (str, int, float))} if isinstance(scheduler, dict) else None
            extra = {'inbox': {key: value for key, value in inbox.items()
                if key in ('pending', 'processing', 'completed', 'ignored', 'deferred', 'needs_attention')
                and type(value) is int and value >= 0}} if isinstance(inbox, dict) else {}
            tools = harness.get("tools")
            slots = data.get('tasks')
            if isinstance(slots, list):
                extra['tasks'] = [{key: value for key, value in row.items()
                    if key in ('task_id', 'revision', 'status', 'filter_job', 'deadline', 'resume_note')
                    and type(value) in (str, int, float)} for row in slots[:100] if isinstance(row, dict)]
            return {"ok": True,
                **({'scheduler': schedule_view} if schedule_view is not None else {}),
                **extra,
                "activity": activity_view,
                "direct_harness": {
                    "enabled": harness.get("enabled") if isinstance(harness.get("enabled"), bool) else None,
                    "mode": harness.get("mode")[:80] if isinstance(harness.get("mode"), str) else None,
                    "tools": [item[:120] for item in tools[:32] if isinstance(item, str)] if isinstance(tools, list) else [],
                    "profile": harness.get("profile")[:80] if isinstance(harness.get("profile"), str) else None,
                    "error": harness.get("error")[:500] if isinstance(harness.get("error"), str) else None,
                }}, 200
        if computer or activity:
            return data,200
        from .server import RuntimeSettingsUpdate
        view = RuntimeSettingsUpdate.model_validate({k: v for k, v in data.items() if k != "ok"})
        return {"ok": True, **view.model_dump()}, 200
    except httpx.TimeoutException:
        return {"error": "操作超时，保存结果未确认；请重新读取设置，勿连续保存。"}, 504
    except (OSError, ValueError, KeyError, TypeError, AttributeError, httpx.HTTPError):
        return {"error": "无法连接本机新版机器人服务，请检查其运行状态。"}, 503
