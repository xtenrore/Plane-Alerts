"""Resilient ADS-B providers and deterministic multi-provider normalization."""
from __future__ import annotations

import abc
import asyncio
from collections import deque
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
_PUBLIC_PROVIDER_NAMES = ("adsb.lol", "adsb.fi", "airplanes.live", "adsb.one")
_STALE_POSITION_S = 30.0
_DYNAMIC_FIELD_SKEW_S = 8.0
_METADATA_MAX_AGE_S = 120.0


class ProviderRateLimited(RuntimeError):
    def __init__(self, retry_after_s: float | None = None) -> None:
        super().__init__("provider rate limited")
        self.retry_after_s = retry_after_s


class MalformedProviderResponse(RuntimeError):
    pass


async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(headers={"User-Agent": "Plane-Alerts/4.5", "Accept": "application/json, text/plain, */*"}, timeout=httpx.Timeout(connect=2.0, read=2.0, write=2.0, pool=2.0), follow_redirects=True)
    return _http_client


async def close_http_client() -> None:
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(math.ceil(q * len(ordered))) - 1))
    return round(ordered[index], 1)


class AircraftDataProvider(abc.ABC):
    name = "base"
    is_unlimited = True
    _min_interval = 0.0
    failure_threshold = 3
    base_cooldown_s = 10.0
    max_cooldown_s = 120.0
    request_timeout_s = 1.7

    def __init__(self) -> None:
        self.request_count = 0
        self.error_count = 0
        self.timeout_count = 0
        self.rate_limit_count = 0
        self.malformed_response_count = 0
        self.last_request_time = 0.0
        self.last_success_time = 0.0
        self.last_error = ""
        self.last_latency_ms: float | None = None
        self.last_aircraft_count = 0
        self.consecutive_failures = 0
        self.circuit_state = "closed"
        self._last_request_mono = 0.0
        self._cooldown_until_mono = 0.0
        self._latencies_ms: deque[float] = deque(maxlen=64)
        self._recent_requests: deque[bool] = deque(maxlen=64)
        self._recent_position_stale: deque[bool] = deque(maxlen=512)
        self.unknown_freshness_count = 0

    @abc.abstractmethod
    async def get_aircraft_in_area(self, latitude: float, longitude: float, radius_nm: int = 250) -> list[NormalizedAircraft]: ...

    def _health_allows_request(self) -> bool:
        now = time.monotonic()
        if self.circuit_state == "open":
            if now < self._cooldown_until_mono:
                return False
            self.circuit_state = "half_open"
        return True

    def can_request_now(self) -> bool:
        if not self._health_allows_request():
            return False
        if self._min_interval and self._last_request_mono:
            return time.monotonic() - self._last_request_mono >= self._min_interval
        return True

    def _record_request_started(self) -> None:
        self.request_count += 1
        self.last_request_time = time.time()
        self._last_request_mono = time.monotonic()

    def record_success(self, latency_ms: float, aircraft: list[NormalizedAircraft]) -> None:
        self.last_success_time = time.time()
        self.last_error = ""
        self.last_latency_ms = round(latency_ms, 1)
        self.last_aircraft_count = len(aircraft)
        self.consecutive_failures = 0
        self.circuit_state = "closed"
        self._cooldown_until_mono = 0.0
        self._latencies_ms.append(latency_ms)
        self._recent_requests.append(True)
        for ac in aircraft:
            if not ac.has_position:
                continue
            age = _effective_age(ac)
            if math.isfinite(age):
                self._recent_position_stale.append(age > _STALE_POSITION_S)
            else:
                self.unknown_freshness_count += 1

    def record_failure(self, reason: str, *, timeout: bool = False, malformed: bool = False, rate_limited: bool = False, retry_after_s: float | None = None) -> None:
        self.error_count += 1
        self.last_error = reason
        self.consecutive_failures += 1
        self._recent_requests.append(False)
        if timeout:
            self.timeout_count += 1
        if malformed:
            self.malformed_response_count += 1
        if rate_limited:
            self.rate_limit_count += 1
        should_open = rate_limited or self.consecutive_failures >= self.failure_threshold
        if not should_open:
            return
        exponent = max(0, self.consecutive_failures - self.failure_threshold)
        cooldown = min(self.max_cooldown_s, self.base_cooldown_s * (2**exponent))
        if retry_after_s is not None:
            cooldown = max(cooldown, min(self.max_cooldown_s, max(1.0, retry_after_s)))
        self.circuit_state = "open"
        self._cooldown_until_mono = time.monotonic() + cooldown

    @property
    def cooldown_remaining_s(self) -> float:
        return max(0.0, self._cooldown_until_mono - time.monotonic())

    def get_status(self) -> dict[str, Any]:
        stale_rate = sum(self._recent_position_stale) / len(self._recent_position_stale) if self._recent_position_stale else None
        recent_failures = sum(1 for ok in self._recent_requests if not ok)
        return {"name": self.name, "is_unlimited": self.is_unlimited, "request_count": self.request_count, "error_count": self.error_count, "timeout_count": self.timeout_count, "rate_limit_count": self.rate_limit_count, "malformed_response_count": self.malformed_response_count, "last_request_time": self.last_request_time, "last_success_time": self.last_success_time, "last_error": self.last_error, "last_latency_ms": self.last_latency_ms, "latency_p50_ms": _percentile(list(self._latencies_ms), 0.50), "latency_p95_ms": _percentile(list(self._latencies_ms), 0.95), "last_aircraft_count": self.last_aircraft_count, "recent_requests": len(self._recent_requests), "recent_failures": recent_failures, "stale_position_rate": round(stale_rate, 4) if stale_rate is not None else None, "unknown_freshness_count": self.unknown_freshness_count, "consecutive_failures": self.consecutive_failures, "circuit_state": self.circuit_state, "cooldown_remaining_s": round(self.cooldown_remaining_s, 1), "can_request_now": self.can_request_now()}


class _V2Provider(AircraftDataProvider):
    base_url_setting = ""
    path_style = "point"

    async def get_aircraft_in_area(self, latitude: float, longitude: float, radius_nm: int = 250) -> list[NormalizedAircraft]:
        client = await get_http_client()
        base = str(getattr(settings, self.base_url_setting)).rstrip("/")
        url = f"{base}/lat/{latitude}/lon/{longitude}/dist/{radius_nm}" if self.path_style == "latlon" else f"{base}/point/{latitude}/{longitude}/{radius_nm}"
        resp = await client.get(url, timeout=self.request_timeout_s)
        if self.name == "adsb.fi" and resp.status_code == 404:
            resp = await client.get(f"https://opendata.adsb.fi/api/v3/lat/{latitude}/lon/{longitude}/dist/{radius_nm}", timeout=self.request_timeout_s)
        if resp.status_code == 429:
            raise ProviderRateLimited(_retry_after_seconds(resp))
        resp.raise_for_status()
        try:
            data = resp.json()
        except Exception as exc:
            raise MalformedProviderResponse("invalid JSON") from exc
        if not isinstance(data, dict):
            raise MalformedProviderResponse("JSON root is not an object")
        return parse_adsb_response(data, source=self.name)


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


class LocalADSBProvider(AircraftDataProvider):
    name = "local"
    is_unlimited = True
    failure_threshold = 2
    base_cooldown_s = 5.0
    max_cooldown_s = 60.0
    _min_interval = 1.0

    def __init__(self) -> None:
        super().__init__()
        self._resolved_url = ""
        self.request_timeout_s = max(0.2, min(1.5, float(settings.local_adsb_timeout_seconds)))

    @property
    def configured(self) -> bool:
        return bool(str(settings.local_adsb_url or "").strip())

    def can_request_now(self) -> bool:
        return self.configured and super().can_request_now()

    def _candidate_urls(self) -> list[str]:
        configured = str(settings.local_adsb_url or "").strip().rstrip("/")
        if not configured:
            return []
        if configured.lower().endswith(".json"):
            return [configured]
        if self._resolved_url:
            return [self._resolved_url]
        return [f"{configured}/data/aircraft.json", f"{configured}/tar1090/data/aircraft.json", f"{configured}/aircraft.json"]

    async def get_aircraft_in_area(self, latitude: float, longitude: float, radius_nm: int = 250) -> list[NormalizedAircraft]:
        if not self.configured:
            return []
        client = await get_http_client()
        headers: dict[str, str] = {}
        auth = str(settings.local_adsb_auth_header or "").strip()
        if auth:
            headers["Authorization"] = auth
        last_status: int | None = None
        for url in self._candidate_urls():
            resp = await client.get(url, headers=headers, timeout=self.request_timeout_s)
            last_status = resp.status_code
            if resp.status_code == 404 and not str(settings.local_adsb_url).lower().strip().endswith(".json"):
                continue
            if resp.status_code == 429:
                raise ProviderRateLimited(_retry_after_seconds(resp))
            resp.raise_for_status()
            try:
                data = resp.json()
            except Exception as exc:
                raise MalformedProviderResponse("invalid local receiver JSON") from exc
            if not isinstance(data, dict):
                raise MalformedProviderResponse("local receiver JSON root is not an object")
            self._resolved_url = url
            observations = parse_local_receiver_response(data, source=self.name)
            radius_km = max(0.0, float(radius_nm)) * 1.852
            return [
                ac
                for ac in observations
                if ac.has_position
                and _haversine_km(latitude, longitude, float(ac.latitude), float(ac.longitude)) <= radius_km
            ]
        if last_status == 404:
            request = httpx.Request("GET", str(settings.local_adsb_url))
            raise httpx.HTTPStatusError("local receiver aircraft JSON not found", request=request, response=httpx.Response(404, request=request))
        return []

    def get_status(self) -> dict[str, Any]:
        status = super().get_status()
        status.update({"configured": self.configured, "receiver_type": settings.local_adsb_receiver_type, "endpoint_resolved": bool(self._resolved_url)})
        return status


def opensky_bounding_boxes(latitude: float, longitude: float, radius_nm: int | float) -> list[dict[str, float]]:
    lat = float(latitude)
    lon = float(longitude)
    radius = max(0.0, float(radius_nm))
    if not math.isfinite(lat) or not -90.0 <= lat <= 90.0:
        raise ValueError("latitude must be between -90 and 90 degrees")
    if not math.isfinite(lon):
        raise ValueError("longitude must be finite")
    lon = ((lon + 180.0) % 360.0) - 180.0
    lat_offset = min(180.0, radius / 60.0)
    lamin = max(-90.0, lat - lat_offset)
    lamax = min(90.0, lat + lat_offset)
    if lamin <= -90.0 or lamax >= 90.0:
        return [{"lamin": lamin, "lamax": lamax, "lomin": -180.0, "lomax": 180.0}]
    cos_lat = abs(math.cos(math.radians(lat)))
    lon_offset = 180.0 if cos_lat < 1e-6 else min(180.0, lat_offset / cos_lat)
    if lon_offset >= 180.0:
        return [{"lamin": lamin, "lamax": lamax, "lomin": -180.0, "lomax": 180.0}]
    left = lon - lon_offset
    right = lon + lon_offset
    if left >= -180.0 and right <= 180.0:
        return [{"lamin": lamin, "lamax": lamax, "lomin": left, "lomax": right}]
    if right > 180.0:
        return [{"lamin": lamin, "lamax": lamax, "lomin": left, "lomax": 180.0}, {"lamin": lamin, "lamax": lamax, "lomin": -180.0, "lomax": right - 360.0}]
    return [{"lamin": lamin, "lamax": lamax, "lomin": left + 360.0, "lomax": 180.0}, {"lamin": lamin, "lamax": lamax, "lomin": -180.0, "lomax": right}]


class OpenSkyProvider(AircraftDataProvider):
    name = "opensky"
    is_unlimited = False
    _min_interval = 5.0
    base_cooldown_s = 20.0
    max_cooldown_s = 180.0

    def can_request_now(self) -> bool:
        if opensky_key_manager.all_exhausted or not opensky_key_manager.has_keys:
            return False
        return super().can_request_now()

    async def _request_box(self, client: httpx.AsyncClient, url: str, params: dict[str, float], token: str) -> dict[str, Any]:
        resp = await client.get(url, params=params, headers={"Authorization": f"Bearer {token}"}, timeout=self.request_timeout_s)
        if resp.status_code == 401:
            new_token = await opensky_key_manager.refresh_current_token()
            if not new_token:
                raise httpx.HTTPStatusError("OpenSky token refresh failed", request=resp.request, response=resp)
            resp = await client.get(url, params=params, headers={"Authorization": f"Bearer {new_token}"}, timeout=self.request_timeout_s)
        if resp.status_code == 429:
            opensky_key_manager.mark_rate_limited()
            raise ProviderRateLimited(_retry_after_seconds(resp))
        resp.raise_for_status()
        try:
            payload = resp.json()
        except Exception as exc:
            raise MalformedProviderResponse("invalid OpenSky JSON") from exc
        if not isinstance(payload, dict):
            raise MalformedProviderResponse("OpenSky JSON root is not an object")
        return payload

    async def get_aircraft_in_area(self, latitude: float, longitude: float, radius_nm: int = 250) -> list[NormalizedAircraft]:
        token = await opensky_key_manager.get_bearer_token()
        if token is None:
            return []
        client = await get_http_client()
        url = f"{settings.opensky_base_url}/states/all"
        payloads = await asyncio.gather(*(self._request_box(client, url, bbox, token) for bbox in opensky_bounding_boxes(latitude, longitude, radius_nm)))
        for _ in payloads:
            opensky_key_manager.record_request()
        merged: dict[str, NormalizedAircraft] = {}
        for payload in payloads:
            for ac in self._parse(payload):
                current = merged.get(ac.icao24)
                if current is None or _effective_age(ac) < _effective_age(current):
                    merged[ac.icao24] = ac
        return list(merged.values())

    @staticmethod
    def _parse(data: dict[str, Any]) -> list[NormalizedAircraft]:
        out: list[NormalizedAircraft] = []
        received_at = time.time()
        for sv in data.get("states") or []:
            if not isinstance(sv, (list, tuple)) or len(sv) < 17:
                continue
            try:
                latitude = _number(sv[6]); longitude = _number(sv[5])
                if not _valid_coordinates(latitude, longitude, allow_missing=True):
                    continue
                observed_at = _epoch_seconds(sv[3])
                if observed_at is not None and observed_at > received_at + 5.0:
                    observed_at = None
                age = max(0.0, received_at - observed_at) if observed_at is not None else None
                values = {"callsign": sv[1], "origin_country": sv[2], "latitude": latitude, "longitude": longitude, "altitude": sv[7], "velocity": sv[9], "heading": sv[10], "vertical_rate_mps": sv[11]}
                out.append(NormalizedAircraft(icao24=(sv[0] or "").lower().strip(), callsign=(sv[1] or "").strip(), origin_country=sv[2] or "", latitude=latitude, longitude=longitude, altitude=_number(sv[7]), velocity=_number(sv[9]), heading=_number(sv[10]), vertical_rate_mps=_number(sv[11]), position_age_s=age, source_freshness_s=age, data_quality=_quality(age, sv[9], sv[10]), aircraft_type="", timestamp=observed_at, observed_at=observed_at, received_at=received_at, source="opensky", field_provenance=_provenance_for_values("opensky", values), source_candidates=["opensky"]))
            except Exception:
                logger.debug("Skipping malformed OpenSky vector: %s", sv[:4])
        return out

    def get_status(self) -> dict[str, Any]:
        status = super().get_status(); key_status = opensky_key_manager.get_status()
        status["key_rotation"] = {"total_keys": key_status.total_keys, "active_key_index": key_status.active_key_index, "all_exhausted": key_status.all_exhausted, "keys": key_status.keys}
        return status


class ProviderManager:
    def __init__(self) -> None:
        self.local = LocalADSBProvider(); self.adsb_lol = ADSBLolProvider(); self.adsb_fi = ADSBFiProvider(); self.opensky = OpenSkyProvider(); self.airplanes_live = AirplanesLiveProvider(); self.adsb_one = ADSBOneProvider()
        self._public_providers: list[AircraftDataProvider] = [self.adsb_lol, self.adsb_fi, self.airplanes_live, self.adsb_one]
        self._all_providers: list[AircraftDataProvider] = [self.local, *self._public_providers]
        self._provider_by_name = {p.name: p for p in [*self._all_providers, self.opensky]}
        self._type_cache: dict[str, tuple[str, str]] = {}

    def get_providers_by_names(self, names: list[str] | None = None) -> list[AircraftDataProvider]:
        if names is None:
            providers = list(self._all_providers)
        else:
            wanted = set(names)
            providers = [p for p in [*self._all_providers, self.opensky] if p.name in wanted]
            if self.local.configured and any(name in _PUBLIC_PROVIDER_NAMES for name in wanted) and self.local not in providers:
                providers.insert(0, self.local)
        return providers

    async def query_providers(self, latitude: float, longitude: float, radius_nm: int = 250, provider_names: list[str] | None = None) -> tuple[list[NormalizedAircraft], dict[str, list[NormalizedAircraft]]]:
        providers = self.get_providers_by_names(provider_names)
        runnable = [p for p in providers if p.can_request_now()]
        if not runnable:
            logger.warning("adsb_no_provider_available requested=%s", ",".join(provider_names or ["default"]))
            return [], {}
        results = await asyncio.gather(*(self._safe_query(p, latitude, longitude, radius_nm) for p in runnable))
        by_provider = {p.name: result for p, result in zip(runnable, results)}
        merged = self._merge_results(by_provider)
        position_sources: dict[str, int] = {}
        for ac in merged:
            source = str(ac.field_provenance.get("latitude") or ac.source or "unknown")
            position_sources[source] = position_sources.get(source, 0) + 1
        logger.info("adsb_source_selection raw=%d providers=%s merged=%d position_sources=%s", sum(len(v) for v in by_provider.values()), ",".join(by_provider) or "none", len(merged), position_sources)
        return merged, by_provider

    async def _safe_query(self, provider: AircraftDataProvider, latitude: float, longitude: float, radius_nm: int) -> list[NormalizedAircraft]:
        max_timeout = max(0.25, min(float(provider.request_timeout_s), max(0.5, min(float(settings.poll_interval_seconds) - 1.0, 4.0))))
        provider._record_request_started(); started = time.monotonic()
        try:
            result = await asyncio.wait_for(provider.get_aircraft_in_area(latitude, longitude, radius_nm), timeout=max_timeout)
        except asyncio.TimeoutError:
            provider.record_failure(f"Timeout (>{max_timeout:.1f}s)", timeout=True); return []
        except ProviderRateLimited as exc:
            provider.record_failure("Rate limited", rate_limited=True, retry_after_s=exc.retry_after_s); return []
        except MalformedProviderResponse as exc:
            provider.record_failure(str(exc), malformed=True); return []
        except httpx.HTTPStatusError as exc:
            provider.record_failure(f"HTTP {exc.response.status_code}"); return []
        except Exception as exc:
            provider.record_failure(type(exc).__name__); logger.warning("Provider %s failed: %s", provider.name, type(exc).__name__); return []
        provider.record_success((time.monotonic() - started) * 1000.0, result)
        return result

    def _provider_rank(self, ac: NormalizedAircraft) -> tuple[Any, ...]:
        age = _effective_age(ac)
        local_fresh = ac.source == "local" and math.isfinite(age) and age <= float(settings.local_adsb_max_position_age_seconds)
        freshness_bucket = 0 if local_fresh else (1 if age <= _STALE_POSITION_S else 2)
        provider = self._provider_by_name.get(ac.source); failures = provider.consecutive_failures if provider else 0
        priority = {"local": 0, "adsb.lol": 1, "adsb.fi": 2, "airplanes.live": 3, "adsb.one": 4, "opensky": 5}.get(ac.source, 99)
        return freshness_bucket, age, failures, priority, ac.source

    @staticmethod
    def _positions_compatible(a: NormalizedAircraft, b: NormalizedAircraft) -> bool:
        if not a.has_position or not b.has_position:
            return True
        ta, tb = a.observed_at, b.observed_at
        dt = abs(float(ta) - float(tb)) if ta is not None and tb is not None else 0.0
        allowed_km = 2.0 + 0.75 * dt
        return _haversine_km(float(a.latitude), float(a.longitude), float(b.latitude), float(b.longitude)) <= allowed_km

    def _merge_results(self, results_by_provider: dict[str, list[NormalizedAircraft]]) -> list[NormalizedAircraft]:
        grouped: dict[str, list[NormalizedAircraft]] = {}
        for provider_name in sorted(results_by_provider):
            for original in results_by_provider[provider_name]:
                key = (original.icao24 or "").lower().strip()
                if not key:
                    continue
                source = original.source or provider_name
                ac = original if original.source else original.model_copy(update={"source": source, "source_candidates": [source]})
                grouped.setdefault(key, []).append(ac)
                if ac.aircraft_type and ac.aircraft_type != "UNKNOWN":
                    self._type_cache[key] = (ac.aircraft_type.upper(), source)
                    while len(self._type_cache) > 8192:
                        self._type_cache.pop(next(iter(self._type_cache)))
        merged: list[NormalizedAircraft] = []
        for icao24 in sorted(grouped):
            records = grouped[icao24]
            position_candidates = [ac for ac in records if ac.has_position and math.isfinite(_effective_age(ac))]
            if not position_candidates:
                logger.warning("adsb_merge_reject icao=%s reason=no_position_with_known_freshness sources=%s", icao24, ",".join(sorted({ac.source for ac in records})))
                continue
            position_candidates.sort(key=self._provider_rank); primary = position_candidates[0]
            sources = sorted({ac.source for ac in records if ac.source}); notes: list[str] = []
            for candidate in position_candidates[1:]:
                if not self._positions_compatible(primary, candidate):
                    notes.append(f"rejected-position:{candidate.source}:conflict")
                    logger.warning("adsb_merge_reject icao=%s source=%s selected=%s reason=position_conflict selected_age=%.1f rejected_age=%.1f", icao24, candidate.source, primary.source, _effective_age(primary), _effective_age(candidate))
                elif _effective_age(candidate) > _STALE_POSITION_S:
                    notes.append(f"rejected-position:{candidate.source}:stale")
            provenance = dict(primary.field_provenance)
            for field_name in ("latitude", "longitude", "altitude", "velocity", "heading", "vertical_rate_mps", "callsign", "origin_country", "aircraft_type"):
                value = getattr(primary, field_name, None)
                if value not in (None, "", "UNKNOWN"):
                    provenance.setdefault(field_name, primary.source)
            updates: dict[str, Any] = {"source": primary.source, "source_candidates": sources, "field_provenance": provenance, "merge_notes": notes}
            sorted_records = sorted(records, key=self._provider_rank)
            for field_name in ("altitude", "velocity", "heading", "vertical_rate_mps"):
                if getattr(primary, field_name) is not None:
                    continue
                for candidate in sorted_records:
                    value = getattr(candidate, field_name)
                    if value is None or not self._positions_compatible(primary, candidate):
                        continue
                    if primary.observed_at is not None and candidate.observed_at is not None and abs(primary.observed_at - candidate.observed_at) > _DYNAMIC_FIELD_SKEW_S:
                        continue
                    updates[field_name] = value; provenance[field_name] = candidate.source; break
            for field_name in ("callsign", "origin_country", "aircraft_type"):
                if getattr(primary, field_name) not in (None, "", "UNKNOWN"):
                    continue
                for candidate in sorted_records:
                    value = getattr(candidate, field_name)
                    if value in (None, "", "UNKNOWN") or _effective_age(candidate) > _METADATA_MAX_AGE_S:
                        continue
                    updates[field_name] = value; provenance[field_name] = candidate.source; break
            if updates.get("aircraft_type", primary.aircraft_type) in ("", "UNKNOWN") and icao24 in self._type_cache:
                cached_type, cached_source = self._type_cache[icao24]; updates["aircraft_type"] = cached_type; provenance["aircraft_type"] = cached_source
            updates["field_provenance"] = provenance
            merged.append(primary.model_copy(update=updates))
        return merged

    def get_all_provider_status(self) -> list[dict[str, Any]]:
        return [p.get_status() for p in [self.local, *self._public_providers, self.opensky]]


def parse_adsb_response(data: dict[str, Any], *, source: str = "public") -> list[NormalizedAircraft]:
    received_at = time.time(); generated_at = _epoch_seconds(data.get("now"))
    if generated_at is None or generated_at > received_at + 5.0:
        generated_at = received_at
    records = data.get("ac") if data.get("ac") is not None else data.get("aircraft")
    return _parse_readsb_records(records or [], reference_time=generated_at, received_at=received_at, source=source)


def parse_local_receiver_response(data: dict[str, Any], *, source: str = "local") -> list[NormalizedAircraft]:
    received_at = time.time(); generated_at = _epoch_seconds(data.get("now"))
    if generated_at is None or generated_at > received_at + 5.0:
        generated_at = received_at
    records = data.get("aircraft") if data.get("aircraft") is not None else data.get("ac")
    if not isinstance(records, list):
        raise MalformedProviderResponse("local receiver payload has no aircraft list")
    return _parse_readsb_records(records, reference_time=generated_at, received_at=received_at, source=source)


def _parse_readsb_records(records: list[Any], *, reference_time: float, received_at: float, source: str) -> list[NormalizedAircraft]:
    out: list[NormalizedAircraft] = []
    for raw in records:
        if not isinstance(raw, dict):
            continue
        try:
            latitude, longitude = _number(raw.get("lat")), _number(raw.get("lon"))
            if not _valid_coordinates(latitude, longitude, allow_missing=True):
                continue
            if (raw.get("lat") is not None and latitude is None) or (raw.get("lon") is not None and longitude is None):
                continue
            seen_pos = _number(raw.get("seen_pos")); observed_at: float | None = None
            if seen_pos is not None:
                if seen_pos > 1_000_000_000:
                    observed_at = _epoch_seconds(seen_pos)
                elif seen_pos >= 0:
                    observed_at = reference_time - seen_pos
            age = max(0.0, received_at - observed_at) if observed_at is not None else None
            vertical_fpm = raw.get("baro_rate") if raw.get("baro_rate") is not None else raw.get("geom_rate")
            aircraft_type = str(raw.get("t") or "").strip().upper()
            values = {"callsign": raw.get("flight"), "latitude": latitude, "longitude": longitude, "altitude": raw.get("alt_baro"), "velocity": raw.get("gs"), "heading": raw.get("track"), "vertical_rate_mps": vertical_fpm, "aircraft_type": aircraft_type}
            out.append(NormalizedAircraft(icao24=str(raw.get("hex") or "").lower().strip(), callsign=str(raw.get("flight") or "").strip(), origin_country="", latitude=latitude, longitude=longitude, altitude=_feet_to_metres(raw.get("alt_baro")), velocity=_knots_to_ms(raw.get("gs")), heading=_number(raw.get("track")), turn_rate=_number(raw.get("track_rate")) or 0.0, vertical_rate_mps=_fpm_to_mps(vertical_fpm), position_age_s=age, source_freshness_s=age, data_quality=_quality(age, raw.get("gs"), raw.get("track")), aircraft_type=aircraft_type, timestamp=observed_at, observed_at=observed_at, received_at=received_at, source=source, field_provenance=_provenance_for_values(source, values), source_candidates=[source]))
        except Exception:
            logger.debug("Skipping malformed aircraft record: %s", raw)
    return out


def _provenance_for_values(source: str, values: dict[str, Any]) -> dict[str, str]:
    return {name: source for name, value in values.items() if value not in (None, "", "UNKNOWN")}


def _effective_age(ac: NormalizedAircraft) -> float:
    if ac.position_age_s is not None:
        try:
            return max(0.0, float(ac.position_age_s))
        except (TypeError, ValueError):
            pass
    if ac.observed_at is not None and ac.received_at is not None:
        try:
            return max(0.0, float(ac.received_at) - float(ac.observed_at))
        except (TypeError, ValueError):
            pass
    return math.inf


def _valid_coordinates(latitude: float | None, longitude: float | None, *, allow_missing: bool) -> bool:
    if latitude is None or longitude is None:
        return allow_missing
    return -90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0


def _epoch_seconds(value: Any) -> float | None:
    number = _number(value)
    if number is None:
        return None
    if number > 10_000_000_000:
        number /= 1000.0
    return number if number > 0 else None


def _retry_after_seconds(response: httpx.Response) -> float | None:
    return _number(response.headers.get("Retry-After"))


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return None if math.isnan(number) or math.isinf(number) else number
    except (TypeError, ValueError):
        return None


def _feet_to_metres(feet: Any) -> float | None:
    value = _number(feet); return None if value is None else round(value * 0.3048, 1)


def _knots_to_ms(knots: Any) -> float | None:
    value = _number(knots); return None if value is None else round(value * 0.514444, 1)


def _fpm_to_mps(fpm: Any) -> float | None:
    value = _number(fpm); return None if value is None else round(value * 0.00508, 2)


def _quality(age: float | None, speed: Any, heading: Any) -> str:
    if age is None: return "unknown"
    if age <= 5 and _number(speed) is not None and _number(heading) is not None: return "high"
    if age <= 15: return "medium"
    if age <= 30: return "low"
    return "stale"


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2); dp = p2 - p1; dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0088 * 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))