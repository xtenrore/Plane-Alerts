from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.prediction_lab_files_v55 import write_evidence


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "prediction_lab"
        samples = []
        for index in range(200):
            started = time.perf_counter()
            write_evidence({
                "kind": "benchmark",
                "aircraft_icao24": f"{index:06x}",
                "current_distance_km": 12.3,
                "projected_closest_km": 4.2,
                "time_to_cpa_s": 180.0,
                "confidence": "High",
            }, root=root)
            samples.append((time.perf_counter() - started) * 1000.0)
        samples.sort()
        p95 = samples[int(len(samples) * 0.95) - 1]
        # Direct disk writes are isolated from the live monitor, but this bound
        # catches accidentally pathological evidence payloads or fsync behavior.
        assert p95 < 50.0, f"Prediction Lab atomic write p95 too slow: {p95:.2f}ms"
        print(f"prediction_lab_v55 atomic_write_p95_ms={p95:.3f} n={len(samples)}")


if __name__ == "__main__":
    main()
