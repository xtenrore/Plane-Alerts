#!/usr/bin/env python3
"""Deterministic v5.3 multi-location scale benchmark."""
from __future__ import annotations

from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.aircraft.models import NormalizedAircraft
from app.intelligence import trajectory as base
from app.intelligence import trajectory_scale_v53 as scaled
from app.worker import scale_v53


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return ordered[index]


def _user(uid: int) -> dict:
    row = uid // 25
    col = uid % 25
    return {
        "user_id": uid,
        "location": {
            "latitude": 40.65 + row * 0.035,
            "longitude": 28.35 + col * 0.035,
            "radius_km": 15.0,
            "geohash": f"bench-{uid}",
        },
        "preferences": {},
    }


def _aircraft(index: int) -> NormalizedAircraft:
    row = index // 40
    col = index % 40
    return NormalizedAircraft(
        icao24=f"{index + 1000:06x}",
        callsign=f"B{index:04d}",
        latitude=40.45 + row * 0.055,
        longitude=28.10 + col * 0.045,
        altitude=5000.0,
        velocity=220.0,
        heading=90.0,
        position_age_s=1.0,
        aircraft_type="A320",
    )


def _samples(now: float) -> list[base.HistorySample]:
    return [
        base.HistorySample(now - 10.0, 41.0, 28.6, 6000.0, 410.0, 90.0, 0.0, 0.5),
        base.HistorySample(now - 5.0, 41.0, 28.62, 6000.0, 410.0, 90.0, 0.0, 0.5),
    ]


def main() -> int:
    users = [_user(uid) for uid in range(500)]
    aircraft = [_aircraft(index) for index in range(600)]

    spatial_ms: list[float] = []
    last_stats = None
    for _ in range(25):
        started = time.perf_counter()
        _, last_stats = scale_v53.partition_aircraft_by_user(users, aircraft)
        spatial_ms.append((time.perf_counter() - started) * 1000.0)

    assert last_stats is not None
    spatial_p95 = _percentile(spatial_ms, 0.95)
    if spatial_p95 >= 250.0:
        raise SystemExit(f"v5.3 spatial p95 too slow: {spatial_p95:.2f} ms")
    if last_stats.capped_users:
        raise SystemExit("v5.3 benchmark unexpectedly hit per-user candidate cap")

    scaled.reset_motion_cache_for_tests()
    now = 1_800_000_000.0
    samples = _samples(now)
    motion_ms: list[float] = []
    for index in range(200):
        started = time.perf_counter()
        scaled.predict_trajectory(
            samples,
            40.5 + (index % 20) * 0.03,
            28.7 + (index // 20) * 0.03,
            20.0,
            now=now,
            user_altitude_m=100.0,
        )
        motion_ms.append((time.perf_counter() - started) * 1000.0)

    cache = scaled.motion_cache_snapshot()
    if cache["misses"] != 1 or cache["hits"] != 199 or cache["entries"] != 1:
        raise SystemExit(f"v5.3 motion reuse failed: {cache}")
    motion_p95 = _percentile(motion_ms[1:], 0.95)
    if motion_p95 >= 10.0:
        raise SystemExit(f"v5.3 shared observer evaluation p95 too slow: {motion_p95:.3f} ms")

    print(
        "v5.3 scale benchmark",
        f"spatial_p50_ms={statistics.median(spatial_ms):.3f}",
        f"spatial_p95_ms={spatial_p95:.3f}",
        f"candidate_pairs={last_stats.candidate_pairs}",
        f"full_pairs={last_stats.full_pairs}",
        f"pair_reduction={last_stats.as_dict()['pair_reduction_ratio']:.3f}",
        f"shared_eval_p95_ms={motion_p95:.3f}",
        f"motion_cache_hits={cache['hits']}",
        f"motion_cache_entries={cache['entries']}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
