from __future__ import annotations

import asyncio
from pathlib import Path
import statistics
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.intelligence.destination_path_guard_v554 import (  # noqa: E402
    CacheEntry,
    DestinationResolution,
    destination_resolver,
    evaluate_destination_path,
)
from app.intelligence import route_history as route_mod  # noqa: E402


async def main() -> None:
    destination_resolver.clear()
    airport = route_mod.AirportInfo(
        icao="LTFM", iata="IST", name="Istanbul Airport",
        latitude=41.2753, longitude=28.7519,
    )
    route = route_mod.FlightRouteInfo(
        callsign="THY123",
        airport_codes="AYT-IST",
        plausible=True,
        origin=route_mod.AirportInfo(
            icao="LTAI", iata="AYT", name="Antalya Airport",
            latitude=36.8987, longitude=30.8005,
        ),
        destination=airport,
    )
    destination_resolver._cache["THY123"] = CacheEntry(
        resolution=DestinationResolution(route, "adsb.lol+adsbdb", "provider-agreement"),
        state="resolved",
        expires_at=time.monotonic() + 300.0,
    )
    ac = SimpleNamespace(
        callsign="THY123",
        latitude=41.10,
        longitude=28.95,
        velocity=220.0,
        ground_speed=427.6,
        heading=90.0,
        altitude=3200.0,
        vertical_rate_mps=-4.0,
    )
    pred = SimpleNamespace(
        current_distance_km=18.0,
        projected_closest_km=3.0,
        time_to_cpa_s=130.0,
        radius_entry_s=105.0,
        path=[],
        stale=False,
    )

    timings = []
    for _ in range(1000):
        started = time.perf_counter()
        await evaluate_destination_path(
            None,
            ac,
            pred,
            user_lat=41.10,
            user_lon=29.22,
            alert_radius_km=10.0,
            current_samples=(),
            notification_sent=False,
        )
        timings.append((time.perf_counter() - started) * 1000.0)

    ordered = sorted(timings)
    p99 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]
    maximum = max(ordered)
    median = statistics.median(ordered)
    assert p99 < 20.0, f"destination gate p99 too slow: {p99:.3f} ms"
    assert maximum < 50.0, f"destination gate max too slow: {maximum:.3f} ms"

    # Prove a deliberately slow provider refresh is scheduled rather than awaited.
    destination_resolver.clear()
    original_refresh = destination_resolver._refresh

    async def slow_refresh(key: str, latitude: float, longitude: float, **kwargs) -> None:
        await asyncio.sleep(0.5)
        destination_resolver._put(key, None, "unavailable", 30.0)

    destination_resolver._refresh = slow_refresh
    try:
        started = time.perf_counter()
        result = await evaluate_destination_path(
            None,
            ac,
            pred,
            user_lat=41.10,
            user_lon=29.22,
            alert_radius_km=10.0,
            current_samples=(),
            notification_sent=False,
        )
        nonblocking_ms = (time.perf_counter() - started) * 1000.0
        assert result.suppress_alert
        assert nonblocking_ms < 50.0, f"slow provider leaked into monitor path: {nonblocking_ms:.3f} ms"
    finally:
        destination_resolver._refresh = original_refresh
        destination_resolver.clear()
        await asyncio.sleep(0)

    print(
        "DESTINATION_PATH_BENCHMARK "
        f"median_ms={median:.4f} p99_ms={p99:.4f} max_ms={maximum:.4f} "
        f"slow_provider_return_ms={nonblocking_ms:.4f}"
    )


if __name__ == "__main__":
    asyncio.run(main())
