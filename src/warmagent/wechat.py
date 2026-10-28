"""Direct WeChat reader/native transport for the durable Bridge outbox."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import time

import httpx

from myatri.contracts import digest
from .bridge import Incoming
from .server import pending_deliveries
from .runtime_inbox import InboxFull
from .wechat_client import NotReady, incoming

logger = logging.getLogger(__name__)


def delivery_error_code(error, stage):
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if stage == "ack":
        return "ack_error"
    if isinstance(error, NotReady):
        return {"SEND_RECEIPT_UNKNOWN": "receipt_timeout",
                "NATIVE_SEND_REJECTED": "native_rejected",
                "ONEBOT_SEND_REJECTED": "native_rejected"}.get(str(error), "binding_unavailable")
    return "transport_error" if isinstance(error, httpx.HTTPError) else "send_error"


class WeChatRunner:
    def __init__(self, bridge, client, *, platform_id="xiaowennuan-wechat", observe_only=False):
        self.bridge = bridge
        self.client = client
        self.platform_id = platform_id
        self.observe_only = observe_only
        self.started_at = int(time.time())
        self.scope = None
        self.scope_targets = {}
        self.stopped = False
        self.tasks = set()
        self.poll_task = None
        self.inflight = set()
        self.delivery_lock = asyncio.Lock()
        self.readiness_task = None
        self.readiness_error = None
        self.readiness_interval = 10

    def message(self, payload):
        session_id = payload.get("sessionId")
        settings = self.client.settings
        group_enabled = getattr(settings, "group_enabled", True)
        private_enabled = getattr(settings, "private_enabled", False)
        if group_enabled and session_id == self.client.group_id:
            conversation_kind, target_id = "group", self.client.group_id
        elif private_enabled and session_id == getattr(self.client, "private_id", ""):
            conversation_kind, target_id = "private", self.client.private_id
        else:
            return None
        item = incoming(payload, self.client, self.started_at,
                        conversation_kind=conversation_kind, target_id=target_id)
        if item is None:
            return None
        route = f"{self.platform_id}|{target_id}"
        if route not in self.bridge.config.get("allowed_groups", []):
            return None
        text = item["text"]
        mentioned = item["mentioned"]
        prefix = self.bridge.config.get("trigger_prefix", "/warm ")
        if conversation_kind == "group" and prefix and text.startswith(prefix):
            mentioned = True
            text = text[len(prefix):]
        if not text.strip():
            return None
        # Preserve the old plugin's textual At conversion and default transport;
        # both participate in the existing message fingerprint.
        return Incoming(
            platform_id=self.platform_id, bot_id=self.client.bot_id,
            group_id=target_id, sender_id=item["sender_id"],
            sender_name=item["sender_name"], message_id=item["message_id"],
            sent_at=datetime.fromtimestamp(item["timestamp"], timezone.utc).isoformat(),
            text=text, mentioned=(conversation_kind == "private" or mentioned),
            mentioned_ids=[self.client.bot_id] if conversation_kind == "group" and item["mentioned"] else [],
            message_type=item.get("message_type", "text"),
            quoted_message=item.get("quoted_message"),
            conversation_kind=conversation_kind,
        )

    async def handle(self, payload):
        accepted = time.monotonic()
        message = self.message(payload)
        if message is None or self.observe_only or message.key in self.inflight:
            return
        source_age = max(0., time.time() - datetime.fromisoformat(message.sent_at).timestamp())
        self.scope = message.scope
        self.inflight.add(message.key)
        try:
            while True:
                try:
                    result = await self.bridge.receive(message)
                    logger.info('WeChat intake key=%s source_age_at_intake_seconds=%.3f receive_seconds=%.3f status=%s',
                        message.key, source_age,
                        time.monotonic() - accepted, result.get('status'))
                    break
                except InboxFull:
                    if self.stopped:
                        return
                    await asyncio.sleep(1)
            if result.get("status") == "prepared":
                await self.deliver(result, target_id=message.group_id)
        finally:
            self.inflight.discard(message.key)

    async def deliver(self, delivery, *, target_id=None):
        if self.readiness_error:
            return
        replies = delivery["reply_parts"]
        if (not isinstance(replies, list) or not 1 <= len(replies) <= 3
                or any(not isinstance(text, str) or not text.strip() for text in replies)):
            raise ValueError("invalid reply parts")
        async with self.delivery_lock:
            if self.readiness_error:
                return
            key = delivery["id"]
            if not (await self.bridge.claim(key))["claimed"]:
                return
            started = None
            stage = "send"
            try:
                for index, text in enumerate(replies):
                    if self.readiness_error:
                        raise NotReady(self.readiness_error)
                    started = time.monotonic()
                    stage = "send"
                    if target_id and target_id != self.client.group_id:
                        receipt = await self.client.send(text, target_id)
                    else:
                        receipt = await self.client.send(text)
                    stage = "ack"
                    await self.bridge.acknowledge(key, "sent", index,
                        elapsed_seconds=min(3600., time.monotonic() - started),
                        **({'transport_message_id': receipt} if isinstance(receipt, str) and receipt else {}))
            except (Exception, asyncio.CancelledError) as error:
                reason = delivery_error_code(error, stage)
                logger.warning("%s delivery stopped: %s", type(self).__name__, reason)
                try:
                    await self.bridge.acknowledge(key, "unknown", reason_code=reason,
                        elapsed_seconds=min(3600., time.monotonic() - started) if started is not None else None)
                except Exception:
                    logger.warning("%s unknown acknowledgement failed", type(self).__name__)
                if isinstance(error, asyncio.CancelledError):
                    raise

    async def poll(self):
        if self.observe_only:
            return
        kind = None if self.bridge.config.get("proactive", {}).get("enabled", False) else "non_proactive"
        targets = dict(self.scope_targets)
        if not targets and self.scope is not None:
            targets[self.scope] = self.client.group_id
        for scope, target_id in targets.items():
            for delivery in pending_deliveries(self.bridge.path, scope, limit=3, kind=kind):
                await self.deliver(delivery, target_id=target_id)

    async def _poll_loop(self):
        while not self.stopped:
            await asyncio.sleep(2)
            try:
                await self.poll()
            except Exception as error:
                logger.warning("%s pending poll failed: %s", type(self).__name__, delivery_error_code(error, "poll"))

    def _finished(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("%s incoming message failed: %s", type(self).__name__, type(task.exception()).__name__)

    async def _wait_until_ready(self):
        while not self.stopped:
            try:
                await self.client.check()
                self.readiness_error = None
                return True
            except NotReady as error:
                self.readiness_error = str(error)
                if str(error) not in {"READER_HTTP_UNAVAILABLE", "NATIVE_HTTP_UNAVAILABLE",
                                      "READER_NOT_READY", "HOOK_INITIALIZATION_PENDING"}:
                    raise
                logger.warning("WeChat readiness interrupted: %s; retrying", error)
                await asyncio.sleep(3)
        return False

    async def _readiness_loop(self):
        while not self.stopped:
            await asyncio.sleep(self.readiness_interval)
            try:
                await self._wait_until_ready()
            except NotReady as error:
                logger.error('WeChat identity/readiness check stopped: %s', error)
                return

    async def run(self):
        if not self.client.settings.bot_id:
            raise NotReady("RUN_CHECK_WECHAT_CONFIGURE_FIRST")
        await self.client.bind()
        targets = []
        if self.client.settings.group_enabled:
            if not self.client.group_id:
                raise NotReady("GROUP_MAPPING_AMBIGUOUS_OR_MISSING")
            targets.append(self.client.group_id)
        if self.client.settings.private_enabled:
            if not self.client.private_id:
                raise NotReady("OWNER_MAPPING_MISSING")
            targets.append(self.client.private_id)
        allowed = self.bridge.config.get("allowed_groups", [])
        for target_id in targets:
            route = f"{self.platform_id}|{target_id}"
            if route not in allowed:
                raise NotReady("ROUTE_NOT_ALLOWED")
            scope = digest([self.platform_id, self.client.bot_id, target_id])[:32]
            self.scope_targets[scope] = target_id
            kind = "private" if target_id == self.client.private_id else "group"
            sender_id = (self.client.settings.owner_id if kind == "private"
                         else getattr(self.client.settings, "owner_id", "") or target_id)
            self.bridge.register_route(Incoming(
                platform_id=self.platform_id, bot_id=self.client.bot_id, group_id=target_id,
                sender_id=sender_id, sender_name="渠道路由", message_id="route-bootstrap",
                sent_at=datetime.now(timezone.utc).isoformat(), text="渠道已绑定",
                mentioned=False, conversation_kind=kind))
            if self.scope is None:
                self.scope = scope
        self.bridge.start()
        self.poll_task = asyncio.create_task(self._poll_loop())
        if not await self._wait_until_ready():
            return
        self.readiness_task = asyncio.create_task(self._readiness_loop())
        while not self.stopped:
            try:
                if not await self._wait_until_ready():
                    return
                async for payload in self.client.events():
                    # Keep this already received event while a transient health
                    # failure recovers; reconnecting here could lose the event.
                    if self.readiness_error and not await self._wait_until_ready():
                        return
                    task = asyncio.create_task(self.handle(payload))
                    self.tasks.add(task)
                    task.add_done_callback(self._finished)
                if not self.stopped:
                    raise httpx.ReadError("reader stream closed")
            except httpx.TransportError:
                if not self.stopped:
                    logger.warning("WeChat reader stream interrupted")
                    await asyncio.sleep(3)

    async def reset(self):
        self.stopped = True
        tasks = [*self.tasks, *([self.poll_task] if self.poll_task else []),
                 *([self.readiness_task] if self.readiness_task else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.poll_task = None
        self.readiness_task = None
        self.tasks.clear()

    async def close(self):
        await self.reset()
        await self.client.close()
