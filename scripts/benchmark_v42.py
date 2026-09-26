"""Deterministic compatibility benchmark for the canonical route guard."""
from __future__ import annotations

import json
import sys
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.intelligence.route_guard import _path_geometry  # noqa: E402
from app.intelligence.route_history import AirportInfo  # noqa: E402
from app.intelligence.trajectory import ProjectedPoint  # noqa: E402


def main() -> None:
    destination = AirportInfo(
        icao="LTFM",
        iata="IST",
        latitude=41.2753,
        longitude=28.7519,
    )
    path = [
        ProjectedPoint(
            seconds=float(seconds),
            latitude=41.0 + seconds * 0.0002,
            longitude=29.15 - seconds * 0.0005,
            horizontal_km=1.0,
            slant_km=1.0,
            altitude_m=3000.0,
            heading_deg=270.0,
        )
        for seconds in range(0, 301, 5)
    ]
    prediction = SimpleNamespace(path=path, time_to_cpa_s=180.0)

    tracemalloc.start()
    started = time.perf_counter()
    evaluations = 0
    for _ in range(2500):
        cpa_destination, landing_time = _path_geometry(
            prediction,
            destination,
            180.0,
        )
        assert cpa_destination is not None
        assert landing_time is None or landing_time >= 0.0
        evaluations += 1
    elapsed = time.perf_counter() - started
    current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    report = {
        "evaluations": evaluations,
        "elapsed_ms": round(elapsed * 1000.0, 3),
        "evaluations_per_second": round(evaluations / max(elapsed, 1e-9), 1),
        "current_kib": round(current_bytes / 1024.0, 1),
        "peak_kib": round(peak_bytes / 1024.0, 1),
        "canonical_route_guard": True,
    }
    print("ROUTE_GUARD_COMPAT_BENCHMARK_JSON=" + json.dumps(report, sort_keys=True))

    assert peak_bytes < 64 * 1024 * 1024
    assert elapsed < 10.0


if __name__ == "__main__":
    main()
