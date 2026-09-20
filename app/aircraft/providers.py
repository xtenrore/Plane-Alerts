"""Aircraft data providers with v3.4 data-quality normalization."""
from __future__ import annotations

import abc
import asyncio
import logging
import math
import time
from typing import Any

import httpx

from app.aircraft.api_keys import opensky_key_manager
from app.aircraft.models import NormalizedAircraft
from app.config import settings

logger = logging.getLogger(__name__)
_http_client: httpx.AsyncClient | None = None


async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            headers={
                "User-Agent": "Plane-Spotting-Intelligence/3.4",
                "Accept": "application/json, text/plain, */*",
            },
            timeout=httpx.Timeout(connect=3.0, read=4.0, write=3.0, pool=3.0),
            follow_redirects=True,
        )
    return _http_client


async def close_http_client() -> None:
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None


class AircraftDataProvider(abc.ABC):
    name = "base"
    is_unlimited = True

    def __init__(self) -> None:
        self.request_count = 0
        self.error_count = 0
        self.last_request_time = 0.0
        self.last_success_time = 0.0
        self.last_error = ""

    @abc.abstractmethod
    async def get_aircraft_in_area(self, latitude: float, longitude: float, radius_nm: int = 250) -> list[NormalizedAircraft]: ...

    def can_request_now(self) -> bool:
        return True

    def get_status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "is_unlimited": self.is_unlimited,
            "request_count": self.request_count,
            "error_count": self.error_count,
            "last_request_time": self.last_request_time,
            "last_success_time": self.last_success_time,
            "last_error": self.last_error,
            "can_request_now": self.can_request_now(),
        }


class _V2Provider(AircraftDataProvider):
    base_url_setting = ""
    path_style = "point"
    _min_interval = 0.0

    def can_request_now(self) -> bool:
        if not self._min_interval or not self.last_request_time:
            return True
        return time.monotonic() - self.last_request_time >= self._min_interval

    async def get_aircraft_in_area(self, latitude: float, longitude: float, radius_nm: int = 250) -> list[NormalizedAircraft]:
        if self._min_interval and self.last_request_time:
            elapsed = time.monotonic() - self.last_request_time
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
        client = await get_http_client()
        base = str(getattr(settings, self.base_url_setting)).rstrip("/")
        url = (
            f"{base}/lat/{latitude}/lon/{longitude}/dist/{radius_nm}"
            if self.path_style == "latlon"
            else f"{base}/point/{latitude}/{longitude}/{radius_nm}"
        )
        self.last_request_time = time.monotonic() if self._min_interval else time.time()
        self.request_count += 1
        resp = await client.get(url)
        if self.name == "adsb.fi" and resp.status_code == 404:
            resp = await client.get(f"https://opendata.adsb.fi/api/v3/lat/{latitude}/lon/{longitude}/dist/{radius_nm}")
        if self.name == "adsb.one" and resp.status_code in (403, 404, 429):
            self.error_count += 1
            self.last_error = f"HTTP {resp.status_code}"
            return []
        resp.raise_for_status()
        self.last_success_time = time.time()
        return parse_adsb_response(resp.json())


class ADSBLolProvider(_V2Provider):
    name = "adsb.lol"
    base_url_setting = "adsb_lol_base_url"


class ADSBFiProvider(_V2Provider):
    name = "adsb.fi"
    base_url_setting = "adsb_fi_base_url"
    path_style = "latlon"
    _min_interval = 2.0


class AirplanesLiveProvider(_V2Provider):
    name = "airplanes.live"
    base_url_setting = "airplanes_live_base_url"
    _min_interval = 2.0


class ADSBOneProvider(_V2Provider):
    name = "adsb.one"
    base_url_setting = "adsb_one_base_url"
    _min_interval = 2.0


class OpenSkyProvider(AircraftDataProvider):
    name = "opensky"
    is_unlimited = False
    _min_interval = 5.0

    def __init__(self) -> None:
        super().__init__()
        self._last_mono = 0.0

    def can_request_now(self) -> bool:
        if opensky_key_manager.all_exhausted or not opensky_key_manager.has_keys:
            return False
        return not self._last_mono or time.monotonic() - self._last_mono >= self._min_interval

    async def get_aircraft_in_area(self, latitude: float, longitude: float, radius_nm: int = 250) -> list[NormalizedAircraft]:
        token = await opensky_key_manager.get_bearer_token()
        if token is None:
            return []
        if self._last_mono:
            elapsed = time.monotonic() - self._last_mono
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
        degree_offset = radius_nm / 60.0
        longitude_offset = min(180.0, degree_offset / max(0.01, math.cos(math.radians(latitude))))
        params = {
            "lamin": max(-90.0, latitude - degree_offset),
            "lamax": min(90.0, latitude + degree_offset),
            "lomin": max(-180.0, longitude - longitude_offset),
            "lomax": min(180.0, longitude + longitude_offset),
        }
        client = await get_http_client()
        url = f"{settings.opensky_base_url}/states/all"
        headers = {"Authorization": f"Bearer {token}"}
        self._last_mono = time.monotonic()
        self.last_request_time = time.time()
        self.request_count += 1
        try:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code == 401:
                new_token = await opensky_key_manager.refresh_current_token()
                if not new_token:
                    self.error_count += 1
                    self.last_error = "Token refresh failed"
                    return []
                resp = await client.get(url, params=params, headers={"Authorization": f"Bearer {new_token}"})
            if resp.status_code == 429:
                opensky_key_manager.mark_rate_limited()
                self.error_count += 1
                self.last_error = "Rate limited (HTTP 429)"
                return []
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                opensky_key_manager.mark_rate_limited()
            raise
        opensky_key_manager.record_request()
        self.last_success_time = time.time()
        return self._parse(resp.json())

    @staticmethod
    def _parse(data: dict[str, Any]) -> list[NormalizedAircraft]:
        out: list[NormalizedAircraft] = []
        now = time.time()
        for sv in data.get("states") or []:
            if len(sv) < 17:
                continue
            try:
                age = max(0.0, now - float(sv[3])) if sv[3] is not None else None
                out.append(
                    NormalizedAircraft(
                        icao24=(sv[0] or "").lower().strip(),
                        callsign=(sv[1] or "").strip(),
                        origin_country=sv[2] or "",
                        latitude=sv[6],
                        longitude=sv[5],
                        altitude=sv[7],
                        velocity=sv[9],
                        heading=sv[10],
                        vertical_rate_mps=_number(sv[11]),
                        position_age_s=age,
                        data_quality=_quality(age, sv[9], sv[10]),
                        aircraft_type="",
                        timestamp=sv[3],
                    )
                )
            except Exception:
                logger.debug("Skipping malformed OpenSky vector: %s", sv[:4])
        return out

    def get_status(self) -> dict[str, Any]:
        status = super().get_status()
        key_status = opensky_key_manager.get_status()
        status["key_rotation"] = {
            "total_keys": key_status.total_keys,
            "active_key_index": key_status.active_key_index,
            "all_exhausted": key_status.all_exhausted,
            "keys": key_status.keys,
        }
        return status


class ProviderManager:
    def __init__(self) -> None:
        self.adsb_lol = ADSBLolProvider()
        self.adsb_fi = ADSBFiProvider()
        self.opensky = OpenSkyProvider()
        self.airplanes_live = AirplanesLiveProvider()
        self.adsb_one = ADSBOneProvider()
        self._all_providers: list[AircraftDataProvider] = [self.adsb_lol, self.adsb_fi, self.airplanes_live, self.adsb_one]
        self._type_cache: dict[str, str] = {}

    def get_providers_by_names(self, names: list[str] | None = None) -> list[AircraftDataProvider]:
        if names is None:
            return list(self._all_providers)
        all_available = self._all_providers + [self.opensky]
        wanted = set(names)
        return [p for p in all_available if p.name in wanted]

    async def query_providers(self, latitude: float, longitude: float, radius_nm: int = 250, provider_names: list[str] | None = None) -> tuple[list[NormalizedAircraft], dict[str, list[NormalizedAircraft]]]:
        providers = self.get_providers_by_names(provider_names)
        tasks: list[Any] = []
        names: list[str] = []
        for provider in providers:
            if provider.can_request_now():
                tasks.append(self._safe_query(provider, latitude, longitude, radius_nm))
                names.append(provider.name)
        if not tasks:
            logger.warning("No ADS-B providers available for this cycle")
            return [], {}
        results = await asyncio.gather(*tasks)
        by_provider = {name: result for name, result in zip(names, results)}
        merged = self._merge_results(by_provider)
        logger.info(
            "Multi-provider query: %d raw from %d provider(s) -> %d merged unique",
            sum(len(v) for v in by_provider.values()),
            len(by_provider),
            len(merged),
        )
        return merged, by_provider

    async def _safe_query(self, provider: AircraftDataProvider, latitude: float, longitude: float, radius_nm: int) -> list[NormalizedAircraft]:
        max_timeout = max(2.5, min(float(settings.poll_interval_seconds) - 1.0, 4.0))
        try:
            return await asyncio.wait_for(provider.get_aircraft_in_area(latitude, longitude, radius_nm), timeout=max_timeout)
        except asyncio.TimeoutError:
            provider.error_count += 1
            provider.last_error = f"Timeout (>{max_timeout:.1f}s)"
        except httpx.HTTPStatusError as exc:
            provider.error_count += 1
            provider.last_error = f"HTTP {exc.response.status_code}"
        except Exception as exc:
            provider.error_count += 1
            provider.last_error = type(exc).__name__
            logger.warning("Provider %s failed: %s", provider.name, type(exc).__name__)
        return []

    def _merge_results(self, results_by_provider: dict[str, list[NormalizedAircraft]]) -> list[NormalizedAircraft]:
        merged: dict[str, NormalizedAircraft] = {}
        for records in results_by_provider.values():
            for ac in records:
                if ac.icao24 and ac.aircraft_type:
                    self._type_cache[ac.icao24] = ac.aircraft_type.upper()
                    while len(self._type_cache) > 8192:
                        self._type_cache.pop(next(iter(self._type_cache)))
        for records in results_by_provider.values():
            for ac in records:
                if not ac.has_position:
                    continue
                if not ac.aircraft_type and ac.icao24 in self._type_cache:
                    ac = ac.model_copy(update={"aircraft_type": self._type_cache[ac.icao24]})
                existing = merged.get(ac.icao24)
                if existing is None:
                    merged[ac.icao24] = ac
                    continue
                existing_age = existing.position_age_s if existing.position_age_s is not None else 9999.0
                ac_age = ac.position_age_s if ac.position_age_s is not None else 9999.0
                preferred = ac if ac_age < existing_age else existing
                other = existing if preferred is ac else ac
                updates: dict[str, Any] = {}
                if not preferred.aircraft_type and other.aircraft_type:
                    updates["aircraft_type"] = other.aircraft_type
                if not preferred.origin_country and other.origin_country:
                    updates["origin_country"] = other.origin_country
                if preferred.vertical_rate_mps is None and other.vertical_rate_mps is not None:
                    updates["vertical_rate_mps"] = other.vertical_rate_mps
                merged[ac.icao24] = preferred.model_copy(update=updates) if updates else preferred
        return list(merged.values())

    def get_all_provider_status(self) -> list[dict[str, Any]]:
        return [p.get_status() for p in self._all_providers]


def parse_adsb_response(data: dict[str, Any]) -> list[NormalizedAircraft]:
    out: list[NormalizedAircraft] = []
    now = time.time()
    generated_at = _number(data.get("now")) or now
    if generated_at > 10_000_000_000:
        generated_at /= 1000.0
    generated_at = min(now, generated_at)
    for ac in data.get("ac", []):
        try:
            latitude, longitude = _number(ac.get("lat")), _number(ac.get("lon"))
            if latitude is not None and not -90 <= latitude <= 90 or longitude is not None and not -180 <= longitude <= 180:
                continue
            if ac.get("lat") is not None and latitude is None or ac.get("lon") is not None and longitude is None:
                continue
            seen = _number(ac.get("seen_pos"))
            if seen is not None and seen > 1_000_000_000:
                seen = max(0.0, now - seen)
            observed_at = generated_at - max(0.0, seen) if seen is not None else None
            seen = max(0.0, now - observed_at) if observed_at is not None else 999.0
            vertical_fpm = ac.get("baro_rate") if ac.get("baro_rate") is not None else ac.get("geom_rate")
            out.append(
                NormalizedAircraft(
                    icao24=(ac.get("hex") or "").lower().strip(),
                    callsign=(ac.get("flight") or "").strip(),
                    origin_country="",
                    latitude=latitude,
                    longitude=longitude,
                    altitude=_feet_to_metres(ac.get("alt_baro")),
                    velocity=_knots_to_ms(ac.get("gs")),
                    heading=_number(ac.get("track")),
                    turn_rate=_number(ac.get("track_rate")) or 0.0,
                    vertical_rate_mps=_fpm_to_mps(vertical_fpm),
                    position_age_s=seen,
                    data_quality=_quality(seen, ac.get("gs"), ac.get("track")),
                    aircraft_type=(ac.get("t") or "").strip().upper(),
                    timestamp=observed_at,
                )
            )
        except Exception:
            logger.debug("Skipping malformed aircraft record: %s", ac)
    return out


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return None if math.isnan(number) or math.isinf(number) else number
    except (TypeError, ValueError):
        return None


def _feet_to_metres(feet: Any) -> float | None:
    value = _number(feet)
    return None if value is None else round(value * 0.3048, 1)


def _knots_to_ms(knots: Any) -> float | None:
    value = _number(knots)
    return None if value is None else round(value * 0.514444, 1)


def _fpm_to_mps(fpm: Any) -> float | None:
    value = _number(fpm)
    return None if value is None else round(value * 0.00508, 3)


def _quality(age: float | None, speed: Any, heading: Any) -> str:
    if age is None:
        return "unknown"
    fields = sum(v is not None for v in (_number(speed), _number(heading)))
    if age <= 2.5 and fields == 2:
        return "high"
    if age <= 10 and fields >= 1:
        return "medium"
    return "low"
