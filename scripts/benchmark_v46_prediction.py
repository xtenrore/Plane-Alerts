"""Synthetic performance gate for Plane Alerts v4.6 prediction intelligence.

This benchmark intentionally exercises only deterministic in-memory work. Route
history loading/clustering remains background work; the cached route evidence cost
is measured separately from clustering so neither can hide in the five-second loop.
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

# Executing a file under scripts/ puts scripts/ first on sys.path. Add the
# repository root explicitly so this benchmark behaves the same in CI/local use.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.intelligence import trajectory as trajectory  # noqa: E402
from app.intelligence.prediction_v46 import (  # noqa: E402
    diagnostics_for,
    position_uncertainty_km,
    predict_trajectory_v46,
    turn_evidence,
)
from app.intelligence.route_history import RoutePoint  # noqa: E402
from app.intelligence.route_intelligence_v46 import (  # noqa: E402
    RouteHistoryBundle,
    _cluster_paths,
    history_features_v46,
)

NOW = 2_000_000_000.0
USER_LAT = 41.0
USER_LON = 29.0
RADIUS_KM = 12.0


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _measure(fn, iterations: int) -> dict[str, float]:
    values: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        fn()
        values.append((time.perf_counter() - started) * 1000.0)
    return {
        "p50_ms": round(_percentile(values, 0.50), 4),
        "p95_ms": round(_percentile(values, 0.95), 4),
        "p99_ms": round(_percentile(values, 0.99), 4),
        "max_ms": round(max(values), 4),
    }


def _sample(index: int) -> trajectory.HistorySample:
    seconds_ago = (11 - index) * 5.0
    heading = 176.0 + index * 0.7
    return trajectory.HistorySample(
        timestamp=NOW - seconds_ago,
        latitude=41.28 - index * 0.012,
        longitude=28.99 + index * 0.0015,
        altitude_m=4800.0 - index * 18.0,
        speed_kts=410.0 + math.sin(index) * 2.0,
        heading_deg=heading,
        vertical_rate_mps=-2.5,
        position_age_s=0.4,
    )


def _route(offset: float, bend: float = 0.0) -> list[RoutePoint]:
    out: list[RoutePoint] = []
    for index in range(24):
        fraction = index / 23.0
        out.append(RoutePoint(
            latitude=41.34 - fraction * 0.44 + bend * fraction * fraction,
            longitude=28.86 + offset + fraction * 0.31,
            timestamp=NOW - (23 - index) * 30.0,
        ))
    return out


def main() -> None:
    samples = [_sample(index) for index in range(12)]
    current_path = [RoutePoint(s.latitude, s.longitude, s.timestamp) for s in samples]
    paths = [
        _route((index % 3) * 0.003, 0.002 if index % 2 else 0.0)
        for index in range(12)
    ]
    clusters = _cluster_paths(paths)
    bundle = RouteHistoryBundle(paths, age_days=[float(index + 1) for index in range(len(paths))], clusters=clusters)

    baseline = _measure(
        lambda: trajectory.predict_trajectory(samples, USER_LAT, USER_LON, RADIUS_KM, now=NOW),
        400,
    )
    full = _measure(
        lambda: predict_trajectory_v46(samples, USER_LAT, USER_LON, RADIUS_KM, now=NOW),
        400,
    )
    turn = _measure(lambda: turn_evidence(samples, now=NOW), 1000)
    uncertainty = _measure(lambda: position_uncertainty_km(samples, now=NOW), 1000)
    history = _measure(
        lambda: history_features_v46(
            current_path,
            bundle,
            observer_lat=USER_LAT,
            observer_lon=USER_LON,
            radius_km=RADIUS_KM,
            aircraft_lat=samples[-1].latitude,
            aircraft_lon=samples[-1].longitude,
        ),
        150,
    )
    clustering = _measure(lambda: _cluster_paths(paths), 60)

    probe = predict_trajectory_v46(samples, USER_LAT, USER_LON, RADIUS_KM, now=NOW)
    diagnostics = diagnostics_for(probe)
    if not diagnostics.get("shadow_only"):
        raise SystemExit("v4.6 benchmark expected turn candidate to remain shadow-only")

    # Broad CI ceilings detect accidental blocking/algorithmic regressions while
    # avoiding hardware-specific microbenchmark flakiness.
    if baseline["p95_ms"] > 5.0:
        raise SystemExit(f"baseline predictor p95 too slow: {baseline}")
    if full["p95_ms"] > 12.0:
        raise SystemExit(f"v4.6 full predictor p95 too slow: {full}")
    if turn["p95_ms"] > 2.0 or uncertainty["p95_ms"] > 2.0:
        raise SystemExit(f"v4.6 evidence primitives too slow: turn={turn} uncertainty={uncertainty}")
    if history["p95_ms"] > 20.0:
        raise SystemExit(f"cached route evidence p95 too slow: {history}")
    if clustering["p95_ms"] > 80.0:
        raise SystemExit(f"background route clustering p95 too slow: {clustering}")

    print("V46_PREDICTION_BENCHMARK_JSON=" + json.dumps({
        "linear_predictor": baseline,
        "full_v46_prediction": full,
        "turn_evidence": turn,
        "position_uncertainty": uncertainty,
        "cached_route_history_evidence": history,
        "background_route_clustering": clustering,
        "route_paths": len(paths),
        "route_clusters": len(set(clusters)),
        "shadow_only": True,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
