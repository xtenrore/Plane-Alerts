"""Community local-receiver relevance guard for Plane Alerts v5.5."""
from __future__ import annotations

import math
import os
from typing import Any

_INSTALLED = False


def _float_env(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def receiver_coverage_config() -> tuple[float, float, float] | None:
    lat = _float_env("LOCAL_ADSB_RECEIVER_LATITUDE")
    lon = _float_env("LOCAL_ADSB_RECEIVER_LONGITUDE")
    radius = _float_env("LOCAL_ADSB_RECEIVER_COVERAGE_KM")
    if lat is None or lon is None or radius is None:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0 and 1.0 <= radius <= 800.0):
        return None
    return lat, lon, radius


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return radius * 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def local_receiver_relevant(latitude: float, longitude: float, radius_nm: int | float) -> bool:
    """Return whether a requested profile area overlaps configured receiver coverage.

    Missing coverage metadata preserves the old manual-URL behavior. The guided
    installer always records receiver location separately from Telegram profile
    locations when a local receiver is enabled.
    """
    configured = receiver_coverage_config()
    if configured is None:
        return True
    receiver_lat, receiver_lon, coverage_km = configured
    query_radius_km = max(0.0, float(radius_nm)) * 1.852
    distance_km = _haversine_km(receiver_lat, receiver_lon, float(latitude), float(longitude))
    return distance_km <= coverage_km + query_radius_km


def install_local_receiver_coverage_guard() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    from app.aircraft.providers import LocalADSBProvider

    original = LocalADSBProvider.get_aircraft_in_area

    async def guarded(self: Any, latitude: float, longitude: float, radius_nm: int = 250):
        if not local_receiver_relevant(latitude, longitude, radius_nm):
            return []
        return await original(self, latitude, longitude, radius_nm)

    LocalADSBProvider.get_aircraft_in_area = guarded  # type: ignore[method-assign]
    _INSTALLED = True
