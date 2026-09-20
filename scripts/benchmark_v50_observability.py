#!/usr/bin/env python3
"""Micro-benchmark the deterministic v5.0 diagnostics formatter.

The operator surface is off the alert-critical path, but this gate prevents a
future explanation formatter from becoming unexpectedly expensive when an
operator asks for a page of recent aircraft decisions.
"""
from __future__ import annotations

import statistics
import time

from app.observability_v50 import explain_prediction

SAMPLE = {
    "user_id": 1,
    "aircraft_icao24": "4bab24",
    "callsign": "THY5DQ",
    "aircraft_type": "A321",
    "current_distance_km": 14.1,
    "projected_closest_km": 7.8,
    "time_to_cpa_s": 165.0,
    "state": "Approaching",
    "confidence": "Medium",
    "confidence_score": 0.61,
    "enters_alert_radius": True,
    "alert_radius_km": 10.0,
    "qualifies": False,
    "route_suppressed": True,
    "route_reason": "terminal-arrival evidence",
    "diagnostics": {
        "sample_age_s": 2.1,
        "sample_count": 8,
        "fresh_observation": True,
        "turn_rate_deg_s": -0.2,
        "route_destination": "TIA",
        "route_history_days": 3,
        "terminal_arrival_state": "TERMINAL_UNCERTAIN",
        "observation": {
            "source": "adsb.fi",
            "source_candidates": ["adsb.fi", "adsb.lol"],
            "data_quality": "good",
            "field_provenance": {"position": "adsb.fi", "speed": "adsb.fi"},
            "merge_notes": [],
        },
    },
}


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * p))))
    return ordered[index]


def main() -> int:
    samples_ms: list[float] = []
    for _ in range(3000):
        started = time.perf_counter()
        result = explain_prediction(SAMPLE)
        samples_ms.append((time.perf_counter() - started) * 1000.0)
        if result["decision"] != "route-or-terminal-suppressed":
            raise SystemExit("unexpected explanation result")

    p50 = percentile(samples_ms, 0.50)
    p95 = percentile(samples_ms, 0.95)
    p99 = percentile(samples_ms, 0.99)
    maximum = max(samples_ms)
    print(
        f"v5.0 diagnostics formatter: p50={p50:.4f} ms p95={p95:.4f} ms "
        f"p99={p99:.4f} ms max={maximum:.4f} ms mean={statistics.fmean(samples_ms):.4f} ms"
    )
    if p95 >= 1.0:
        raise SystemExit(f"v5.0 diagnostics p95 regression: {p95:.4f} ms >= 1.0 ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
