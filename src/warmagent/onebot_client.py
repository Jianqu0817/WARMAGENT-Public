"""OneBot 11 reverse Universal WebSocket. No login or model operations."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hmac
import json
import logging
from pathlib import Path
import re
import tomllib
import uuid

from aiohttp import web, WSMsgType

from .wechat_client import NotReady

logger = logging.getLogger(__name__)


@dataclass
class OneBotSettings:
    enabled: bool = False
    platform_id: str = 'xiaowennuan-qq'
    bot_id: str = ''
    owner_id: str = ''
    group_ids: list[str] = field(default_factory=list)
    private_ids: list[str] = field(default_factory=list)
    ws_host: str = '127.0.0.1'
    ws_port: int = 30021
    ws_path: str = '/ws'
    access_token: str = field(default='', repr=False)
    receipt_timeout: float = 15
    queue_size: int = 256

    @classmethod
    def load(cls, path):
        if not Path(path).exists():
            return cls()
        settings = cls(**tomllib.loads(Path(path).read_text(encoding='utf-8-sig'))['onebot'])
        if type(settings.enabled) is not bool:
            raise ValueError('onebot.enabled must be boolean')
        if not settings.enabled:
            return settings
        if settings.ws_host != '127.0.0.1' or not settings.ws_path.startswith('/'):
            raise ValueError('OneBot requires a loopback host and absolute ws_path')
        if not 1 <= settings.ws_port <= 65535 or settings.receipt_timeout <= 0 or settings.queue_size < 1:
            raise ValueError('invalid OneBot port, timeout or queue size')
        if settings.enabled:
            if (not isinstance(settings.group_ids, list) or not isinstance(settings.private_ids, list)
                    or not isinstance(settings.access_token, str)):
                raise ValueError('OneBot target IDs must be lists and access_token must be a string')
            ids = [settings.bot_id, settings.owner_id, *settings.group_ids, *settings.private_ids]
            if any(not isinstance(value, str) or not value.isdecimal() or int(value) <= 0 for value in ids):
                raise ValueError('OneBot account and target IDs must be positive decimal strings')
            if settings.bot_id == settings.owner_id or not settings.access_token:
                raise ValueError('OneBot needs separate bot/owner accounts and an access token')
            if any(value != settings.owner_id for value in settings.private_ids):
                raise ValueError('QQ private chats are owner-only')
            if not settings.platform_id or any(c in settings.platform_id for c in '|\r\n'):
                raise ValueError('invalid OneBot platform_id')
        return settings


def _decode(text, *, parameter=False):
    if parameter:
        text = text.replace('&#44;', ',')
    return text.replace('&#91;', '[').replace('&#93;', ']').replace('&amp;', '&')


def parse_event(event, bot_id):
    """Keep transport identities, mentions and reply ID; first version is text-only."""
    if event.get('post_type') != 'message' or str(event.get('self_id', '')) != bot_id:
        return None
    sender = str(event.get('user_id', ''))
    kind = event.get('message_type')
    if kind not in {'group', 'private'} or sender == bot_id or not sender:
        return None
    message = event.get('message', [])
    if isinstance(message, str):
        segments, pos = [], 0
        for match in re.finditer(r'\[CQ:([a-zA-Z_]+)((?:,[^\]]*)?)\]', message):
            segments.append({'type': 'text', 'data': {'text': _decode(message[pos:match.start()])}})
            params = dict(part.split('=', 1) for part in match[2].lstrip(',').split(',') if '=' in part)
            segments.append({'type': match[1], 'data': {k: _decode(v, parameter=True) for k, v in params.items()}})
            pos = match.end()
        segments.append({'type': 'text', 'data': {'text': _decode(message[pos:])}})
    elif isinstance(message, list):
        segments = message
    else:
        return None
    texts, mentions, reply = [], [], None
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get('data'), dict):
            continue
        data = segment['data']
        if segment.get('type') == 'text':
            texts.append(str(data.get('text', '')))
        elif segment.get('type') == 'at':
            mentions.append(str(data.get('qq', '')))
        elif segment.get('type') == 'reply':
            reply = str(data.get('id', '')) or None
    text = ''.join(texts).strip()
    target = str(event.get('group_id', '')) if kind == 'group' else sender
    if not text or not target or not event.get('message_id'):
        return None
    return {'kind': kind, 'target': target, 'sender_id': sender,
            'sender_name': str((event.get('sender') or {}).get('card') or (event.get('sender') or {}).get('nickname') or ''),
            'message_id': str(event['message_id']), 'timestamp': event.get('time', 0), 'text': text,
            'mentioned': bot_id in mentions, 'mentioned_ids': list(dict.fromkeys(mentions))[:30], 'reply_to_id': reply}


class OneBotClient:
    group_id = ''  # The shared outbox delivery method always passes a QQ target token.

    def __init__(self, settings):
        self.settings = settings
        self.bot_id = settings.bot_id
        self.queue = asyncio.Queue(maxsize=settings.queue_size)
        self.pending = {}
        self.ws = None
        self.server = None
        self.site = None
        self.overflow_count = 0
        self.closed = False

    @property
    def connected(self):
        return self.ws is not None and not self.ws.closed

    async def start(self):
        if not self.settings.enabled:
            raise NotReady('ONEBOT_DISABLED')
        if self.server is not None:
            return
        app = web.Application()
        app.router.add_get(self.settings.ws_path, self._connect)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(runner, self.settings.ws_host, self.settings.ws_port)
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        self.server, self.site = runner, site

    async def _connect(self, request):
        token = request.headers.get('Authorization', '').removeprefix('Bearer ')
        token = token or request.query.get('access_token', '')
        if not self.settings.access_token or not hmac.compare_digest(token, self.settings.access_token):
            raise web.HTTPUnauthorized()
        if (request.headers.get('X-Self-ID') != self.bot_id
                or request.headers.get('X-Client-Role', '').lower() != 'universal'):
            raise web.HTTPForbidden()
        if self.ws is not None and not self.ws.closed:
            raise web.HTTPConflict(text='OneBot already connected')
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1_000_000)
        await ws.prepare(request)
        self.ws = ws
        try:
            async for frame in ws:
                if frame.type != WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(frame.data)
                except ValueError:
                    continue
                if not isinstance(payload, dict):
                    continue
                echo = payload.get('echo')
                pending = self.pending.get(echo) if isinstance(echo, str) else None
                if pending and pending[0] is ws and 'post_type' not in payload:
                    if not pending[1].done():
                        pending[1].set_result(payload)
                elif payload.get('post_type') == 'message':
                    try:
                        self.queue.put_nowait(payload)
                    except asyncio.QueueFull:
                        self.overflow_count += 1
                        logger.error('OneBot event queue overflow; QQ connection closed, event may be lost')
                        await ws.close(code=1013, message=b'event backlog full')
                        break
        finally:
            if self.ws is ws:
                self.ws = None
            for socket, future in list(self.pending.values()):
                if socket is ws and not future.done():
                    future.set_exception(NotReady('SEND_RECEIPT_UNKNOWN'))
        return ws

    async def events(self):
        while not self.closed:
            yield await self.queue.get()

    async def send(self, text, target_id):
        ws = self.ws
        if ws is None or ws.closed:
            raise NotReady('ONEBOT_WS_NOT_CONNECTED')
        kind, number = target_id.split(':', 1)
        if kind not in {'group', 'private'} or not number.isdecimal():
            raise ValueError('invalid QQ target')
        targets = self.settings.group_ids if kind == 'group' else self.settings.private_ids
        if number not in targets:
            raise NotReady('ONEBOT_SEND_REJECTED')
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = (ws, future)
        try:
            await ws.send_json({'action': 'send_group_msg' if kind == 'group' else 'send_private_msg',
                'params': {'group_id' if kind == 'group' else 'user_id': int(number),
                           'message': [{'type': 'text', 'data': {'text': text}}]}, 'echo': echo})
            response = await asyncio.wait_for(future, self.settings.receipt_timeout)
            if response.get('status') != 'ok' or response.get('retcode') != 0:
                raise NotReady('ONEBOT_SEND_REJECTED')
            if not isinstance(response.get('data'), dict) or response['data'].get('message_id') is None:
                raise NotReady('SEND_RECEIPT_UNKNOWN')
            return str(response['data']['message_id'])
        except (TimeoutError, ConnectionError, OSError) as error:
            raise NotReady('SEND_RECEIPT_UNKNOWN') from error
        finally:
            self.pending.pop(echo, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def close(self):
        self.closed = True
        if self.ws is not None:
            await self.ws.close()
        if self.server is not None:
            await self.server.cleanup()
            self.server = self.site = None
