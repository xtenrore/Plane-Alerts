#!/usr/bin/env python3
"""Micro-benchmark for Plane Alerts v5.1 deterministic 3D proximity."""
from __future__ import annotations

import json
import statistics
import time

from app.intelligence.trajectory import HistorySample, predict_trajectory

NOW = 2_000_000_000.0
USER_LAT = 41.0
USER_LON = 29.0


def _history() -> list[HistorySample]:
    return [
        HistorySample(NOW - 15, 41.24, 29.0, 4200.0, 410.0, 180.0, -4.0, 0.0),
        HistorySample(NOW - 10, 41.22, 29.0, 4180.0, 411.0, 180.0, -4.0, 0.0),
        HistorySample(NOW - 5, 41.20, 29.0, 4160.0, 412.0, 180.0, -4.0, 0.0),
        HistorySample(NOW, 41.18, 29.0, 4140.0, 412.0, 180.0, -4.0, 0.0),
    ]


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return ordered[idx]


def main() -> None:
    history = _history()
    timings_ms: list[float] = []
    for _ in range(2500):
        started = time.perf_counter()
        prediction = predict_trajectory(
            history,
            USER_LAT,
            USER_LON,
            8.0,
            now=NOW,
            user_altitude_m=120.0,
            altitude_relevance=True,
        )
        timings_ms.append((time.perf_counter() - started) * 1000.0)
        if prediction.projected_closest_3d_km is None:
            raise SystemExit("v5.1 benchmark did not produce true 3D CPA")

    result = {
        "samples": len(timings_ms),
        "p50_ms": round(statistics.median(timings_ms), 4),
        "p95_ms": round(percentile(timings_ms, 0.95), 4),
        "p99_ms": round(percentile(timings_ms, 0.99), 4),
        "max_ms": round(max(timings_ms), 4),
    }
    print("V51_GEOMETRY_BENCHMARK_JSON=" + json.dumps(result, sort_keys=True, separators=(",", ":")))

    # The five-second production cadence is far above this budget. Keep the
    # micro-gate intentionally strict enough to catch accidental heavy work in
    # the per-aircraft geometry path without being flaky on shared CI runners.
    if result["p50_ms"] >= 2.0 or result["p99_ms"] >= 8.0:
        raise SystemExit(f"v5.1 geometry benchmark exceeded budget: {result}")


if __name__ == "__main__":
    main()
