"""Loopback browser transport for the existing local Bridge runtime."""
from __future__ import annotations

import asyncio
from contextlib import closing
from copy import deepcopy
import hmac
import json
from pathlib import Path
import secrets
import socket
import sqlite3
import uuid
import webbrowser

from aiohttp import web

from myatri.providers import ProviderError
from .bridge import Bridge, Conflict, Incoming
from .local_chat import allow_local_route, existing_channels, local_message, new_local_private_route


class LocalWebChat:
    def __init__(self, bridge, *, initial_scope=None):
        self.bridge, self.settings = bridge, bridge.config
        local = new_local_private_route(self.settings['owner_id'])
        allow_local_route(self.settings, local)
        self.routes = {scope: route for scope, route in existing_channels(bridge.root, self.settings)
            if route.platform_id in {'xiaowennuan-wechat', 'xiaowennuan-local'}}
        # Incompatible old private routes are omitted, not rewritten as owner.
        self.routes = {scope: route for scope, route in self.routes.items()
            if route.conversation_kind != 'private' or route.group_id == self.settings['owner_id']}
        self.routes[local.scope] = local
        if initial_scope and initial_scope not in self.routes:
            raise ValueError('没有找到指定的授权会话。')
        preferred = next((scope for scope, route in self.routes.items()
            if route.platform_id == 'xiaowennuan-wechat' and route.conversation_kind == 'private'), local.scope)
        self.selected_scope = initial_scope or preferred
        bridge.register_route(local)
        bridge.active_local_scopes.clear()
        bridge.active_local_scopes.add(self.selected_scope)
        self._receipts = asyncio.Lock()
        self._claimed = set()

    def route(self, scope):
        try:
            return self.routes[scope]
        except (KeyError, TypeError):
            raise web.HTTPForbidden(text='会话未授权。') from None

    def select(self, scope):
        self.route(scope)
        self.selected_scope = scope
        self.bridge.active_local_scopes.clear()
        self.bridge.active_local_scopes.add(scope)

    async def submit(self, body):
        scope = body['scope']
        route = self.route(scope)
        if scope != self.selected_scope:
            raise web.HTTPConflict(text='当前会话已切换，请刷新后发送。')
        text = body['text']
        if not isinstance(text, str) or not text.strip() or len(text) > 20000:
            raise ValueError('消息需要1—20000个字符。')
        if not isinstance(body['client_id'], str):
            raise ValueError('invalid client ID')
        client_id = str(uuid.UUID(body['client_id']))
        message = local_message(route, self.settings, text)
        message = message.model_copy(update={'message_id': 'web_' + client_id})
        old = self.bridge.input_inbox.get(message.key)
        if old:
            previous = Incoming.model_validate(old['payload'])
            if previous.text != text or previous.scope != scope or previous.transport != 'local_cli':
                raise Conflict('message ID reused with different content')
            # Keep the original timestamp/fingerprint when the browser retries.
            message = previous
        return await self.bridge.receive(message)

    def snapshot(self, scope):
        self.route(scope)
        messages = {}
        with closing(sqlite3.connect(self.bridge.path)) as db:
            rows = db.execute('SELECT id,status,envelope,reply_parts,reply,COALESCE(received_at,updated),'
                'updated,sent_parts,trigger_kind FROM deliveries WHERE scope=? ORDER BY rowid DESC LIMIT 80', (scope,)).fetchall()
        for key, status, encoded, parts, reply, received, updated, sent_parts, trigger in rows:
            envelope = json.loads(encoded) if encoded else {}
            client_id = envelope.get('message_id', '').removeprefix('web_')
            if (envelope.get('text') and trigger not in {'autonomy', 'batch_report', 'proactive', 'computer_report'}
                    and not key.startswith('job_report_')):
                messages[key + ':user'] = {'id': key + ':user', 'role': 'user', 'text': envelope['text'],
                    'status': status, 'created_at': received, 'client_id': client_id}
            local = envelope.get('transport') == 'local_cli'
            for part_index, text in enumerate(json.loads(parts) if parts else ([reply] if reply else [])):
                messages[f'{key}:assistant:{part_index}'] = {
                    'id': f'{key}:assistant:{part_index}', 'role': 'assistant', 'text': text,
                    'status': status, 'created_at': updated + part_index * .000001,
                    'delivery_id': key, 'part_index': part_index,
                    'ack_required': local and status in {'prepared', 'sending'} and part_index >= sent_parts}
        with closing(sqlite3.connect(self.bridge.input_inbox.path)) as db:
            pending = db.execute('SELECT key,payload,state,created,last_error FROM runtime_inputs '
                'WHERE scope=? ORDER BY rowid DESC LIMIT 80', (scope,)).fetchall()
        for key, encoded, state, created, error in pending:
            payload = json.loads(encoded)
            identifier = key + ':user'
            if identifier not in messages and payload.get('transport') == 'local_cli':
                messages[identifier] = {'id': identifier, 'role': 'user', 'text': payload.get('text', ''),
                    'created_at': created, 'status': 'failed' if error else state,
                    'client_id': payload.get('message_id', '').removeprefix('web_')}
        activity = self.bridge.activities.overview()
        app = self.bridge.apps.get(scope)
        index = app.index.incremental_status() if app and hasattr(app.index, 'incremental_status') else None
        conversations = []
        for key, route in self.routes.items():
            label = ('本机私聊' if route.platform_id == 'xiaowennuan-local' else
                     '微信私聊 · 延续原对话' if route.conversation_kind == 'private' else '群聊上下文 · ' + route.group_id)
            conversations.append({'scope': key, 'label': label, 'kind': route.conversation_kind})
        return {'mode': self.settings['mode'], 'selected_scope': scope, 'conversations': conversations,
            'messages': sorted(messages.values(), key=lambda row: (row['created_at'], row['id']))[-160:],
            'activity': activity, 'scheduler': self.bridge.activities.schedule_view(), 'index': index}

    async def acknowledge(self, body):
        scope, key, part = body['scope'], body['delivery_id'], body['part_index']
        self.route(scope)
        if type(part) is not int:
            raise ValueError('invalid part index')
        async with self._receipts:
            with closing(sqlite3.connect(self.bridge.path)) as db:
                row = db.execute('SELECT scope,envelope,status FROM deliveries WHERE id=?', (key,)).fetchone()
            if not row or row[0] != scope or not row[1] or json.loads(row[1]).get('transport') != 'local_cli':
                raise web.HTTPForbidden(text='只能确认本地窗口显示的回复。')
            if row[2] == 'sent':
                return {'status': 'sent'}
            if row[2] == 'prepared':
                if not (await self.bridge.claim(key))['claimed']:
                    raise Conflict('delivery already claimed')
                self._claimed.add(key)
            elif row[2] != 'sending' or key not in self._claimed:
                raise Conflict('delivery is not confirmed for this browser runtime')
            result = await self.bridge.acknowledge(key, 'sent', part)
            if result['status'] == 'sent':
                self._claimed.discard(key)
            return result


LOCAL_CHAT = web.AppKey('local_chat', LocalWebChat)


def create_app(bridge, *, initial_scope=None):
    chat = LocalWebChat(bridge, initial_scope=initial_scope)
    session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)

    @web.middleware
    async def authenticate(request, handler):
        host = request.host
        if host.rsplit(':', 1)[0] not in {'127.0.0.1', 'localhost'}:
            raise web.HTTPForbidden()
        origin = request.headers.get('Origin')
        if (origin and origin != 'http://' + host) or request.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise web.HTTPForbidden()
        if request.path.startswith('/api/'):
            if not hmac.compare_digest(request.cookies.get('local_chat_session', ''), session):
                raise web.HTTPForbidden()
            if request.method == 'POST' and (origin != 'http://' + host or
                    not hmac.compare_digest(request.headers.get('X-Local-CSRF', ''), csrf)):
                raise web.HTTPForbidden()
        try:
            response = await handler(request)
        except Conflict:
            response = web.json_response({'error': '状态已变化，请刷新后确认。'}, status=409)
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            response = web.json_response({'error': '请求内容无效。'}, status=400)
        except ProviderError:
            response = web.json_response({'error': '模型请求失败，原消息已保留。'}, status=503)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})
        return response

    async def index(request):
        html = Path(__file__).with_name('local_web.html').read_text(encoding='utf-8')
        response = web.Response(text=html.replace('__CSRF__', csrf), content_type='text/html')
        response.set_cookie('local_chat_session', session, httponly=True, samesite='Strict')
        return response

    async def asset(request):
        name = request.match_info['name']
        if name not in {'local_web.js', 'local_web.css'}:
            raise web.HTTPNotFound()
        return web.Response(text=Path(__file__).with_name(name).read_text(encoding='utf-8'),
            content_type='text/javascript' if name.endswith('.js') else 'text/css')

    async def state(request):
        return web.json_response(await asyncio.to_thread(chat.snapshot, chat.selected_scope))

    async def select(request):
        chat.select((await request.json())['scope'])
        return web.json_response({'ok': True, 'selected_scope': chat.selected_scope})

    async def send(request):
        return web.json_response(await chat.submit(await request.json()))

    async def receipt(request):
        return web.json_response(await chat.acknowledge(await request.json()))

    async def activity(request):
        action = (await request.json())['action']
        if action not in {'pause', 'resume'}:
            raise ValueError('unsupported activity control')
        bridge.control_activity(action)
        return web.json_response({'ok': True})

    app = web.Application(middlewares=[authenticate], client_max_size=131072)
    app[LOCAL_CHAT] = chat
    app.add_routes([web.get('/', index), web.get('/api/state', state), web.post('/api/select', select),
        web.post('/api/messages', send), web.post('/api/receipts', receipt),
        web.post('/api/activity', activity)])
    from .personal_view import install_personal_routes
    def personal_scopes():
        return [{'scope': scope, 'label': '本机私聊' if route.platform_id == 'xiaowennuan-local' else '微信私聊'}
                for scope, route in chat.routes.items() if bridge._private_owner(route)]
    install_personal_routes(app, bridge.root, Path(bridge.config.get('project_root', bridge.root.parent)),
                            personal_scopes, 'local_chat_session', session)
    app.router.add_get('/{name}', asset)
    return app


async def run(root, state_root, settings, engine, *, port=8769, ui=False, initial_scope=None, open_browser=True):
    from contextlib import AsyncExitStack
    from .server import create_app as admin_app
    runtime = deepcopy(settings)
    runtime.setdefault('proactive', {})['enabled'] = False
    runtime['active_platforms'] = ['xiaowennuan-wechat', 'xiaowennuan-local']
    bridge = Bridge(state_root, runtime, engine, root / runtime['persona_file'])
    admin_owns_cleanup = False
    try:
        local_app = create_app(bridge, initial_scope=initial_scope)
        async with AsyncExitStack() as stack:
            local_runner = web.AppRunner(local_app, shutdown_timeout=2)
            stack.push_async_callback(local_runner.cleanup)
            await local_runner.setup()
            await web.TCPSite(local_runner, '127.0.0.1', port).start()
            api = web.AppRunner(admin_app(bridge, (root / runtime['token_file']).read_text(encoding='ascii').strip()),
                shutdown_timeout=2)
            # Admin cleanup owns Bridge close; run it before draining chat HTTP.
            stack.push_async_callback(api.cleanup)
            await api.setup()
            admin_owns_cleanup = True
            await web.TCPSite(api, '127.0.0.1', runtime.get('port', 8766)).start()
            if ui:
                from .monitor import create_monitor_app
                from .context_viewer import create_app as context_app
                for factory, page_port in ((create_monitor_app, 8767), (context_app, 8768)):
                    with socket.socket() as connection:
                        connection.settimeout(.2)
                        if connection.connect_ex(('127.0.0.1', page_port)) == 0:
                            continue
                    page = web.AppRunner(factory(root))
                    stack.push_async_callback(page.cleanup)
                    await page.setup()
                    await web.TCPSite(page, '127.0.0.1', page_port).start()
            url = f'http://127.0.0.1:{port}/'
            print(f'小温暖本地聊天 · {runtime["mode"]} · {url}\n同库记忆与心跳保留；Ctrl+C正常退出。', flush=True)
            if open_browser:
                await asyncio.to_thread(webbrowser.open, url)
            await asyncio.Event().wait()
    finally:
        # setup failures may happen before admin cleanup is installed.
        if not admin_owns_cleanup:
            await bridge.close()
