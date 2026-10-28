"""Independent loopback operations console; reuse existing entrypoints and stores."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time
import tomllib
import uuid
import webbrowser

from aiohttp import ClientSession, ClientTimeout, web

from .console_catalog import ACTIONS, EDIT_FILES, LINKS
from .console_settings import read_form, save_form

CONSOLE = web.AppKey('control_console', object)
SERVICE_LOGS = {'chat': 'warmagent.log', 'reader': 'reader.stdout.log',
    'reader_error': 'reader.stderr.log', 'monitor': 'monitor.stderr.log', 'context': 'context_viewer.stderr.log'}


class Console:
    def __init__(self, root, *, popen=subprocess.Popen):
        self.root = Path(root).resolve()
        self.popen = popen
        self.runs = {}
        self.lock = threading.Lock()
        self.actions = {row['id']: row for row in ACTIONS}

    def settings(self):
        path = self.root / 'config/bridge.local.toml'
        return tomllib.loads(path.read_text(encoding='utf-8-sig')).get('bridge', {}) if path.is_file() else {}

    @staticmethod
    def listening(port):
        with socket.socket() as connection:
            connection.settimeout(.15)
            return connection.connect_ex(('127.0.0.1', port)) == 0

    def state(self):
        with self.lock:
            runs = self.run_states()
        try:
            config = self.settings()
            config_error = None
        except (ValueError, OSError):
            config, config_error = {}, '主配置格式错误，请从配置文件入口修正。'
        port = config.get('port', 8766)
        ports = sorted({8767, 8768, 8769, 30001, 5031, 6099, int(port)})
        listeners = {str(number): self.listening(number) for number in ports}
        return {'actions': [{key: value for key, value in row.items() if key != 'argv'} for row in ACTIONS],
            'links': [{**link, 'available': listeners[str(link['port'])],
                'url': f"http://127.0.0.1:{link['port']}{link['path']}"} for link in LINKS],
            'ports': listeners, 'admin_port': port, 'runs': runs,
            'project': self.root.name, 'config_error': config_error,
            'config_files': [{'id': key, 'label': value[0], 'exists': self.editor_path(key).is_file()}
                for key, value in EDIT_FILES.items()],
            'documents': self.documents(),
            'note': '端口亮灯只表示正在监听；任务启动不等于登录或业务就绪。关闭控制台不停止聊天。'}

    def run_states(self):
        result = []
        for row in reversed(list(self.runs.values())):
            code = row['process'].poll()
            result.append({'id': row['id'], 'action': row['action'], 'label': row['label'],
                'started': row['started'], 'pid': row['process'].pid, 'exit_code': code,
                'state': 'running' if code is None else 'completed' if code == 0 else 'failed',
                'interactive': row['interactive'], 'log': bool(row['log'])})
        return result[:40]

    def start(self, action_id):
        if action_id not in self.actions:
            raise ValueError('未知操作。')
        spec = self.actions[action_id]
        with self.lock:
            active = [row for row in self.runs.values() if row['process'].poll() is None]
            family = spec['family']
            if any(row['action'] == action_id or family and family == row['family'] for row in active):
                raise FileExistsError('同类操作仍在运行，请先查看任务记录。')
            if family in {'chat', 'setup', 'maintenance'}:
                if any(row['family'] in {'setup', 'maintenance'} for row in active):
                    raise FileExistsError('安装或维护仍在运行，请完成后再启动。')
                if family != 'chat' and any(row['family'] == 'chat' for row in active):
                    raise FileExistsError('请先正常停止聊天，再安装或维护。')
            if family in {'chat', 'setup', 'maintenance'} and self.listening(int(self.settings().get('port', 8766))):
                raise FileExistsError('已有聊天服务或管理端口正在使用；先正常停止，避免重复启动或并行修改。')
            if os.name != 'nt':
                raise RuntimeError('启动原CMD入口需要Windows；此系统可查看文档和配置。')
            python = self.root / '.venv/Scripts/python.exe'
            argv = [part.replace('{python}', str(python)) for part in spec['argv']] if spec['argv'] else [
                os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/c', spec['cmd']]
            if spec['argv'] and not python.is_file():
                raise ValueError('请先运行项目安装入口。')
            interactive = spec['interactive']
            identity = uuid.uuid4().hex
            directory = self.root / 'var/logs/control-console'
            directory.mkdir(parents=True, exist_ok=True)
            log = None if interactive else directory / f'{identity}.log'
            info = subprocess.STARTUPINFO()
            if not interactive:
                info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                info.wShowWindow = 0
            environment = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONUNBUFFERED': '1'}
            # Each runtime keeps its own real Windows console for the existing
            # authenticated Ctrl+C stop script. Interactive terminals are visible.
            kwargs = {'cwd': str(self.root), 'env': environment, 'shell': False,
                'creationflags': subprocess.CREATE_NEW_CONSOLE, 'startupinfo': info}
            stream = log.open('ab') if log else None
            try:
                if stream:
                    kwargs.update(stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT)
                process = self.popen(argv, **kwargs)
            finally:
                if stream:
                    stream.close()
            row = {'id': identity, 'action': action_id, 'label': spec['label'], 'family': family,
                'interactive': interactive, 'log': log, 'started': time.time(), 'process': process}
            self.runs[identity] = row
            for old in list(self.runs):
                if len(self.runs) <= 200:
                    break
                if self.runs[old]['process'].poll() is not None:
                    del self.runs[old]
            return {'id': identity, 'message': '已打开交互窗口，请在该窗口继续。' if interactive else '已提交，结果请看任务记录和服务状态。'}

    def read_log(self, run_id):
        if run_id not in self.runs:
            raise ValueError('本控制台没有这条任务记录。')
        path = self.runs[run_id]['log']
        if path is None:
            return {'text': '这是交互操作，请查看独立窗口。密钥和扫码内容不抄送到网页。'}
        return self.tail(path)

    @staticmethod
    def tail(path):
        if not path.is_file():
            return {'text': '这份日志尚未生成。'}
        with path.open('rb') as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - 64000))
            content = stream.read().decode('utf-8', errors='replace')
        return {'text': content or '操作尚无输出。', 'truncated': size > 64000}

    def service_log(self, key):
        if key not in SERVICE_LOGS:
            raise ValueError('未知服务日志。')
        return self.tail(self.root / 'var/logs' / SERVICE_LOGS[key])

    def editor_path(self, key):
        if key not in EDIT_FILES:
            raise ValueError('未知配置文件。')
        return self.root / EDIT_FILES[key][1]

    def edit_file(self, key):
        path = self.editor_path(key)
        if not path.is_file():
            if key != 'persona':
                raise ValueError('配置文件尚不存在，请先补齐缺失配置。')
            source = self.root / self.settings().get('persona_file', 'config/persona/persona.md')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source.read_bytes())
        if os.name != 'nt':
            raise RuntimeError('打开本机编辑器需要Windows。')
        editor = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/notepad.exe'
        self.popen([str(editor), str(path)], cwd=str(self.root), shell=False)
        return {'ok': True, 'message': '已打开本机编辑器。角色卡修改不会自动改写已有记忆中的人格；私人卡需在主配置中指定。'}

    def documents(self):
        paths = [self.root / 'README.md', *sorted((self.root / 'docs').glob('*.md'))]
        return [{'id': path.relative_to(self.root).as_posix(), 'title': path.stem}
            for path in paths if path.is_file()]

    def document(self, key):
        if key not in {item['id'] for item in self.documents()}:
            raise ValueError('未知文档。')
        path = self.root / key
        if path.stat().st_size > 1_000_000:
            raise ValueError('文档较大，请在本机编辑器中打开。')
        return {'title': path.stem, 'text': path.read_text(encoding='utf-8-sig')}


async def create_app(root, *, controller=None):
    console = controller or Console(root)
    csrf, session = secrets.token_urlsafe(32), secrets.token_urlsafe(32)

    @web.middleware
    async def authenticate(request, handler):
        host = request.headers.get('Host', '')
        if request.remote not in {'127.0.0.1', '::1'} or host.split(':')[0] not in {'127.0.0.1', 'localhost'}:
            raise web.HTTPForbidden()
        origin = request.headers.get('Origin')
        if (origin and origin != 'http://' + host) or request.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise web.HTTPForbidden()
        if request.path.startswith('/api/'):
            if not hmac.compare_digest(request.cookies.get('warm_console', ''), session):
                raise web.HTTPForbidden()
            if request.method == 'POST' and (origin != 'http://' + host or
                    not hmac.compare_digest(request.headers.get('X-Console-CSRF', ''), csrf)):
                raise web.HTTPForbidden()
        try:
            response = await handler(request)
        except FileExistsError as error:
            response = web.json_response({'error': str(error)}, status=409)
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as error:
            response = web.json_response({'error': str(error)}, status=400)
        except (OSError, RuntimeError) as error:
            response = web.json_response({'error': str(error)}, status=503)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})
        return response

    async def index(request):
        html = Path(__file__).with_name('control_console.html').read_text(encoding='utf-8')
        response = web.Response(text=html.replace('__CSRF__', csrf), content_type='text/html')
        response.set_cookie('warm_console', session, httponly=True, samesite='Strict')
        return response

    async def asset(request):
        name = request.match_info['name']
        if name not in {'control_console.js', 'control_console.css'}:
            raise web.HTTPNotFound()
        return web.Response(text=Path(__file__).with_name(name).read_text(encoding='utf-8'),
            content_type='application/javascript' if name.endswith('.js') else 'text/css')

    async def state(request):
        return web.json_response(await asyncio.to_thread(console.state))

    async def start(request):
        body = await request.json()
        return web.json_response(await asyncio.to_thread(console.start, body['id']))

    async def log(request):
        return web.json_response(await asyncio.to_thread(console.read_log, request.match_info['id']))

    async def service_log(request):
        return web.json_response(await asyncio.to_thread(console.service_log, request.match_info['id']))

    async def settings(request):
        if request.method == 'GET':
            return web.json_response(await asyncio.to_thread(read_form, console.root))
        body = await request.json()
        return web.json_response(await asyncio.to_thread(save_form, console.root, body))

    async def edit(request):
        return web.json_response(await asyncio.to_thread(console.edit_file, (await request.json())['id']))

    async def document(request):
        return web.json_response(await asyncio.to_thread(console.document, request.query['id']))

    async def health(request):
        return web.json_response({'console': 'warmagent', 'project_id': hashlib.sha256(str(console.root).encode()).hexdigest()})

    app = web.Application(middlewares=[authenticate], client_max_size=65536)
    app[CONSOLE] = console
    app.add_routes([web.get('/', index), web.get('/health', health), web.get('/assets/{name}', asset),
        web.get('/api/state', state), web.post('/api/actions', start), web.get('/api/runs/{id}', log),
        web.get('/api/logs/{id}', service_log),
        web.get('/api/settings', settings), web.post('/api/settings', settings),
        web.post('/api/edit', edit), web.get('/api/document', document)])
    return app


async def serve(args):
    root = args.project.resolve()
    if not 1 <= args.port <= 65535:
        raise ValueError('控制台端口无效。')
    url = f'http://127.0.0.1:{args.port}/'
    if Console.listening(args.port):
        async with ClientSession(timeout=ClientTimeout(total=2), trust_env=False) as client:
            async with client.get(url + 'health') as response:
                payload = await response.json()
        expected = hashlib.sha256(str(root).encode()).hexdigest()
        if payload.get('console') != 'warmagent' or payload.get('project_id') != expected:
            raise RuntimeError('控制台端口已被其他项目占用，请用 --port 选择另一个端口。')
        if not args.no_browser:
            await asyncio.to_thread(webbrowser.open, url)
        print('本项目控制台已运行：' + url)
        return
    from .server import service_lock
    # Independent lock: never acquires or releases the actual chat service lock.
    with service_lock(root / 'var/control-console.lock'):
        app = await create_app(root)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, '127.0.0.1', args.port).start()
            print('WARMAGENT 操作台 · ' + url + '\n关闭本窗口仅退出控制台，不停止聊天或设备。', flush=True)
            if not args.no_browser:
                await asyncio.to_thread(webbrowser.open, url)
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--port', type=int, default=8770)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
