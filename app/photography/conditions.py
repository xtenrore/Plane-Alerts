"""Current photographic conditions from Open-Meteo weather and air-quality APIs."""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings
from app.photography.models import WeatherContext


@dataclass
class _CacheEntry:
    expires_at: float
    value: WeatherContext


_CACHE: dict[tuple[float, float], _CacheEntry] = {}
_CACHE_LOCK = asyncio.Lock()


def _number(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return None
        return number
    except (TypeError, ValueError):
        return None


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _heat_haze_signal(values: dict[str, float | None]) -> tuple[int, list[str]]:
    """Produce a transparent heat-haze signal, not a camera-setting decision."""
    temp = values.get("temperature_c")
    soil = values.get("soil_temperature_0cm_c")
    solar = values.get("shortwave_radiation_wm2")
    wind = values.get("wind_speed_kmh")
    visibility = values.get("visibility_m")
    score = 0.0
    factors: list[str] = []

    if temp is not None:
        score += 20.0 * _clamp((temp - 18.0) / 18.0)
        if temp >= 28:
            factors.append(f"warm air {temp:.1f}°C")
    if soil is not None and temp is not None:
        delta = soil - temp
        score += 30.0 * _clamp(delta / 12.0)
        if delta >= 4:
            factors.append(f"ground ~{delta:.1f}°C warmer than air")
    if solar is not None:
        score += 25.0 * _clamp(solar / 900.0)
        if solar >= 600:
            factors.append(f"strong solar heating {solar:.0f} W/m²")
    if wind is not None:
        score += 15.0 * (1.0 - _clamp(wind / 25.0))
        if wind <= 7:
            factors.append(f"light wind {wind:.1f} km/h")
    if visibility is not None and visibility < 15000:
        score += 10.0 * _clamp((15000.0 - visibility) / 15000.0)
        factors.append(f"visibility {visibility / 1000:.1f} km")
    return int(round(_clamp(score, 0.0, 100.0))), factors


async def get_current_conditions(latitude: float, longitude: float) -> WeatherContext:
    """Fetch current weather + atmospheric clarity, with a short per-location cache."""
    key = (round(latitude, 3), round(longitude, 3))
    now_mono = time.monotonic()
    async with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached.expires_at > now_mono:
            return cached.value.model_copy(deep=True)

    weather_params = {
        "latitude": latitude,
        "longitude": longitude,
        "timezone": "auto",
        "current": ",".join([
            "temperature_2m", "relative_humidity_2m", "dew_point_2m",
            "apparent_temperature", "precipitation", "cloud_cover", "visibility",
            "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
            "surface_pressure", "shortwave_radiation", "direct_normal_irradiance",
            "diffuse_radiation", "soil_temperature_0cm",
        ]),
    }
    air_params = {
        "latitude": latitude,
        "longitude": longitude,
        "timezone": "auto",
        "current": "aerosol_optical_depth,dust,pm2_5,pm10,uv_index",
    }
    weather_data: dict[str, Any] = {}
    air_data: dict[str, Any] = {}
    errors: list[str] = []
    sources_ok: list[str] = []

    timeout = httpx.Timeout(settings.photography_http_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        weather_response, air_response = await asyncio.gather(
            client.get(settings.open_meteo_forecast_url, params=weather_params),
            client.get(settings.open_meteo_air_quality_url, params=air_params),
            return_exceptions=True,
        )
        if not isinstance(weather_response, Exception) and weather_response.status_code == 400:
            reduced = dict(weather_params)
            reduced["current"] = ",".join(
                v for v in str(weather_params["current"]).split(",")
                if v != "soil_temperature_0cm"
            )
            try:
                weather_response = await client.get(settings.open_meteo_forecast_url, params=reduced)
                if weather_response.status_code == 200:
                    errors.append("weather model did not expose ground temperature; continued without it")
            except Exception as exc:
                weather_response = exc

    if isinstance(weather_response, Exception):
        errors.append(f"weather unavailable: {type(weather_response).__name__}")
    elif weather_response.status_code == 200:
        weather_data = weather_response.json()
        sources_ok.append("Open-Meteo Weather")
    else:
        errors.append(f"weather HTTP {weather_response.status_code}")
    if isinstance(air_response, Exception):
        errors.append(f"air quality unavailable: {type(air_response).__name__}")
    elif air_response.status_code == 200:
        air_data = air_response.json()
        sources_ok.append("Open-Meteo Air Quality")
    else:
        errors.append(f"air quality HTTP {air_response.status_code}")

    current = weather_data.get("current") or {}
    air_current = air_data.get("current") or {}
    timezone_name = str(weather_data.get("timezone") or air_data.get("timezone") or "UTC")
    timestamp = str(current.get("time") or air_current.get("time") or "")
    mapped: dict[str, float | None] = {
        "temperature_c": _number(current.get("temperature_2m")),
        "apparent_temperature_c": _number(current.get("apparent_temperature")),
        "relative_humidity_pct": _number(current.get("relative_humidity_2m")),
        "dew_point_c": _number(current.get("dew_point_2m")),
        "precipitation_mm": _number(current.get("precipitation")),
        "cloud_cover_pct": _number(current.get("cloud_cover")),
        "visibility_m": _number(current.get("visibility")),
        "wind_speed_kmh": _number(current.get("wind_speed_10m")),
        "wind_direction_deg": _number(current.get("wind_direction_10m")),
        "wind_gusts_kmh": _number(current.get("wind_gusts_10m")),
        "surface_pressure_hpa": _number(current.get("surface_pressure")),
        "shortwave_radiation_wm2": _number(current.get("shortwave_radiation")),
        "direct_normal_irradiance_wm2": _number(current.get("direct_normal_irradiance")),
        "diffuse_radiation_wm2": _number(current.get("diffuse_radiation")),
        "soil_temperature_0cm_c": _number(current.get("soil_temperature_0cm")),
        "aerosol_optical_depth": _number(air_current.get("aerosol_optical_depth")),
        "dust_ugm3": _number(air_current.get("dust")),
        "pm2_5_ugm3": _number(air_current.get("pm2_5")),
        "pm10_ugm3": _number(air_current.get("pm10")),
        "uv_index": _number(air_current.get("uv_index")),
    }
    haze_score, haze_factors = _heat_haze_signal(mapped)
    result = WeatherContext(
        timestamp=timestamp,
        timezone=timezone_name,
        heat_haze_signal=haze_score,
        heat_haze_factors=haze_factors,
        sources_ok=sources_ok,
        source_errors=errors,
        **mapped,
    )
    ttl = settings.photography_conditions_cache_seconds if sources_ok else 15
    async with _CACHE_LOCK:
        _CACHE[key] = _CacheEntry(expires_at=time.monotonic() + ttl, value=result)
        while len(_CACHE) > 256:
            _CACHE.pop(next(iter(_CACHE)))
    return result.model_copy(deep=True)
