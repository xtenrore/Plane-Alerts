"""Synthetic v4.5 provider critical-path benchmark.

Hardware-independent by design. Real five-second cadence is verified from
production monitor_timing telemetry after deployment.
"""
from __future__ import annotations

import asyncio
import statistics
import time

from app.aircraft.models import NormalizedAircraft
from app.aircraft.providers import AircraftDataProvider, ProviderManager


class BenchProvider(AircraftDataProvider):
    def __init__(self, name: str, delay_s: float) -> None:
        super().__init__()
        self.name = name
        self.delay_s = delay_s
        self.request_timeout_s = 0.5

    async def get_aircraft_in_area(self, latitude, longitude, radius_nm=250):
        await asyncio.sleep(self.delay_s)
        now = time.time()
        return [NormalizedAircraft(icao24="4b1801", latitude=latitude, longitude=longitude, velocity=200.0, heading=270.0, position_age_s=0.2, observed_at=now-0.2, received_at=now, timestamp=now-0.2, source=self.name, field_provenance={"latitude": self.name, "longitude": self.name})]


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered)-1, int((len(ordered)-1)*q))]


async def main() -> None:
    manager = ProviderManager()
    local = BenchProvider("local", 0.020)
    public = BenchProvider("adsb.lol", 0.035)
    manager._provider_by_name["local"] = local
    manager._provider_by_name["adsb.lol"] = public
    durations: list[float] = []
    for _ in range(80):
        started = time.perf_counter()
        local_result, public_result = await asyncio.gather(manager._safe_query(local, 41.0, 29.0, 100), manager._safe_query(public, 41.0, 29.0, 100))
        merged = manager._merge_results({"local": local_result, "adsb.lol": public_result})
        assert len(merged) == 1
        durations.append((time.perf_counter()-started)*1000.0)
    p50 = statistics.median(durations); p95 = percentile(durations, .95); p99 = percentile(durations, .99); worst = max(durations)
    print(f"v4.5 synthetic provider benchmark: p50={p50:.1f}ms p95={p95:.1f}ms p99={p99:.1f}ms worst={worst:.1f}ms")
    assert p99 < 250.0


if __name__ == "__main__":
    asyncio.run(main())
