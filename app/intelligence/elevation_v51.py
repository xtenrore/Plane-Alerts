"""Optional observer-elevation enrichment for Plane Alerts v5.1.

Elevation is fetched outside the alert-critical path using the same free
Open-Meteo provider already used for photographic conditions. Failure is benign:
3D proximity automatically falls back to conservative/horizontal geometry.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.database import locations_col
from app.intelligence.proximity3d_v51 import OBSERVER_ELEVATION_MAX_M, OBSERVER_ELEVATION_MIN_M


def _valid(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if not OBSERVER_ELEVATION_MIN_M <= number <= OBSERVER_ELEVATION_MAX_M:
        return None
    return number


async def resolve_observer_elevation(user_id: int, latitude: float, longitude: float) -> float | None:
    """Best-effort terrain elevation lookup and persistence.

    This function is intended for the bounded optional-work queue. It must never
    be awaited by the physical prediction path.
    """
    timeout_s = max(0.5, min(3.0, float(settings.photography_http_timeout_seconds)))
    params = {
        "latitude": float(latitude),
        "longitude": float(longitude),
        "timezone": "UTC",
        "current": "temperature_2m",
        "forecast_days": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
            response = await client.get(settings.open_meteo_forecast_url, params=params)
        if response.status_code != 200:
            return None
        elevation = _valid((response.json() or {}).get("elevation"))
        if elevation is None:
            return None
        await locations_col().update_one(
            {"user_id": int(user_id)},
            {"$set": {
                "elevation_m": elevation,
                "elevation_source": "open-meteo-terrain",
                "elevation_updated_at": datetime.now(timezone.utc),
            }},
        )
        return elevation
    except Exception:
        return None
