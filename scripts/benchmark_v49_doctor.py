#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.doctor_v49 import static_checks  # noqa: E402


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def main() -> int:
    config = Settings(
        _env_file=None,
        telegram_bot_token="123456:test-token",
        mongo_uri="mongodb://localhost:27017",
        poll_interval_seconds=5,
        default_radius_km=15.0,
    )
    samples: list[float] = []
    for _ in range(2000):
        started = time.perf_counter()
        checks = static_checks(config)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if any(check.status == "fail" for check in checks):
            raise SystemExit("valid doctor configuration unexpectedly failed")
        samples.append(elapsed_ms)

    payload = {
        "iterations": len(samples),
        "p50_ms": round(statistics.median(samples), 4),
        "p95_ms": round(percentile(samples, 0.95), 4),
        "p99_ms": round(percentile(samples, 0.99), 4),
        "max_ms": round(max(samples), 4),
    }
    print("V49_DOCTOR_BENCHMARK_JSON=" + json.dumps(payload, sort_keys=True, separators=(",", ":")))
    if payload["p99_ms"] > 5.0:
        raise SystemExit(f"doctor static diagnostics exceeded budget: {payload}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
