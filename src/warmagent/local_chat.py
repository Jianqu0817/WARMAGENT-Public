"""Mutually exclusive terminal entry using the existing AstrBot conversation library."""
import argparse
import asyncio
from contextlib import closing, ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import socket
import tomllib
import uuid

from myatri.app import load_config
from myatri.providers import ProviderError
from .bridge import Bridge, Incoming
from .server import service_lock

LOCAL_PLATFORM_ID = 'xiaowennuan-local'
LOCAL_BOT_ID = 'xiaowennuan-local-bot'
OFFLINE_OWNER_ID = 'offline-owner'


def existing_channels(state_root, settings):
    path = Path(state_root) / 'deliveries.sqlite3'
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        routes = db.execute('SELECT scope,payload FROM group_routes ORDER BY scope').fetchall()
    channels = []
    for scope, payload in routes:
        route = Incoming.model_validate(json.loads(payload))
        if route.route in settings.get('allowed_groups', []):
            channels.append((scope, route))
    return channels


def local_message(route, settings, text):
    owner_id = settings.get('owner_id')
    if not owner_id or owner_id in {route.bot_id, LOCAL_BOT_ID}:
        raise ValueError('本机私聊需要配置独立、固定的用户owner_id。')
    if route.conversation_kind == 'private' and route.group_id != owner_id:
        raise ValueError('本机私聊路由的owner_id与当前配置不一致。')
    return Incoming(platform_id=route.platform_id, bot_id=route.bot_id, group_id=route.group_id,
        sender_id=settings['owner_id'], sender_name=settings.get('owner_name') or '用户',
        message_id='cli_' + uuid.uuid4().hex, sent_at=datetime.now(timezone.utc).isoformat(),
        text=text, mentioned=True, transport='local_cli',
        conversation_kind=route.conversation_kind)


def local_runtime_settings(settings, mode):
    """Return an in-memory CLI config with an explicit owner identity."""
    runtime = deepcopy(settings)
    runtime['mode'] = mode
    owner_id = runtime.get('owner_id')
    if not owner_id:
        if mode != 'offline':
            raise ValueError('live本机私聊必须配置实际、固定的owner_id。')
        owner_id = OFFLINE_OWNER_ID
    if owner_id == LOCAL_BOT_ID:
        raise ValueError('owner_id不能与本机bot_id相同。')
    runtime['owner_id'] = owner_id
    return runtime


def new_local_private_route(owner_id):
    return Incoming(platform_id=LOCAL_PLATFORM_ID, bot_id=LOCAL_BOT_ID,
        group_id=owner_id, sender_id=owner_id, sender_name='用户',
        message_id='local_private_route', sent_at=datetime.now(timezone.utc).isoformat(),
        text='本机私聊路由', mentioned=True, transport='local_cli',
        conversation_kind='private')


def allow_local_route(settings, route):
    routes = settings.setdefault('allowed_groups', [])
    if route.route not in routes:
        routes.append(route.route)
    return settings


async def reply(bridge, message, emit=print):
    result = await bridge.receive(message)
    if result['status'] == 'waiting_for_computer':
        emit('系统 > Harness执行中，结果返回后由小温暖回复。')
    while result['status'] == 'waiting_for_computer':
        await asyncio.sleep(.25)
        with closing(sqlite3.connect(bridge.path)) as db:
            result = bridge._delivery_result(db, message.key)
            report = db.execute('SELECT state FROM computer_reports WHERE delivery_id=?', (message.key,)).fetchone()
        if report and report[0] == 'failed':
            raise RuntimeError('任务执行记录已保存，但小温暖生成汇报失败；可用/pc状态查询。')
    if result['status'] == 'sent':
        emit('系统 > 本条回复已显示，不重复输出。')
        return
    if result['status'] != 'prepared':
        raise RuntimeError('本条回复尚未确认，请检查本机任务和投递状态。')
    if not (await bridge.claim(message.key))['claimed']:
        raise RuntimeError('本条回复已被领取，不重复输出。')
    try:
        for part in result['reply_parts']:
            emit('小温暖 > ' + part)
    except BaseException:
        await bridge.acknowledge(message.key, 'unknown')
        raise
    await bridge.acknowledge(message.key, 'sent')


async def display_background_reports(bridge, scope, emit=print):
    with closing(sqlite3.connect(bridge.path)) as db:
        rows = db.execute("SELECT id,reply_parts FROM deliveries WHERE scope=? AND status='prepared' "
            "AND json_extract(envelope,'$.transport')='local_cli' "
            "AND (id LIKE 'job_report_%' OR trigger_kind IN ('autonomy','batch_report')) ORDER BY rowid LIMIT 3", (scope,)).fetchall()
    for key, encoded in rows:
        if not (await bridge.claim(key))['claimed']:
            continue
        try:
            for index, part in enumerate(json.loads(encoded)):
                emit('小温暖 > ' + part)
                await bridge.acknowledge(key, 'sent', index)
        except BaseException:
            await bridge.acknowledge(key, 'unknown')
            raise


async def chat(root, state_root, settings, engine, route, *, ui=False):
    # Local mode has no WeChat sender; do not prepare unsolicited group speech.
    settings = deepcopy(settings)
    settings.setdefault('proactive', {})['enabled'] = False
    bridge = Bridge(state_root, settings, engine, root / settings['persona_file'])
    # Local-only routes are persisted so the next invocation can resume them.
    if route.transport == 'local_cli':
        bridge.routes[route.scope] = route.model_dump()
        with closing(sqlite3.connect(bridge.path)) as db, db:
            db.execute('INSERT OR REPLACE INTO group_routes(scope,payload) VALUES (?,?)',
                       (route.scope, json.dumps(route.model_dump(), ensure_ascii=False)))
    previous = None
    bridge.active_local_scopes.add(route.scope)
    async def poll_reports():
        while True:
            await display_background_reports(bridge, route.scope)
            await asyncio.sleep(1)
    reports, api = None, None
    pages = []
    try:
        if ui:
            from aiohttp import web
            from .server import create_app
            from .monitor import create_monitor_app
            from .context_viewer import create_app as context_app
            api = web.AppRunner(create_app(bridge, (root / settings['token_file']).read_text(encoding='ascii').strip()))
            await api.setup()
            await web.TCPSite(api, '127.0.0.1', settings.get('port', 8766)).start()
            for factory, port in ((create_monitor_app, 8767), (context_app, 8768)):
                with socket.socket() as connection:
                    connection.settimeout(.2)
                    if connection.connect_ex(('127.0.0.1', port)) == 0:
                        continue
                runner = web.AppRunner(factory(root))
                pages.append(runner)
                await runner.setup()
                await web.TCPSite(runner, '127.0.0.1', port).start()
            print('监视：http://127.0.0.1:8767/  上下文：http://127.0.0.1:8768/')
        else:
            bridge.start()
        reports = asyncio.create_task(poll_reports())
        print(f"小温暖本机私聊 · {settings['mode']}模式\n记忆根目录：{state_root}\n"
              '心跳与自主活动沿用原配置。命令：/exit 退出；/retry 重试上条未完成消息。')
        while True:
            try:
                text = (await asyncio.to_thread(input, '\n你 > ')).strip()
            except EOFError:
                break
            if text in {'/exit', '/quit'}:
                break
            if not text:
                continue
            if text == '/retry':
                if previous is None:
                    print('系统 > 没有可重试的消息。')
                    continue
                message = previous
            else:
                message = previous = local_message(route, settings, text)
            try:
                await reply(bridge, message)
            except (ProviderError, RuntimeError, ValueError) as error:
                print(f'系统 > {error}\n原消息保留；/retry复用原消息编号，不重新提交已存在的任务。')
    finally:
        if reports:
            reports.cancel()
            await asyncio.gather(reports, return_exceptions=True)
        for page in reversed(pages):
            await page.cleanup()
        if api:
            await api.cleanup()
        else:
            await bridge.close()


def main():
    parser = argparse.ArgumentParser(description='AstrBot同库命令行私聊；不连接微信，不另建记忆或上下文库。')
    parser.add_argument('--config', default='config/bridge.local.toml')
    parser.add_argument('--scope', help='有多个群库时，指定要延续的32位scope')
    parser.add_argument('--offline', action='store_true', help='仅使用离线测试回复')
    parser.add_argument('--ui', action='store_true', help='同时提供管理API和观察页面')
    args = parser.parse_args()
    file = Path(args.config).resolve()
    root = file.parent.parent
    settings = tomllib.loads(file.read_text(encoding='utf-8'))['bridge']
    state_root = (root / settings['state_root']).resolve()
    if not state_root.is_relative_to(root) or state_root == root:
        raise SystemExit('记忆库必须位于本项目目录内。')
    with ExitStack() as stack:
        try:
            stack.enter_context(service_lock(root / 'var/service.lock'))
        except OSError:
            raise SystemExit('群聊服务或另一个私聊窗口正在使用同一份库。请先正常退出，再切换模式。') from None
        mode = 'offline' if args.offline else settings.get('mode', 'offline')
        runtime = local_runtime_settings(settings, mode)
        local_route = new_local_private_route(runtime['owner_id'])
        allow_local_route(runtime, local_route)
        channels = existing_channels(state_root, runtime)
        if args.scope:
            channels = [row for row in channels if row[0] == args.scope]
        if not channels:
            if args.scope:
                raise SystemExit('没有找到指定的授权会话库。')
            channels = [(local_route.scope, local_route)]
        if len(channels) > 1:
            print('选择要接着聊的现有会话库：')
            for number, (scope, route) in enumerate(channels, 1):
                print(f'{number}. {route.group_id} ({scope})')
            try:
                selected = int(input('编号 > ')) - 1
                if not 0 <= selected < len(channels):
                    raise ValueError()
            except (ValueError, EOFError):
                raise SystemExit('会话库选择无效。') from None
            channels = [channels[selected]]
        route = channels[0][1]
        # Validate identity before loading the engine or making a model request.
        local_message(route, runtime, 'identity check')
        engine = load_config(root / runtime['engine_config'])
        engine['chat']['allow_paid'] = runtime['mode'] == 'live'
        try:
            asyncio.run(chat(root, state_root, runtime, engine, route, ui=args.ui))
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
