"""Prepare files and compose the existing core, terminal and direct transport."""
import asyncio
from contextlib import AsyncExitStack, contextmanager
import json
import logging
import re
import secrets
import shutil
import tomllib


def read_settings(config):
    file = config.resolve()
    root = file.parent.parent
    settings = tomllib.loads(file.read_text(encoding="utf-8-sig"))["bridge"]
    state = (root / settings["state_root"]).resolve()
    if not state.is_relative_to(root) or state == root:
        raise ValueError("记忆库必须位于本项目目录内。")
    return root, settings, state


def wechat_file(args):
    return args.wechat_config or args.config.resolve().parent / "wechatagent.local.toml"


def onebot_file(args):
    return getattr(args, 'onebot_config', None) or args.config.resolve().parent / 'onebot.local.toml'


def prepare(args):
    file = args.config.resolve()
    root = file.parent.parent
    for target, template in ((file, root / "config/bridge.example.toml"),
                             (wechat_file(args), root / "config/wechatagent.example.toml"),
                             (onebot_file(args), root / 'config/onebot.example.toml')):
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(template, target)
    _, settings, _ = read_settings(file)
    token = root / settings["token_file"]
    token.parent.mkdir(parents=True, exist_ok=True)
    if not token.exists():
        with token.open("x", encoding="ascii") as stream:
            stream.write(secrets.token_urlsafe(48))
    template = root / "config/harness/executor.example.json"
    target = template.with_name("executor.local.json")
    if template.exists() and not target.exists():
        shutil.copyfile(template, target)
    print("准备完成：仅补齐缺失的本地模板与令牌，已有文件保留。")


def check(args):
    from myatri.app import load_config
    from .server import create_app, service_lock  # noqa: F401
    from .wechat import WeChatRunner  # noqa: F401
    root, settings, state = read_settings(args.config)
    load_config(root / settings["engine_config"])
    if settings.get("host", "127.0.0.1") != "127.0.0.1":
        raise ValueError("管理API仅支持127.0.0.1。")
    if settings.get("mode", "offline") not in {"offline", "live"}:
        raise ValueError("mode必须为offline或live。")
    if not (root / settings["persona_file"]).is_file():
        raise ValueError("角色卡不存在。")
    if len((root / settings["token_file"]).read_text(encoding="ascii").strip()) < 32:
        raise ValueError("管理令牌长度不足。")
    tomllib.loads(wechat_file(args).read_text(encoding="utf-8-sig"))["wechatagent"]
    from .onebot_client import OneBotSettings
    OneBotSettings.load(onebot_file(args))
    print("通过：核心/传输入口导入、bridge/engine/wechat TOML、角色卡、令牌、库路径。")
    print(f"库路径：{state}\n未连接微信、未检查端口/凭据/外部运行区、未调用模型。")


@contextmanager
def locked(root):
    from .server import service_lock
    lock = service_lock(root / "var/service.lock")
    try:
        lock.__enter__()
    except OSError:
        raise RuntimeError("群聊或另一个私聊正在使用同一份库，请先正常退出。") from None
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def engine_for(root, settings, mode):
    from myatri.app import load_config
    settings["mode"] = mode
    engine = load_config(root / settings["engine_config"])
    engine["chat"]["allow_paid"] = mode == "live"
    return engine


def versions(args):
    from pathlib import Path
    from urllib.parse import urlsplit

    from .wechat_versions import SUPPORTED, TABLE_FILE, UPGRADE_GUIDE
    print(f"微信兼容表：{TABLE_FILE.name}；升级指南：{UPGRADE_GUIDE}")
    for spec in SUPPORTED.values():
        line = f"- 微信 {spec.wechat_version} · WeChatAgent {spec.wechatagent_runtime} · {spec.client_dir}"
        if spec.notes:
            line += f" · {spec.notes}"
        print(line)
    file = wechat_file(args)
    if not file.is_file():
        print("未找到微信本地配置；跳过运行区实况。")
        return
    data = tomllib.loads(file.read_text(encoding="utf-8-sig"))["wechatagent"]
    directory = Path(data.get("project_dir", ""))
    if not directory:
        print("微信本地配置缺少 project_dir；跳过运行区实况。")
        return
    if not directory.is_absolute():
        directory = file.resolve().parent.parent / directory
    build = directory / "state/hook/native/build.json"
    if build.is_file():
        record = json.loads(build.read_text(encoding="utf-8-sig"))
        target = record.get("targetVersion")
        state = "在表内" if target in SUPPORTED else "不在表内"
        print(f"运行区 hook 构建：targetVersion={target}（{state}）integration={record.get('integration')}")
    else:
        print("运行区缺少 state/hook/native/build.json。")
    hook_root = directory / "state/hook"
    if hook_root.is_dir():
        clients = sorted(path.name for path in hook_root.glob("wechat-*") if path.is_dir())
        print("客户端目录：" + ("、".join(clients) if clients else "无"))
    token_file = directory / data.get("native_token", "state/hook/native-token.txt")
    url = data.get("native_url", "http://127.0.0.1:30001")
    if token_file.is_file() and urlsplit(url).hostname == "127.0.0.1":
        import httpx

        token = token_file.read_text(encoding="ascii").strip()
        try:
            response = httpx.get(url.rstrip("/") + "/WeChatAgent/health",
                                 headers={"Authorization": "Bearer " + token},
                                 timeout=2, trust_env=False)
            version = response.json().get("version")
            state = "在表内" if version in SUPPORTED else "不在表内"
            print(f"在线 hook：{version}（{state}）")
        except (httpx.RequestError, ValueError):
            print("微信 hook 未在线。")
    print("以上为只读诊断：未连接 reader、未发送消息、未修改任何文件。")


async def chat(args):
    from .local_chat import (allow_local_route, chat as terminal_chat,
        existing_channels, local_message, local_runtime_settings,
        new_local_private_route)
    root, settings, state = read_settings(args.config)
    settings = local_runtime_settings(settings, args.mode)
    if getattr(args, 'ui', False) and args.config.resolve() != root / 'config/bridge.local.toml':
        raise ValueError('现有监视窗口读取config/bridge.local.toml；自定义配置请不带--ui运行。')
    local_route = new_local_private_route(settings["owner_id"])
    allow_local_route(settings, local_route)
    with locked(root):
        channels = existing_channels(state, settings)
        if args.scope:
            channels = [row for row in channels if row[0] == args.scope]
        if not channels:
            if args.scope:
                raise ValueError("没有找到指定的授权会话库。")
            channels = [(local_route.scope, local_route)]
        if len(channels) > 1:
            for number, (scope, route) in enumerate(channels, 1):
                print(f"{number}. {route.group_id} ({scope})")
            selected = int(await asyncio.to_thread(input, "编号 > ")) - 1
            if not 0 <= selected < len(channels):
                raise ValueError("会话库选择无效。")
            channels = [channels[selected]]
        route = channels[0][1]
        local_message(route, settings, "identity check")
        await terminal_chat(root, state, settings, engine_for(root, settings, args.mode), route,
                            ui=getattr(args, 'ui', False))


async def web(args):
    from .local_chat import local_runtime_settings
    from .local_web import run
    root, settings, state = read_settings(args.config)
    if not 1 <= args.port <= 65535 or args.port in {settings.get('port', 8766), 8767, 8768}:
        raise ValueError('请选择有效且不与管理/观察页面重叠的本地聊天端口。')
    if args.ui and args.config.resolve() != root / 'config/bridge.local.toml':
        raise ValueError('现有监视窗口读取config/bridge.local.toml；自定义配置请不带--ui运行。')
    runtime = local_runtime_settings(settings, args.mode)
    with locked(root):
        await run(root, state, runtime, engine_for(root, runtime, args.mode), port=args.port,
            ui=args.ui, initial_scope=args.scope, open_browser=not args.no_browser)


def replace_setting(text, key, value):
    result, count = re.subn(rf"(?m)^{re.escape(key)}\s*=.*$",
                           lambda _: key + " = " + json.dumps(value, ensure_ascii=False), text, count=1)
    if count != 1:
        raise ValueError(f"配置中缺少唯一{key}字段。")
    tomllib.loads(result)
    return result


async def bind(args):
    from .wechat_client import NativeClient, Settings
    root, _, _ = read_settings(args.config)
    with locked(root):
        file = wechat_file(args)
        bridge_settings = tomllib.loads(args.config.read_text(encoding="utf-8-sig"))["bridge"]
        client = NativeClient(Settings.load(file, owner_id=bridge_settings.get("owner_id", "")))
        try:
            await client.bind()
            bridge_text = args.config.read_text(encoding="utf-8-sig")
            settings_text = file.read_text(encoding="utf-8-sig")
            groups = tomllib.loads(bridge_text)["bridge"].get("allowed_groups", [])
            routes = []
            if client.settings.group_enabled:
                routes.append("xiaowennuan-wechat|" + client.group_id)
            if client.settings.private_enabled:
                routes.append("xiaowennuan-wechat|" + client.private_id)
            bridge_text = replace_setting(bridge_text, "allowed_groups", list(dict.fromkeys([*groups, *routes])))
            settings_text = replace_setting(settings_text, "group_id", client.group_id)
            settings_text = replace_setting(settings_text, "bot_id", client.bot_id)
            try:
                settings_text = replace_setting(settings_text, "private_id", client.private_id)
            except ValueError as error:
                if "private_id" not in str(error):
                    raise
                settings_text = settings_text.rstrip() + "\nprivate_id = " + json.dumps(client.private_id) + "\n"
                tomllib.loads(settings_text)
            args.config.write_text(bridge_text, encoding="utf-8")
            file.write_text(settings_text, encoding="utf-8")
        finally:
            await client.close()
    print("认证绑定完成：已保存真实群/机器人ID和bridge白名单。")


async def wechat(args):
    from aiohttp import web
    from .bridge import Bridge
    from .server import create_app
    from .wechat import WeChatRunner
    from .wechat_client import NativeClient, Settings
    root, settings, state = read_settings(args.config)
    from .onebot_client import OneBotSettings
    qq, qq_error = OneBotSettings(), None
    if getattr(args, 'command', '') == 'dual':
        try:
            qq = OneBotSettings.load(onebot_file(args))
        except (OSError, TypeError, ValueError) as error:
            qq_error = type(error).__name__
    if args.ui and args.config.resolve() != root / "config/bridge.local.toml":
        raise ValueError("现有监视窗口读取config/bridge.local.toml；自定义配置请不带--ui运行。")
    if settings.get("host", "127.0.0.1") != "127.0.0.1":
        raise ValueError("管理API仅支持127.0.0.1。")
    native_settings = Settings.load(wechat_file(args), owner_id=settings.get("owner_id", ""))
    with locked(root):
        client = NativeClient(native_settings)
        try:
            # Authenticate before constructing/starting a Bridge with persisted jobs.
            await client.bind()
            routes = []
            if native_settings.group_enabled:
                routes.append("xiaowennuan-wechat|" + client.group_id)
            if native_settings.private_enabled:
                routes.append("xiaowennuan-wechat|" + client.private_id)
            if any(route not in settings.get("allowed_groups", []) for route in routes):
                raise ValueError("认证会话不在bridge白名单中，请先运行bind。")
        except BaseException:
            await client.close()
            raise
        if qq.enabled:
            try:
                if not settings.get('owner_id') or not settings.get('shared_memory'):
                    raise ValueError('dual requires a bound owner and shared_memory=true')
                if not native_settings.private_enabled or not client.private_id:
                    raise ValueError('dual requires the existing WeChat owner private report route')
                targets = [f'{qq.platform_id}|{kind}:{number}' for kind, numbers in
                           (('group', qq.group_ids), ('private', qq.private_ids)) for number in numbers]
                if any(route not in settings.get('allowed_groups', []) for route in targets):
                    raise ValueError('QQ routes must be explicitly added to bridge.allowed_groups')
            except ValueError as error:
                qq_error, qq = type(error).__name__, OneBotSettings()
            else:
                settings['owner_bindings'] = [*settings.get('owner_bindings', []),
                    {'platform_id': qq.platform_id, 'bot_id': qq.bot_id, 'sender_id': qq.owner_id}]
                settings['owner_report_route'] = 'xiaowennuan-wechat|' + client.private_id
        settings['active_platforms'] = ['xiaowennuan-wechat', *([qq.platform_id] if qq.enabled else [])]
        bridge = Bridge(state, settings, engine_for(root, settings, args.mode), root / settings["persona_file"])
        if qq_error:
            bridge.channel_status = {'qq': {'state': 'degraded', 'reason': 'configuration_error'}}
            logging.getLogger(__name__).warning('QQ configuration rejected (%s); WeChat remains enabled', qq_error)
        runner = WeChatRunner(bridge, client, observe_only=args.observe_only)
        async with AsyncExitStack() as stack:
            # Close the transport before the app's existing Bridge cleanup hook.
            api = web.AppRunner(create_app(bridge, (root / settings["token_file"]).read_text(encoding="ascii").strip()))
            stack.push_async_callback(api.cleanup)
            await api.setup()
            stack.push_async_callback(runner.close)
            await web.TCPSite(api, "127.0.0.1", settings.get("port", 8766)).start()
            if args.ui:
                from .monitor import create_monitor_app
                from .context_viewer import create_app as context_app
                for app, port in ((create_monitor_app(root), 8767), (context_app(root), 8768)):
                    ui = web.AppRunner(app)
                    stack.push_async_callback(ui.cleanup)
                    await ui.setup()
                    await web.TCPSite(ui, "127.0.0.1", port).start()
                print("监视：http://127.0.0.1:8767/  上下文：http://127.0.0.1:8768/")
            channel_label = 'wechat + qq' if qq.enabled else 'wechat'
            print(f"{channel_label} · {args.mode} · Ctrl+C正常退出")
            if qq.enabled:
                from .onebot import OneBotRunner
                from .onebot_client import OneBotClient
                from .channel_runtime import run_channels
                await run_channels(bridge, {'wechat': runner,
                    'qq': OneBotRunner(bridge, OneBotClient(qq), observe_only=args.observe_only)})
            else:
                await runner.run()


async def dual(args):
    return await wechat(args)
