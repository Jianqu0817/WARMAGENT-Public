"""Small helpers for synchronous archive work inside async runtimes."""
from __future__ import annotations

import asyncio


async def run_sync(function, /, *args, **kwargs):
    """Run one synchronous operation off-loop and let it finish before release.

    Archive reads are immutable snapshots and ArchiveStore serializes writes.
    Waiting for an in-flight thread on cancellation keeps a later maintenance
    retry from overlapping the operation whose stage was just interrupted.
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise
