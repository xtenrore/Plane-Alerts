"""Plane Alerts v4.4 non-blocking route-history read guard.

Historical flight paths improve false-alert suppression, but MongoDB latency must
never sit in front of ADS-B -> trajectory -> CPA -> qualification -> alert.

Cold/expired history reads are therefore refreshed in bounded background work.
The live evaluator immediately receives the last cached value (or an empty safe
fallback on a true cold miss). Refreshes are single-flight per callsign, capped
in count and concurrency, timed out, and dropped if they wait too long.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.intelligence import route_guard_v2 as v2
from app.intelligence import route_history as route_mod

logger = logging.getLogger(__name__)

_HISTORY_TTL_S = 120.0
_NEGATIVE_RETRY_S = 15.0
_READ_TIMEOUT_S = 1.5
_MAX_PENDING = 16
_READ_CONCURRENCY = 2
_MAX_CACHE_ENTRIES = 256
_MAX_QUEUE_AGE_S = 5.0
_INSTALLED = False


def _history_cache(service: Any) -> dict[str, tuple[float, list[list[route_mod.RoutePoint]]]]:
    cache = getattr(service, "_route_guard_v2_history_cache", None)
    if cache is None:
        cache = {}
        service._route_guard_v2_history_cache = cache
    return cache


def _negative_until(service: Any) -> dict[str, float]:
    values = getattr(service, "_route_guard_v44_history_negative_until", None)
    if values is None:
        values = {}
        service._route_guard_v44_history_negative_until = values
    return values


def _pending(service: Any) -> dict[str, asyncio.Task]:
    tasks = getattr(service, "_route_guard_v44_history_tasks", None)
    if tasks is None:
        tasks = {}
        service._route_guard_v44_history_tasks = tasks
    return tasks


def _semaphore(service: Any) -> asyncio.Semaphore:
    semaphore = getattr(service, "_route_guard_v44_history_semaphore", None)
    if semaphore is None:
        semaphore = asyncio.Semaphore(_READ_CONCURRENCY)
        service._route_guard_v44_history_semaphore = semaphore
    return semaphore


def _trim_cache(cache: dict[str, tuple[float, list[list[route_mod.RoutePoint]]]]) -> None:
    if len(cache) <= _MAX_CACHE_ENTRIES:
        return
    for key, _ in sorted(cache.items(), key=lambda item: item[1][0])[: max(1, _MAX_CACHE_ENTRIES // 4)]:
        cache.pop(key, None)


async def _refresh_history(service: Any, key: str, queued_mono: float) -> None:
    semaphore = _semaphore(service)
    try:
        async with semaphore:
            if time.monotonic() - queued_mono > _MAX_QUEUE_AGE_S:
                logger.info("flight_route_history_refresh_dropped callsign=%s reason=stale_queue", key)
                return
            try:
                paths = await asyncio.wait_for(service._historical_paths(key), timeout=_READ_TIMEOUT_S)
            except asyncio.TimeoutError:
                _negative_until(service)[key] = time.monotonic() + _NEGATIVE_RETRY_S
                logger.warning(
                    "flight_route_history_read_timeout callsign=%s timeout_s=%.1f",
                    key,
                    _READ_TIMEOUT_S,
                )
                return
            except Exception:
                _negative_until(service)[key] = time.monotonic() + _NEGATIVE_RETRY_S
                logger.exception("flight_route_history_read_failed callsign=%s", key)
                return

            cache = _history_cache(service)
            cache[key] = (time.monotonic(), paths)
            _negative_until(service).pop(key, None)
            _trim_cache(cache)
    finally:
        _pending(service).pop(key, None)


def _schedule_refresh(service: Any, key: str) -> None:
    tasks = _pending(service)
    existing = tasks.get(key)
    if existing is not None and not existing.done():
        return
    if len(tasks) >= _MAX_PENDING:
        logger.warning(
            "flight_route_history_read_saturated pending=%d limit=%d callsign=%s",
            len(tasks),
            _MAX_PENDING,
            key,
        )
        return

    queued_mono = time.monotonic()
    task = asyncio.create_task(
        _refresh_history(service, key, queued_mono),
        name=f"route-history-read:{key}",
    )
    tasks[key] = task

    def _finished(done: asyncio.Task, *, callsign: str = key) -> None:
        tasks.pop(callsign, None)
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("flight_route_history_refresh_failed callsign=%s", callsign)

    task.add_done_callback(_finished)


async def historical_paths_nonblocking(
    service: Any,
    key: str,
) -> list[list[route_mod.RoutePoint]]:
    """Return immediately; refresh a cold/expired Mongo value in background."""
    now = time.monotonic()
    cache = _history_cache(service)
    cached = cache.get(key)
    if cached is not None and now - cached[0] < _HISTORY_TTL_S:
        return cached[1]

    # Stale data is safer than blocking: history can only veto a live CPA and
    # never creates one. A true cold miss falls back to no historical veto.
    fallback = cached[1] if cached is not None else []
    if _negative_until(service).get(key, 0.0) <= now:
        _schedule_refresh(service, key)
    return fallback


def install_route_history_read_guard_v44() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    v2._historical_paths_cached = historical_paths_nonblocking
    _INSTALLED = True
    logger.info(
        "Route-history v4.4 read guard enabled: cold reads backgrounded pending=%d concurrency=%d timeout=%.1fs ttl=%.0fs",
        _MAX_PENDING,
        _READ_CONCURRENCY,
        _READ_TIMEOUT_S,
        _HISTORY_TTL_S,
    )
