#!/usr/bin/env python3
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.shadow_evaluation_v52 import evaluate_snapshot_outcome  # noqa: E402


SNAPSHOT = {
    "_id": "bench-snapshot",
    "release_version": "5.2.0",
    "prediction_version": "5.1-3d-proximity",
    "captured_at": "2026-09-21T05:00:00+00:00",
    "aircraft_icao24": "abc123",
    "user_id": 1,
    "alert_radius_km": 9.0,
    "projected_closest_km": 5.0,
    "time_to_cpa_s": 120.0,
    "enters_alert_radius": True,
    "confidence_score": 0.82,
    "horizon_bucket": "0-10m",
    "coverage_mode": "regional_adsb_current_predictor",
    "diagnostics": {
        "v46": {
            "linear_shadow": {"model_version": "4.6-turn-shadow-1", "mode": "linear", "cpa_km": 6.0, "eta_s": 130.0, "enters_radius": True},
            "turn_shadow": {"model_version": "4.6-turn-shadow-1", "mode": "turn-aware", "cpa_km": 4.4, "eta_s": 118.0, "enters_radius": True},
        }
    },
}
OUTCOME = {
    "_id": "bench-outcome",
    "outcome": "passed",
    "outcome_basis": "observed_in_radius_pass",
    "scoreable": True,
    "captured_at": "2026-09-21T05:02:00+00:00",
    "observed_closest_km": 4.2,
}


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return ordered[index]


def main() -> int:
    samples_ms: list[float] = []
    for _ in range(5000):
        start = time.perf_counter()
        rows = evaluate_snapshot_outcome(SNAPSHOT, OUTCOME)
        elapsed = (time.perf_counter() - start) * 1000.0
        if len(rows) != 3:
            raise SystemExit("unexpected shadow evaluation row count")
        samples_ms.append(elapsed)

    p50 = statistics.median(samples_ms)
    p95 = percentile(samples_ms, 0.95)
    p99 = percentile(samples_ms, 0.99)
    maximum = max(samples_ms)
    print(f"V52_SHADOW_EVAL_BENCH p50_ms={p50:.4f} p95_ms={p95:.4f} p99_ms={p99:.4f} max_ms={maximum:.4f}")
    # Evaluation is optional/off-path, but keep the pure calculation comfortably
    # below 1 ms p95 so replay/reporting cannot become gratuitously expensive.
    return 0 if p95 < 1.0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
