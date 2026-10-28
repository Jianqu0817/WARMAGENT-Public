"""QQ transport mapping; reuse the existing durable outbox delivery lifecycle."""
import asyncio
from datetime import datetime, timezone

from myatri.contracts import digest

from .bridge import Incoming
from .onebot_client import parse_event
from .wechat import WeChatRunner


class OneBotRunner(WeChatRunner):
    def __init__(self, bridge, client, *, observe_only=False):
        super().__init__(bridge, client, platform_id=client.settings.platform_id, observe_only=observe_only)

    def message(self, payload):
        item = parse_event(payload, self.client.bot_id)
        if item is None:
            return None
        settings = self.client.settings
        targets = settings.group_ids if item['kind'] == 'group' else settings.private_ids
        if item['target'] not in targets:
            return None
        if item['kind'] == 'private' and item['sender_id'] != settings.owner_id:
            return None
        target = f"{item['kind']}:{item['target']}"
        if f'{self.platform_id}|{target}' not in self.bridge.config.get('allowed_groups', []):
            return None
        text, mentioned = item['text'], item['mentioned']
        prefix = self.bridge.config.get('trigger_prefix', '/warm ')
        if item['kind'] == 'group' and prefix and text.startswith(prefix):
            text, mentioned = text[len(prefix):], True
        if not text.strip():
            return None
        return Incoming(platform_id=self.platform_id, bot_id=self.client.bot_id, group_id=target,
            sender_id=item['sender_id'], sender_name=item['sender_name'], message_id=item['message_id'],
            sent_at=datetime.fromtimestamp(item['timestamp'], timezone.utc).isoformat(), text=text,
            mentioned=mentioned or item['kind'] == 'private', mentioned_ids=item['mentioned_ids'],
            reply_to_id=item['reply_to_id'], conversation_kind=item['kind'], transport='onebot')

    async def reset(self):
        await super().reset()
        await self.client.close()

    async def deliver(self, delivery, *, target_id=None):
        if not self.client.connected:
            return  # Definitely not attempted: leave prepared for a later connection.
        return await super().deliver(delivery, target_id=target_id)

    async def run(self):
        settings = self.client.settings
        if not settings.enabled:
            return
        self.stopped = self.client.closed = False
        for kind, targets in (('group', settings.group_ids), ('private', settings.private_ids)):
            for number in targets:
                target = f'{kind}:{number}'
                route = f'{self.platform_id}|{target}'
                if route not in self.bridge.config.get('allowed_groups', []):
                    raise ValueError('QQ route not allowed')
                message = Incoming(platform_id=self.platform_id, bot_id=self.client.bot_id,
                    group_id=target, sender_id=settings.owner_id, sender_name='渠道路由',
                    message_id='route-bootstrap', sent_at=datetime.now(timezone.utc).isoformat(),
                    text='渠道已绑定', mentioned=False, transport='onebot', conversation_kind=kind)
                self.bridge.register_route(message)
                scope = digest([self.platform_id, self.client.bot_id, target])[:32]
                self.scope_targets[scope] = target
        await self.client.start()
        self.bridge.start()
        self.poll_task = asyncio.create_task(self._poll_loop())
        async for payload in self.client.events():
            if len(self.tasks) >= 16:
                await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
            task = asyncio.create_task(self.handle(payload))
            self.tasks.add(task)
            task.add_done_callback(self._finished)
