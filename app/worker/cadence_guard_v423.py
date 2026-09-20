"""Plane Alerts v4.2.3 alert-cadence latency guard.

The live five-second cadence must not be stretched by database fan-out or by a
single slow ADS-B refresh. This guard keeps correctness-critical trajectory and
alert work on the foreground path while bounding or batching supporting I/O.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from typing import Any

from app.worker import monitor, v35

logger = logging.getLogger(__name__)

_POLL_BUDGET_S = 2.75
_PROVIDER_LEARNING_QUEUE_LIMIT = 32
_PROVIDER_LEARNING_WORKERS = 2
_INSTALLED = False

_ORIGINAL_GET_DB = monitor.get_db
_ORIGINAL_MATCH = monitor._match_user_aircraft
_ORIGINAL_GET_ACTIVE_USERS = monitor._get_active_users
_ORIGINAL_POLL = v35.SharedRegionPoller.poll
_ORIGINAL_RECORD_CYCLE = monitor.provider_learner.record_cycle_observation

_APPROACH_STATE_CONTEXT: contextvars.ContextVar[tuple[int, dict[str, dict[str, Any]]] | None] = (
    contextvars.ContextVar("plane_alerts_v423_approach_state_cache", default=None)
)

_learning_queue: asyncio.Queue | None = None
_learning_workers: list[asyncio.Task] = []
_learning_keys: set[tuple[int, str]] = set()


class _ApproachStatesCollectionProxy:
    def __init__(self, collection: Any, user_id: int, cache: dict[str, dict[str, Any]]) -> None:
        self._collection = collection
        self._user_id = int(user_id)
        self._cache = cache

    async def find_one(self, query: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        """Serve the hot-path user/aircraft lookup from one prefetched batch."""
        if isinstance(query, dict):
            uid = query.get("user_id")
            icao = query.get("aircraft_icao24")
            if (
                uid is not None
                and int(uid) == self._user_id
                and icao
                and set(query).issubset({"user_id", "aircraft_icao24"})
            ):
                return self._cache.get(str(icao))
        return await self._collection.find_one(query, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._collection, name)


class _DbProxy:
    def __init__(self, db: Any, user_id: int, cache: dict[str, dict[str, Any]]) -> None:
        self._db = db
        self._user_id = user_id
        self._cache = cache

    def __getitem__(self, name: str) -> Any:
        collection = self._db[name]
        if name == "approach_states":
            return _ApproachStatesCollectionProxy(collection, self._user_id, self._cache)
        return collection

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


def _cached_get_db() -> Any:
    db = _ORIGINAL_GET_DB()
    context = _APPROACH_STATE_CONTEXT.get()
    if context is None:
        return db
    user_id, cache = context
    return _DbProxy(db, user_id, cache)


async def _prefetch_approach_states(user_id: int, aircraft_list: list[Any]) -> dict[str, dict[str, Any]]:
    icao24s = sorted(
        {
            str(getattr(ac, "icao24", "") or "").strip()
            for ac in aircraft_list
            if str(getattr(ac, "icao24", "") or "").strip()
        }
    )
    if not icao24s:
        return {}

    collection = _ORIGINAL_GET_DB()["approach_states"]
    cache: dict[str, dict[str, Any]] = {}
    started = time.monotonic()
    cursor = collection.find(
        {
            "user_id": int(user_id),
            "aircraft_icao24": {"$in": icao24s},
        }
    )
    async for document in cursor:
        icao = str(document.get("aircraft_icao24") or "").strip()
        if icao:
            cache[icao] = document
    elapsed_ms = (time.monotonic() - started) * 1000.0
    if elapsed_ms >= 350.0:
        logger.warning(
            "approach_state_batch_slow user=%s aircraft=%d duration_ms=%.1f",
            user_id,
            len(icao24s),
            elapsed_ms,
        )
    return cache


async def _match_user_aircraft_batched(
    user: dict[str, Any],
    aircraft_list: list[Any],
    results_by_provider: dict[str, list[Any]],
) -> int:
    """Replace N sequential Mongo state reads with one query per user/cycle."""
    user_id = int(user["user_id"])
    from app.worker.timing import phase
    with phase("state_batch_read"):
        cache = await _prefetch_approach_states(user_id, aircraft_list)
    token = _APPROACH_STATE_CONTEXT.set((user_id, cache))
    try:
        return await _ORIGINAL_MATCH(user, aircraft_list, results_by_provider)
    finally:
        _APPROACH_STATE_CONTEXT.reset(token)


async def _get_active_users_batched() -> list[dict[str, Any]]:
    """Load all active users with three bounded queries instead of 1 + 2N."""
    cursor = monitor.users_col().find({"setup_complete": True}, {"user_id": 1})
    ids = [int(document["user_id"]) async for document in cursor]
    if not ids:
        return []

    locations: dict[int, dict[str, Any]] = {}
    preferences: dict[int, dict[str, Any]] = {}

    async for document in monitor.locations_col().find({"user_id": {"$in": ids}}):
        locations[int(document["user_id"])] = document
    async for document in monitor.preferences_col().find({"user_id": {"$in": ids}}):
        preferences[int(document["user_id"])] = document

    return [
        {
            "user_id": user_id,
            "location": locations[user_id],
            "preferences": preferences[user_id],
        }
        for user_id in ids
        if user_id in locations and user_id in preferences
        and locations[user_id].get("config_revision") == preferences[user_id].get("config_revision")
    ]


def _continuity_result_after_budget(
    poller: v35.SharedRegionPoller,
    region: v35.SharedPollRegion,
    elapsed_s: float,
) -> v35.PollResult:
    previous = poller._snapshots.get(region.key)
    if previous is not None and previous.aircraft:
        age = max(0.0, time.monotonic() - previous.fetched_mono)
        if age <= v35.CONTINUITY_TTL_S:
            logger.warning(
                "v423_provider_budget_continuity region=%s budget_s=%.2f elapsed_s=%.2f age_s=%.1f",
                region.key,
                _POLL_BUDGET_S,
                elapsed_s,
                age,
            )
            return v35.PollResult(
                aircraft=v35._aged_aircraft(previous.aircraft, age),
                by_provider=previous.by_provider,
                fresh=False,
                cache_hit=True,
                provider_queries=0,
                provider_name="budget-continuity",
                cache_age_s=age,
            )

    logger.warning(
        "v423_provider_budget_empty region=%s budget_s=%.2f elapsed_s=%.2f",
        region.key,
        _POLL_BUDGET_S,
        elapsed_s,
    )
    return v35.PollResult(
        aircraft=[],
        by_provider={},
        fresh=False,
        cache_hit=True,
        provider_queries=0,
        provider_name="budget-timeout",
        cache_age_s=0.0,
    )


async def _poll_bounded(
    self: v35.SharedRegionPoller,
    region: v35.SharedPollRegion,
    *,
    cycle_number: int,
    stagger_new_regions: bool,
) -> v35.PollResult:
    """Give provider refreshes a hard share of the five-second foreground budget."""
    started = time.monotonic()
    try:
        return await asyncio.wait_for(
            _ORIGINAL_POLL(
                self,
                region,
                cycle_number=cycle_number,
                stagger_new_regions=stagger_new_regions,
            ),
            timeout=_POLL_BUDGET_S,
        )
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - started
        return _continuity_result_after_budget(self, region, elapsed)


async def _learning_worker(queue: asyncio.Queue) -> None:
    while True:
        key, kwargs = await queue.get()
        try:
            await _ORIGINAL_RECORD_CYCLE(**kwargs)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "provider_learning_background_failed user=%s geohash=%s",
                key[0],
                key[1],
            )
        finally:
            _learning_keys.discard(key)
            queue.task_done()


def _ensure_learning_workers() -> asyncio.Queue:
    global _learning_queue, _learning_workers
    if _learning_queue is None:
        _learning_queue = asyncio.Queue(maxsize=_PROVIDER_LEARNING_QUEUE_LIMIT)
    if not _learning_workers or any(worker.done() for worker in _learning_workers):
        for worker in _learning_workers:
            if not worker.done():
                worker.cancel()
        _learning_workers = [
            asyncio.create_task(_learning_worker(_learning_queue), name=f"provider-learning-v423:{index}")
            for index in range(_PROVIDER_LEARNING_WORKERS)
        ]
    return _learning_queue


async def _record_cycle_observation_queued(
    user_id: int,
    geohash: str,
    results_by_provider: dict[str, list[Any]],
    user_lat: float,
    user_lon: float,
    radius_km: float,
) -> None:
    """Keep provider-learning database/AI work off the alert-critical path."""
    key = (int(user_id), str(geohash))
    if key in _learning_keys:
        return

    queue = _ensure_learning_workers()
    snapshot = {name: list(items) for name, items in results_by_provider.items()}
    kwargs = {
        "user_id": int(user_id),
        "geohash": str(geohash),
        "results_by_provider": snapshot,
        "user_lat": float(user_lat),
        "user_lon": float(user_lon),
        "radius_km": float(radius_km),
    }
    _learning_keys.add(key)
    try:
        queue.put_nowait((key, kwargs))
    except asyncio.QueueFull:
        _learning_keys.discard(key)
        logger.warning(
            "provider_learning_queue_full pending=%d limit=%d",
            queue.qsize(),
            _PROVIDER_LEARNING_QUEUE_LIMIT,
        )


def install_cadence_guard_v423() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    monitor.get_db = _cached_get_db
    monitor._match_user_aircraft = _match_user_aircraft_batched
    monitor._get_active_users = _get_active_users_batched
    v35.SharedRegionPoller.poll = _poll_bounded
    monitor.provider_learner.record_cycle_observation = _record_cycle_observation_queued
    _INSTALLED = True
    logger.info(
        "Plane Alerts v4.2.3 cadence guard enabled: provider_budget=%.2fs state_reads=batched provider_learning=background",
        _POLL_BUDGET_S,
    )
