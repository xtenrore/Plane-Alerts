#!/usr/bin/env python3
"""Micro-benchmark for v5.4 preset application and menu construction.

This is intentionally off the alert-critical path; the gate prevents profile UX
helpers from becoming unexpectedly expensive on constrained self-hosted nodes.
"""
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.profile_presets_v54 import PRESETS, apply_preset


BASE = {
    "location": {"latitude": 41.0, "longitude": 29.0, "geohash": "sxk9", "radius_km": 15.0},
    "preferences": {
        "proximity_3d": {"altitude_relevance": True},
        "aircraft_filter": {"mode": "all", "selected_categories": [], "selected_types": [], "excluded_types": []},
        "filter_rules": {"profile": {}, "categories": {}, "aircraft": {}},
    },
}


def main() -> int:
    timings_ms: list[float] = []
    for i in range(6000):
        preset = PRESETS[i % len(PRESETS)]
        started = time.perf_counter()
        config = apply_preset(BASE, preset.preset_id)
        assert config["location"]["latitude"] == 41.0
        timings_ms.append((time.perf_counter() - started) * 1000.0)

    timings_ms.sort()
    p50 = statistics.median(timings_ms)
    p95 = timings_ms[int(len(timings_ms) * 0.95) - 1]
    p99 = timings_ms[int(len(timings_ms) * 0.99) - 1]
    maximum = max(timings_ms)
    print(f"v5.4 preset UX benchmark p50={p50:.4f}ms p95={p95:.4f}ms p99={p99:.4f}ms max={maximum:.4f}ms")
    if p95 >= 0.5:
        raise SystemExit(f"v5.4 preset UX p95 regression: {p95:.4f}ms >= 0.5ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
