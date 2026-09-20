"""Small, bounded single-flight cache for optional monitoring enrichment.

Callers never wait for I/O. Missing or expired data is explicitly unavailable
until a refresh completes. Admission is bounded as well as concurrency.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import logging
import time
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


class OptionalCache:
    def __init__(self, *, max_entries=256, max_pending=12, concurrency=3, timeout=12.0):
        self.max_entries = max_entries
        self.max_pending = max_pending
        self.timeout = timeout
        self.entries: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self.pending: dict[Any, asyncio.Task] = {}
        self.semaphore = asyncio.Semaphore(concurrency)
        self.dropped = 0
        self.failures = 0

    def get(self, key: Any, factory: Callable[[], Awaitable[Any]], *, default=None, ttl=120.0):
        now = time.monotonic()
        entry = self.entries.get(key)
        if entry and entry[0] > now:
            self.entries.move_to_end(key)
            return entry[1]
        self.entries.pop(key, None)
        if key not in self.pending:
            if len(self.pending) >= self.max_pending:
                self.dropped += 1
                return default
            self.pending[key] = asyncio.create_task(self._refresh(key, factory, ttl), name="optional-enrichment")
        return default

    async def _refresh(self, key, factory, ttl):
        try:
            async with self.semaphore:
                result = await asyncio.wait_for(factory(), self.timeout)
            self.entries[key] = (time.monotonic() + ttl, result)
            self.entries.move_to_end(key)
            while len(self.entries) > self.max_entries:
                self.entries.popitem(last=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failures += 1
            # Negative caching avoids retrying a broken dependency every cycle.
            self.entries[key] = (time.monotonic() + 15, None)
            while len(self.entries) > self.max_entries:
                self.entries.popitem(last=False)
            logger.exception("optional_enrichment_failed kind=%s", key[0] if isinstance(key, tuple) else "unknown")
        finally:
            self.pending.pop(key, None)

    async def close(self):
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.pending.clear()


enrichment = OptionalCache()
