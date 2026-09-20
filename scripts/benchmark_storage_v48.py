"""Plane Alerts v4.8 storage isolation benchmark.

Simulates 100 ms, 500 ms and 1 s persistence latency plus timeout/unavailable
conditions while repeatedly executing the memory-only operations used by the
five-second aircraft loop. The benchmark intentionally does not sleep for five
real seconds per sample; production cadence is verified separately from Railway
monitor_timing telemetry after deployment.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.storage_runtime_v48 import StorageRuntimeV48


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * q) - 1))
    return ordered[idx]


def distribution(values: list[float]) -> dict[str, float]:
    return {
        "p50_ms": round(percentile(values, 0.50), 4),
        "p95_ms": round(percentile(values, 0.95), 4),
        "p99_ms": round(percentile(values, 0.99), 4),
        "max_ms": round(max(values) if values else 0.0, 4),
    }


class SlowCollection:
    def __init__(self, delay: float, fail: bool = False):
        self.delay = delay
        self.fail = fail
        self.row = {
            "user_id": 1,
            "aircraft_icao24": "abc123",
            "active": True,
            "first_notification_sent": True,
            "updated_at": datetime.now(timezone.utc),
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        }

    async def replace_one(self, query, document, upsert=False):
        await asyncio.sleep(self.delay)
        if self.fail:
            raise TimeoutError("simulated storage failure")
        self.row = dict(document)
        return SimpleNamespace(matched_count=1, modified_count=1, upserted_id=None)

    async def find_one(self, query):
        await asyncio.sleep(self.delay)
        if self.fail:
            raise TimeoutError("simulated storage failure")
        return dict(self.row)


class SlowDB:
    def __init__(self, delay: float, fail: bool = False):
        self.collection = SlowCollection(delay, fail)

    def __getitem__(self, name):
        return self.collection


async def scenario(delay: float, fail: bool = False) -> dict[str, object]:
    runtime = StorageRuntimeV48()
    runtime._config_loaded = True
    runtime._last_config_refresh_mono = time.monotonic()
    runtime._users = {
        1: {
            "user_id": 1,
            "location": {"latitude": 41.0, "longitude": 29.0, "config_revision": "r1"},
            "preferences": {"config_revision": "r1", "aircraft_filter": {"mode": "all"}},
            "admin_control": {},
        }
    }
    runtime._cache_state(
        {
            "user_id": 1,
            "aircraft_icao24": "abc123",
            "active": True,
            "first_notification_sent": True,
            "updated_at": datetime.now(timezone.utc),
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        },
        dirty=False,
    )
    cache = runtime.approach_states(1, {"abc123"})
    runtime.apply_approach_update(
        1,
        cache,
        {"aircraft_icao24": "abc123"},
        {"$set": {"updated_at": datetime.now(timezone.utc) + timedelta(seconds=1)}},
    )

    db = SlowDB(delay, fail)
    flush = asyncio.create_task(runtime.flush_once(db))
    samples: list[float] = []
    for _ in range(5000):
        started = time.perf_counter()
        runtime.active_users()
        runtime.approach_states(1, {"abc123"})
        samples.append((time.perf_counter() - started) * 1000.0)
        if _ % 250 == 0:
            await asyncio.sleep(0)
    try:
        await flush
    except Exception:
        pass
    result = distribution(samples)
    result.update({"simulated_db_latency_ms": int(delay * 1000), "simulated_failure": fail})
    # A storage delay must not consume a meaningful fraction of the 5-second loop.
    if result["p99_ms"] >= 10.0 or result["max_ms"] >= 100.0:
        raise SystemExit(f"v4.8 storage isolation regression: {result}")
    return result


async def main() -> None:
    results = [
        await scenario(0.1),
        await scenario(0.5),
        await scenario(1.0),
        await scenario(0.05, fail=True),
    ]
    print("STORAGE_V48_BENCHMARK_JSON=" + json.dumps({"scenarios": results}, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
