#!/usr/bin/env python3
"""Benchmark local worldwide airport lookup used by v4.7.1 terminal context."""
from __future__ import annotations

import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.intelligence.airport_repository import AirportRepository  # noqa: E402

LOCATIONS = (
    (41.275, 28.732),   # Istanbul
    (51.470, -0.454),   # London Heathrow
    (40.641, -73.778),  # New York JFK
    (35.549, 139.779),  # Tokyo Haneda
    (25.253, 55.365),   # Dubai
    (-33.940, 151.175), # Sydney
    (-23.435, -46.473), # Sao Paulo
    (-26.139, 28.246),  # Johannesburg
    (1.364, 103.991),   # Singapore
    (61.174, -149.998), # Anchorage
)


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * p))))
    return ordered[index]


def main() -> int:
    db = ROOT / "data" / "aviation" / "compiled" / "global_airports.sqlite3"
    repo = AirportRepository(db)
    if not repo.available:
        raise SystemExit("global airport database unavailable")

    # Warm immutable SQLite pages and code/runway caches before measuring the
    # steady-state critical-path lookup cost.
    for lat, lon in LOCATIONS:
        if repo.nearest(lat, lon, max_distance_km=120) is None:
            raise SystemExit(f"no airport near benchmark location {lat},{lon}")

    samples: list[float] = []
    for repeat in range(40):
        jitter = (repeat % 7 - 3) * 0.004
        for lat, lon in LOCATIONS:
            started = time.perf_counter()
            result = repo.nearest(lat + jitter, lon - jitter, max_distance_km=120)
            elapsed_ms = (time.perf_counter() - started) * 1000
            if result is None:
                raise SystemExit(f"lookup unexpectedly empty near {lat},{lon}")
            samples.append(elapsed_ms)

    payload = {
        "iterations": len(samples),
        "p50_ms": round(statistics.median(samples), 4),
        "p95_ms": round(percentile(samples, 0.95), 4),
        "p99_ms": round(percentile(samples, 0.99), 4),
        "max_ms": round(max(samples), 4),
    }
    print("V471_AIRPORT_LOOKUP_BENCHMARK_JSON=" + json.dumps(payload, sort_keys=True, separators=(",", ":")))
    if payload["p95_ms"] > 8.0 or payload["max_ms"] > 25.0:
        raise SystemExit(f"airport lookup benchmark exceeded budget: {payload}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
