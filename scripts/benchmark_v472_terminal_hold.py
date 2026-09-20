#!/usr/bin/env python3
"""Micro-benchmark for the v4.7.2 authoritative initial terminal-arrival hold."""
from __future__ import annotations

import json
from pathlib import Path
import statistics
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.intelligence import airport_terminal_v47 as terminal
from app.intelligence import route_history as route_mod
from app.intelligence import terminal_arrival_hold_v472 as hold_v472
from app.intelligence import trajectory as traj


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def main() -> int:
    airport = terminal.AirportGeometry(
        "TEST", "TST", "Benchmark Airport", 41.0, 29.0, 50.0,
        (
            terminal.RunwayGeometry(
                "TEST",
                terminal.RunwayEnd("05", 40.99, 28.99, 50.0),
                terminal.RunwayEnd("23", 41.01, 29.01, 230.0),
            ),
        ),
    )
    samples: list[terminal.TerminalSample] = []
    for timestamp, distance, altitude in zip(
        (960.0, 970.0, 980.0, 990.0, 1000.0),
        (32.0, 27.0, 22.0, 17.0, 13.0),
        (2700.0, 2450.0, 2200.0, 1950.0, 1700.0),
    ):
        lat, lon = traj.project_point(airport.latitude, airport.longitude, distance, 230.0)
        samples.append(terminal.TerminalSample(timestamp, lat, lon, altitude, 210.0, 50.0, -3.0, 0.0))

    assessment = terminal.TerminalAssessment(
        airport_icao="TEST",
        airport_iata="TST",
        terminal_area=True,
        airport_distance_km=13.0,
        state="TERMINAL_UNCERTAIN",
        state_confidence="Low",
        confidence_penalty=0.06,
        vector_state="STABLE_STRAIGHT",
        runway_id="",
        runway_family="",
        runway_confidence="Uncertain",
        runway_support_count=0,
        probable_holding=False,
        holding_score=0.0,
        go_around=False,
        missed_approach=False,
        runway_path_cpa_km=None,
        reasons=(),
    )
    pred = SimpleNamespace(
        stale=False,
        enters_alert_radius=True,
        current_distance_km=24.0,
        projected_closest_km=4.0,
        time_to_cpa_s=150.0,
        already_passed=False,
        state="Approaching",
    )
    destination = route_mod.AirportInfo(
        icao="TEST",
        iata="TST",
        latitude=airport.latitude,
        longitude=airport.longitude,
    )

    # Warm caches/import paths before measuring.
    for _ in range(200):
        hold_v472.evaluate_initial_terminal_hold(
            assessment,
            airport=airport,
            pred=pred,
            samples=samples,
            destination=destination,
            route_plausible=True,
            alert_radius_km=10.0,
            baseline_terminal_score=0.82,
            baseline_expected_turn_state="EXPECTED_TURN_PENDING",
            now=1000.0,
        )

    timings_ms: list[float] = []
    held = 0
    for _ in range(5000):
        start = time.perf_counter_ns()
        decision = hold_v472.evaluate_initial_terminal_hold(
            assessment,
            airport=airport,
            pred=pred,
            samples=samples,
            destination=destination,
            route_plausible=True,
            alert_radius_km=10.0,
            baseline_terminal_score=0.82,
            baseline_expected_turn_state="EXPECTED_TURN_PENDING",
            now=1000.0,
        )
        timings_ms.append((time.perf_counter_ns() - start) / 1_000_000.0)
        held += int(decision.hold)

    result = {
        "iterations": len(timings_ms),
        "held": held,
        "p50_ms": round(statistics.median(timings_ms), 4),
        "p95_ms": round(percentile(timings_ms, 0.95), 4),
        "p99_ms": round(percentile(timings_ms, 0.99), 4),
        "max_ms": round(max(timings_ms), 4),
    }
    print("V472_TERMINAL_HOLD_BENCHMARK_JSON=" + json.dumps(result, sort_keys=True, separators=(",", ":")))
    if held != len(timings_ms):
        raise SystemExit("benchmark scenario did not consistently produce the expected hold")
    if result["p95_ms"] >= 1.0:
        raise SystemExit(f"v4.7.2 terminal hold p95 too slow: {result['p95_ms']} ms")
    if result["max_ms"] >= 5.0:
        raise SystemExit(f"v4.7.2 terminal hold max too slow: {result['max_ms']} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
