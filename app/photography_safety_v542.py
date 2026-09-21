"""Photography correctness fixes for Plane Alerts v5.4.2.

This module keeps photography optional and outside the alert-critical path. It
reuses the canonical deterministic profile filter for automatic subject choice
and prevents retained notification snapshots from becoming fresh trajectory
observations after time has elapsed.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from telegram.constants import ParseMode
from telegram.ext import ApplicationHandlerStop

from app.aircraft.filtering import compile_filter, normalized_rule_config
from app.config import settings
from app.photography.models import AircraftPhotoContext
from app.version import VERSION
from app.worker.geo import haversine, km_to_nautical_miles

_INSTALLED = False
_ORIGINAL_RECOMMEND = None


def _as_epoch(value: Any) -> float | None:
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if isinstance(value, (int, float)):
        raw = float(value)
        return raw if raw > 1_000_000_000 else None
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            try:
                raw = float(text)
                return raw if raw > 1_000_000_000 else None
            except ValueError:
                return None
    return None


def _snapshot_observation_age(doc: dict[str, Any], *, now: float | None = None) -> float | None:
    now_ts = time.time() if now is None else float(now)
    observed = _as_epoch(doc.get("observed_at"))
    if observed is not None:
        return max(0.0, now_ts - observed)

    captured = None
    for key in ("captured_at", "notified_at", "created_at", "updated_at"):
        captured = _as_epoch(doc.get(key))
        if captured is not None:
            break
    if captured is None:
        return None

    try:
        original_age = max(0.0, float(doc.get("position_age_s") or 0.0))
    except (TypeError, ValueError):
        original_age = 0.0
    return max(0.0, now_ts - captured) + original_age


def _aircraft_from_doc_v542(doc: dict[str, Any]) -> AircraftPhotoContext:
    def num(name: str) -> float | None:
        try:
            return float(doc[name]) if doc.get(name) is not None else None
        except (TypeError, ValueError):
            return None

    return AircraftPhotoContext(
        icao24=str(doc.get("aircraft_icao24") or ""),
        aircraft_type=str(doc.get("aircraft_type") or ""),
        callsign=str(doc.get("callsign") or ""),
        distance_km=num("distance_km"),
        altitude_m=num("altitude_m"),
        speed_ms=num("speed_ms"),
        heading_deg=num("heading_deg"),
        vertical_rate_mps=num("vertical_rate_mps"),
        position_age_s=_snapshot_observation_age(doc),
        latitude=num("latitude"),
        longitude=num("longitude"),
        eta_seconds=num("eta_seconds"),
        live=False,
    )


def _maximum_profile_radius(prefs: dict[str, Any] | None, base_radius_km: float) -> float:
    maximum = float(base_radius_km)
    rules = normalized_rule_config(prefs or {})
    for rule in [rules["profile"], *rules["categories"].values(), *rules["aircraft"].values()]:
        try:
            value = float(rule.get("radius_km")) if rule.get("radius_km") is not None else None
        except (TypeError, ValueError):
            value = None
        if value is not None:
            maximum = max(maximum, value)
    return maximum


async def _find_live_aircraft_v542(
    latitude: float,
    longitude: float,
    radius_km: float,
    *,
    target_icao24: str = "",
    prefs: dict[str, Any] | None = None,
) -> AircraftPhotoContext | None:
    from app.worker.monitor import get_provider_manager

    query_radius_km = _maximum_profile_radius(prefs, radius_km) + 80.0
    radius_nm = min(250, max(60, int(km_to_nautical_miles(query_radius_km))))
    aircraft, _ = await get_provider_manager().query_providers(
        latitude=latitude,
        longitude=longitude,
        radius_nm=radius_nm,
        provider_names=None,
    )
    target = target_icao24.lower().strip()
    compiled = compile_filter(prefs or {}) if not target else None
    candidates: list[tuple[float, Any]] = []

    for ac in aircraft:
        if not ac.has_position:
            continue
        if target:
            if (ac.icao24 or "").lower() != target:
                continue
            allowed_radius = query_radius_km
        else:
            assert compiled is not None
            decision = compiled.evaluate(ac, float(radius_km))
            if not decision.matched:
                continue
            allowed_radius = float(decision.radius_km) + 60.0

        distance = haversine(latitude, longitude, ac.latitude, ac.longitude)
        if target or distance <= allowed_radius:
            candidates.append((distance, ac))

    if not candidates:
        return None
    distance, ac = min(candidates, key=lambda item: item[0])
    return AircraftPhotoContext(
        icao24=ac.icao24 or "",
        aircraft_type=ac.aircraft_type or ac.display_type or "",
        callsign=ac.callsign or "",
        distance_km=round(distance, 2),
        altitude_m=ac.altitude,
        speed_ms=ac.velocity,
        heading_deg=ac.heading,
        vertical_rate_mps=getattr(ac, "vertical_rate_mps", None),
        position_age_s=getattr(ac, "position_age_s", None),
        latitude=ac.latitude,
        longitude=ac.longitude,
        live=True,
    )


def _trajectory_samples_v542(context: Any) -> list[Any]:
    from app.intelligence.trajectory import HistorySample

    ac = context.aircraft
    if not ac or ac.latitude is None or ac.longitude is None:
        return []

    try:
        from app.worker.monitor import get_aircraft_history

        history = get_aircraft_history(ac.icao24)
        if history:
            return history
    except Exception:
        pass

    freshness_limit = max(30.0, float(settings.local_adsb_max_position_age_seconds))
    if not ac.live:
        if ac.position_age_s is None or float(ac.position_age_s) > freshness_limit:
            return []

    now = time.time()
    return [HistorySample(
        now - float(ac.position_age_s or 0.0),
        ac.latitude,
        ac.longitude,
        ac.altitude_m,
        (ac.speed_ms * 1.9438444924406) if ac.speed_ms is not None else None,
        ac.heading_deg,
        ac.vertical_rate_mps,
        float(ac.position_age_s or 0.0),
    )]


def install_photography_safety_v542() -> None:
    global _INSTALLED, _ORIGINAL_RECOMMEND
    if _INSTALLED:
        return

    from app.photography import service
    from app.photography import telegram as photo_telegram

    _ORIGINAL_RECOMMEND = service.recommend_for_user
    service._find_live_aircraft = _find_live_aircraft_v542
    service._aircraft_from_doc = _aircraft_from_doc_v542
    service._trajectory_samples = _trajectory_samples_v542

    async def recommend_v542(user_id: int, *, notification_id: str = ""):
        assert _ORIGINAL_RECOMMEND is not None
        rec, context = await _ORIGINAL_RECOMMEND(user_id, notification_id=notification_id)
        rec.title = f"Plane Alerts v{VERSION} Spotting Intelligence"
        ac = context.aircraft
        freshness_limit = max(30.0, float(settings.local_adsb_max_position_age_seconds))
        if ac and not ac.live and (ac.position_age_s is None or float(ac.position_age_s) > freshness_limit):
            rec.best_timing = (
                "Historical alert context only. Wait for a fresh live position before using a shooting countdown."
            )
            warning = "The retained alert position is not a current observed approach."
            if warning not in rec.warnings:
                rec.warnings.append(warning)
        return rec, context

    async def cmd_photo_help_v542(update: Any, context: Any) -> None:
        del context
        if update.message is None:
            raise ApplicationHandlerStop
        await update.message.reply_text(
            f"✈️ <b>Plane Alerts v{VERSION} — Spotting Intelligence</b>\n\n"
            "<b>Monitoring</b>\n/start — setup\n/status — current config\n/location — update observer location\n/preferences — aircraft filters\n\n"
            "<b>Spotting</b>\n/camera — camera body\n/lens — aircraft lens\n/conditions — measured atmosphere and Sun\n/photo — deterministic live camera setup\n/spotting — modes and alert controls\n\n"
            "Trajectory/CPA, framing, shutter floor, Sun geometry, atmosphere and contrail estimates are deterministic. Gemini is optional and only explains the result.",
            parse_mode=ParseMode.HTML,
        )
        raise ApplicationHandlerStop

    service.recommend_for_user = recommend_v542
    photo_telegram.recommend_for_user = recommend_v542
    photo_telegram.cmd_photo_help = cmd_photo_help_v542
    photo_telegram._CAMERA_PROMPT = photo_telegram._CAMERA_PROMPT.replace("Plane?", "Plane Alerts")
    _INSTALLED = True
