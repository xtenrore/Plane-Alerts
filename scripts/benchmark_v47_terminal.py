"""Deterministic micro-benchmark for Plane Alerts v4.7 terminal intelligence."""
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

# Executing a file under scripts/ puts scripts/ first on sys.path. Add the
# repository root explicitly so this benchmark behaves identically in CI/local
# use, matching the established v4.6 benchmark pattern.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.intelligence import airport_terminal_v47 as v47  # noqa: E402


def percentile(values, q):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(len(ordered) * q) - 1))]


def main() -> None:
    v47.reset_v47_state_for_tests()
    samples = [
        v47.TerminalSample(i * 5.0, 41.18 + i * .006, 28.7099, 1800 - i * 60, 175, 354, -2.5, 0)
        for i in range(16)
    ]
    timings = []
    for _ in range(5000):
        started = time.perf_counter()
        result = v47.assess_terminal(samples, v47.LTFM, now=75.0, observer_lat=41.25, observer_lon=28.56)
        timings.append((time.perf_counter() - started) * 1000)
    assert result.terminal_area
    p50 = statistics.median(timings)
    p95 = percentile(timings, .95)
    p99 = percentile(timings, .99)
    worst = max(timings)
    # Pure CPU work over bounded history. Keep a generous CI limit while
    # preventing accidental database/network/clustering work on this path.
    assert p95 < 5.0, f"terminal assessment p95 too slow: {p95:.3f}ms"
    print(f"v4.7 terminal assessment: p50={p50:.3f}ms p95={p95:.3f}ms p99={p99:.3f}ms max={worst:.3f}ms")


if __name__ == "__main__":
    main()
