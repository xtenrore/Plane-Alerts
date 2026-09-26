"""Bound route-history persistence behind a small fixed worker queue.

Route samples are useful telemetry, but they must never create one asyncio task
per visible aircraft or compete with live alert work. This layer keeps the
background route-history writer bounded, deduplicated and isolated from the
live alert path.
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from typing import Any

from app.config import settings
from app.intelligence import route_history as route_mod
from app.intelligence.route_intelligence_v46 import observe_v46
from app.intelligence.trajectory import haversine_km

logger = logging.getLogger(__name__)

_OBSERVE_TIMEOUT_S = 3.0
_OBSERVE_WORKERS = 6
_OBSERVE_QUEUE_LIMIT = 96
_MAX_QUEUE_AGE_S = 20.0
_QUEUE_FULL_LOG_INTERVAL_S = 30.0
_BASE_OBSERVE = observe_v46
_INSTALLED = False


async def _bounded_original_observe(self: Any, ac: Any, *, now: float | None = None) -> None:
    try:
        await asyncio.wait_for(
            _BASE_OBSERVE(self, ac, now=now),
            timeout=_OBSERVE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "flight_route_observe_timeout callsign=%s timeout_s=%.1f",
            getattr(ac, "callsign", ""),
            _OBSERVE_TIMEOUT_S,
        )


def _sample_due(self: Any, key: str, lat: float, lon: float, now: float) -> bool:
    previous = getattr(self, "_last_sample", {}).get(key)
    if not previous:
        return True
    last_t, last_lat, last_lon = previous
    interval = max(15, int(settings.route_sample_interval_seconds))
    moved = haversine_km(float(last_lat), float(last_lon), lat, lon)
    return not (now - float(last_t) < interval and moved < 1.5)


async def _observe_worker(self: Any, queue: asyncio.Queue) -> None:
    while True:
        key, ac, observed_at, queued_mono = await queue.get()
        try:
            if time.monotonic() - queued_mono <= _MAX_QUEUE_AGE_S:
                await _bounded_original_observe(self, ac, now=observed_at)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("flight_route_observe_worker_failed callsign=%s", key)
        finally:
            queued_keys = getattr(self, "_route_guard_v44_observe_keys", set())
            queued_keys.discard(key)
            queue.task_done()


def _ensure_queue(self: Any) -> tuple[asyncio.Queue, set[str]]:
    queue = getattr(self, "_route_guard_v44_observe_queue", None)
    workers = getattr(self, "_route_guard_v44_observe_workers", None)
    queued_keys = getattr(self, "_route_guard_v44_observe_keys", None)
    if queue is None:
        queue = asyncio.Queue(maxsize=_OBSERVE_QUEUE_LIMIT)
        self._route_guard_v44_observe_queue = queue
    if queued_keys is None:
        queued_keys = set()
        self._route_guard_v44_observe_keys = queued_keys
    if not workers or any(worker.done() for worker in workers):
        if workers:
            for worker in workers:
                if not worker.done():
                    worker.cancel()
        workers = [
            asyncio.create_task(
                _observe_worker(self, queue),
                name=f"route-observe-worker:{index}",
            )
            for index in range(_OBSERVE_WORKERS)
        ]
        self._route_guard_v44_observe_workers = workers
    return queue, queued_keys


async def observe_queued(
    self: route_mod.RouteHistoryService,
    ac: Any,
    *,
    now: float | None = None,
) -> None:
    """Queue a bounded route-history write without blocking the alert loop."""
    key = route_mod.normalize_flight_key(getattr(ac, "callsign", ""))
    latitude = getattr(ac, "latitude", None)
    longitude = getattr(ac, "longitude", None)
    if not key or latitude is None or longitude is None:
        return

    observed_at = time.time() if now is None else float(now)
    lat = float(latitude)
    lon = float(longitude)
    if not _sample_due(self, key, lat, lon, observed_at):
        return

    queue, queued_keys = _ensure_queue(self)
    if key in queued_keys:
        return

    snapshot = SimpleNamespace(
        callsign=key,
        latitude=lat,
        longitude=lon,
        altitude=getattr(ac, "altitude", None),
        heading=getattr(ac, "heading", None),
    )
    queued_keys.add(key)
    try:
        queue.put_nowait((key, snapshot, observed_at, time.monotonic()))
    except asyncio.QueueFull:
        queued_keys.discard(key)
        last_log = float(getattr(self, "_route_guard_v44_queue_full_log", 0.0) or 0.0)
        now_mono = time.monotonic()
        if now_mono - last_log >= _QUEUE_FULL_LOG_INTERVAL_S:
            self._route_guard_v44_queue_full_log = now_mono
            logger.warning(
                "route_history_queue_full pending=%d limit=%d; dropping non-critical sample",
                queue.qsize(),
                _OBSERVE_QUEUE_LIMIT,
            )


def install_route_observe_guard_v44() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    route_mod.RouteHistoryService.observe = observe_queued
    _INSTALLED = True
    logger.info(
        "Route history v4.4 queue enabled: workers=%d queue_limit=%d write_timeout=%.1fs",
        _OBSERVE_WORKERS,
        _OBSERVE_QUEUE_LIMIT,
        _OBSERVE_TIMEOUT_S,
    )
