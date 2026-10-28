"""Read-only owner views over existing notes, interests and artifact registries."""
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import asyncio
from urllib.parse import quote

from aiohttp import web

from .artifacts import ArtifactStore


def _rows(path, table, sql, params=()):
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.3)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone():
            return []
        return [dict(row) for row in db.execute(sql, params)]


def personal_scopes(root, owner_id, allowed_routes):
    rows = _rows(root / 'deliveries.sqlite3', 'group_routes', 'SELECT scope,payload FROM group_routes')
    scopes = []
    for row in rows:
        route = json.loads(row['payload'])
        platform = route.get('platform_id', '')
        local = platform == 'xiaowennuan-local'
        if (route.get('conversation_kind') == 'private' and owner_id
                and route.get('group_id') == owner_id and route.get('sender_id') == owner_id
                and (local or f"{platform}|{route.get('group_id')}" in allowed_routes)):
            scopes.append({'scope': row['scope'], 'label': '本机私聊' if local else '微信私聊'})
    return sorted(scopes, key=lambda row: (row['label'] != '微信私聊', row['scope']))


def personal_dashboard(root, scopes, *, scope=None, offset=0, day=None):
    if not scopes:
        return {'available': False, 'scopes': [], 'notes': [], 'interests': [], 'artifacts': []}
    scope = scope or scopes[0]['scope']
    if scope not in {row['scope'] for row in scopes}:
        raise PermissionError('owner private scope required')
    if type(offset) is not int or offset < 0:
        raise ValueError('invalid offset')
    condition, params = 'scope=? AND archived=0', [scope]
    if day:
        start = datetime.combine(date.fromisoformat(day), time(), tzinfo=timezone(timedelta(hours=8)))
        condition += ' AND updated_at>=? AND updated_at<?'
        params.extend((start.timestamp(), (start + timedelta(days=1)).timestamp()))
    notes = _rows(root / 'personal-space.sqlite3', 'personal_note',
                  'SELECT note_id,kind,text,source_ref,revision,created_at,updated_at,shared_at '
                  f'FROM personal_note WHERE {condition} ORDER BY updated_at DESC,note_id LIMIT 21 OFFSET ?',
                  (*params, offset))
    interests = _rows(root / 'self-direction.sqlite3', 'direction_interest',
                      'SELECT interest_id,description,motivation,progress,next_step,status,source_ref,updated_at '
                      "FROM direction_interest WHERE scope=? ORDER BY CASE status WHEN 'active' THEN 0 "
                      "WHEN 'paused' THEN 1 ELSE 2 END,updated_at DESC LIMIT 20", (scope,))
    artifacts = _rows(root / 'artifacts.sqlite3', 'artifacts',
                      'SELECT id,path,media_type,revision FROM artifacts WHERE scope=? ORDER BY rowid DESC LIMIT 20', (scope,))
    for row in artifacts:
        row['name'] = Path(row.pop('path')).name
    return {'available': True, 'scope': scope, 'scopes': scopes, 'notes': notes[:20],
            'interests': interests, 'artifacts': artifacts, 'offset': offset,
            'next_offset': offset + 20 if len(notes) > 20 else None, 'day': day or '',
            'timezone': 'Asia/Shanghai', 'subjective': True}


def personal_artifact(root, project, scopes, scope, artifact_id):
    if scope not in {row['scope'] for row in scopes}:
        raise PermissionError('owner private scope required')
    rows = _rows(root / 'artifacts.sqlite3', 'artifacts',
                 'SELECT path,content_hash FROM artifacts WHERE scope=? AND id=?', (scope, artifact_id))
    if not rows:
        raise FileNotFoundError('artifact not in scope')
    path = Path(rows[0]['path'])
    path = (path if path.is_absolute() else project / path).resolve()
    if not path.is_file():
        raise FileNotFoundError('artifact file unavailable')
    if ArtifactStore._hash(path) != rows[0]['content_hash']:
        raise ValueError('artifact changed since registration')
    return path


def install_personal_routes(app, root, project, scopes_getter, cookie, session):
    async def page(request):
        response = web.Response(text=Path(__file__).with_name('personal_view.html').read_text(encoding='utf-8'),
                                content_type='text/html')
        response.set_cookie(cookie, session, httponly=True, samesite='Strict')
        return response

    async def asset(request):
        name = request.match_info['name']
        if name not in {'personal_view.js', 'personal_view.css'}:
            raise web.HTTPNotFound()
        return web.Response(text=Path(__file__).with_name(name).read_text(encoding='utf-8'),
                            content_type='text/javascript' if name.endswith('.js') else 'text/css')

    async def read(request):
        try:
            scopes = await asyncio.to_thread(scopes_getter)
            data = await asyncio.to_thread(personal_dashboard, root, scopes, scope=request.query.get('scope'),
                offset=int(request.query.get('offset', '0')), day=request.query.get('day'))
            return web.json_response(data)
        except PermissionError:
            raise web.HTTPForbidden() from None
        except ValueError:
            raise web.HTTPBadRequest() from None
        except sqlite3.OperationalError:
            raise web.HTTPServiceUnavailable(text='小本暂时不可读，请稍后刷新。') from None

    async def download(request):
        try:
            scopes = await asyncio.to_thread(scopes_getter)
            path = await asyncio.to_thread(personal_artifact, root, project, scopes,
                                          request.query.get('scope'), request.query.get('id'))
            return web.FileResponse(path, headers={'Content-Type': 'application/octet-stream',
                'Content-Disposition': "attachment; filename*=UTF-8''" + quote(path.name)})
        except PermissionError:
            raise web.HTTPForbidden() from None
        except FileNotFoundError:
            raise web.HTTPNotFound() from None
        except ValueError:
            raise web.HTTPConflict(text='文件已改变，请让小温暖重新登记当前版本。') from None

    app.router.add_get('/personal', page)
    app.router.add_get('/assets/personal/{name}', asset)
    app.router.add_get('/api/personal', read)
    app.router.add_get('/api/personal/artifact', download)
