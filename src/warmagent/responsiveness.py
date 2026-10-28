"""Event-loop delay observation without inspecting private chat contents."""
import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class LoopDelay:
    def __init__(self):
        self.last_seconds = 0.
        self.max_seconds = 0.
        self.observed_at = None

    async def run(self):
        while True:
            expected = time.monotonic() + 1
            await asyncio.sleep(1)
            self.last_seconds = max(0., time.monotonic() - expected)
            self.max_seconds = max(self.max_seconds, self.last_seconds)
            self.observed_at = time.time()
            if self.last_seconds >= 1:
                logger.warning('Main event loop delayed seconds=%.3f', self.last_seconds)

    def view(self):
        return {'last_seconds': round(self.last_seconds, 3),
            'max_seconds': round(self.max_seconds, 3), 'observed_at': self.observed_at}
