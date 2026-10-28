"""Supervise channel failures independently; Bridge ownership stays in launch."""
import asyncio
import logging

logger = logging.getLogger(__name__)


def channel_snapshot(bridge):
    result = {}
    for name, status in getattr(bridge, 'channel_status', {}).items():
        result[name] = dict(status)
        runner = getattr(bridge, 'channel_runners', {}).get(name)
        if runner and hasattr(runner.client, 'connected'):
            result[name].update(connected=runner.client.connected,
                overflow_count=runner.client.overflow_count)
    return result


async def run_channels(bridge, runners, *, retry_seconds=3):
    bridge.channel_status = {}
    bridge.channel_runners = runners

    async def supervise(name, runner):
        try:
            while True:
                try:
                    runner.stopped = False
                    bridge.channel_status[name] = {'state': 'running'}
                    await runner.run()
                    raise RuntimeError('channel runner exited')
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    bridge.channel_status[name] = {'state': 'degraded', 'reason': type(error).__name__}
                    logger.warning('%s channel degraded: %s', name, type(error).__name__)
                try:
                    await runner.reset()
                except Exception as error:
                    logger.warning('%s channel reset failed: %s', name, type(error).__name__)
                await asyncio.sleep(retry_seconds)
        finally:
            try:
                await runner.close()
            except Exception as error:
                logger.warning('%s channel cleanup failed: %s', name, type(error).__name__)

    tasks = [asyncio.create_task(supervise(name, runner)) for name, runner in runners.items()]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
