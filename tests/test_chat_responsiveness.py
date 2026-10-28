"""Offline checks: slow local work and health probes must not block intake."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from warmagent.wechat import WeChatRunner
from test_wechat_unread_batch import make_bridge, message


@pytest.mark.asyncio
async def test_slow_context_assembly_keeps_private_intake_running(tmp_path, monkeypatch):
    bridge, _ = make_bridge(tmp_path, [{'content': '统一回复。'}])
    monkeypatch.setattr(bridge, 'start', lambda: None)
    first = message('第一句', message_id='assembly-first')
    await bridge.receive(first)
    app = bridge._application(first, start_workers=False)
    original = app.conversation.context._assemble
    started, release = threading.Event(), threading.Event()

    def slow_assemble(*args, **kwargs):
        started.set()
        assert release.wait(3), 'context blocked the intake event loop'
        return original(*args, **kwargs)

    monkeypatch.setattr(app.conversation.context, '_assemble', slow_assemble)
    worker = None
    try:
        await asyncio.sleep(.02)
        worker = asyncio.create_task(bridge._process_next_input())
        assert await asyncio.to_thread(started.wait, 3)
        second = message('补充一句', message_id='assembly-second')
        result = await asyncio.wait_for(bridge.receive(second), 1)
        assert result['reason'] == 'unread_batch'
        assert bridge.input_inbox.get(second.key)['state'] == 'pending'
        release.set()
        assert (await asyncio.wait_for(worker, 8))['status'] == 'prepared'
    finally:
        release.set()
        if worker and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await bridge.close()


@pytest.mark.asyncio
async def test_periodic_health_probe_does_not_block_event_stream():
    probing, release = asyncio.Event(), asyncio.Event()
    seen = []

    class Client:
        bot_id = 'bot'
        group_id = 'room'
        private_id = 'owner'
        settings = SimpleNamespace(bot_id='bot', group_enabled=True, private_enabled=False, owner_id='owner')
        checks = 0

        async def bind(self):
            pass

        async def check(self):
            self.checks += 1
            if self.checks >= 3:
                probing.set()
                await release.wait()

        async def events(self):
            await probing.wait()
            for index in range(5):
                yield {'index': index}
                await asyncio.sleep(0)
            runner.stopped = True

        async def close(self):
            pass

    bridge = SimpleNamespace(config={'allowed_groups': ['xiaowennuan-wechat|room']},
        register_route=lambda _: None, start=lambda: None)
    runner = WeChatRunner(bridge, Client())
    runner.readiness_interval = .001

    async def handle(payload):
        seen.append(payload['index'])

    runner.handle = handle
    try:
        await asyncio.wait_for(runner.run(), 1)
        await asyncio.gather(*runner.tasks)
        assert seen == list(range(5))
        assert runner.client.checks == 3
    finally:
        release.set()
        await runner.close()


@pytest.mark.asyncio
async def test_background_schedule_runs_off_the_transport_loop(tmp_path, monkeypatch):
    bridge, _ = make_bridge(tmp_path)
    monkeypatch.setattr(bridge, 'start', lambda: None)
    entered, release = threading.Event(), threading.Event()

    def schedule():
        entered.set()
        assert release.wait(3), 'maintenance schedule blocked the event loop'

    async def consume():
        await asyncio.Event().wait()

    app = SimpleNamespace(maintenance=SimpleNamespace(turns=3, schedule=schedule),
        recover_pending=lambda: None, foreground_pending=lambda: False,
        worker=SimpleNamespace(stopping=False, poll_seconds=.01, run=consume))
    task = asyncio.create_task(bridge._run_background(app))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        result = await asyncio.wait_for(bridge.receive(message('调度期间补话', message_id='during-schedule')), 1)
        assert result['reason'] == 'unread_batch'
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bridge.close()
