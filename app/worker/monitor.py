"""Plane? v3.4 Spotting Intelligence monitoring loop."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.aircraft.ai_judge import ai_judge
from app.aircraft.categories import AIRCRAFT_CATEGORIES, resolve_match_prefixes
from app.aircraft.learner import provider_learner
from app.aircraft.providers import ProviderManager
from app.config import settings
from app.database import get_db, locations_col, preferences_col, system_status_col, users_col
from app.intelligence.camera import recommend_camera
from app.intelligence.lifecycle import (
    advance_cancellation_confirmation,
    decide_lifecycle,
    prediction_changed,
    should_cancel_active_alert,
)
from app.intelligence.celestial import positions as celestial_positions
from app.intelligence.environment import detect_crossing, estimate_atmosphere, estimate_contrail, interpolate_flight_level
from app.intelligence.route_history import route_history_service
from app.intelligence.trajectory import HistorySample, TrajectoryHistoryStore, predict_trajectory
from app.intelligence.upper_air import get_upper_air_profile
from app.photography.conditions import get_current_conditions
from app.photography.solar import get_solar_context
from app.worker.geo import bounding_box, haversine, km_to_nautical_miles, merge_bounding_boxes
from app.worker.notifications import send_or_update_approach
from app.worker.optional_work import enrichment
from app.worker.timing import phase
from app.photography.models import WeatherContext
from app.version import PREDICTION_VERSION
from app.prediction_lab_audit import enqueue_snapshot, enqueue_outcome

logger = logging.getLogger(__name__)
_provider_manager = ProviderManager()
_history = TrajectoryHistoryStore(max_age_s=120, max_samples=64)
_last_cycle_time = 0.0
_last_cycle_duration = 0.0
_total_cycles = 0


def get_provider_manager() -> ProviderManager:
    return _provider_manager


def get_aircraft_history(icao24: str):
    """Expose a copy of recent samples to the photography service."""
    return _history.get(icao24)


async def init_services() -> None:
    ai_judge.initialize()


def get_cycle_stats() -> dict:
    return {
        "last_cycle_time": _last_cycle_time,
        "last_cycle_duration_ms": round(_last_cycle_duration * 1000, 1),
        "total_cycles": _total_cycles,
    }


async def run_monitor_cycle() -> None:
    try:
        await _monitor_cycle()
    except Exception:
        logger.exception("Monitor cycle failed unexpectedly")


async def _record_worker_heartbeat(active_count: int, notifications_sent: int) -> None:
    try:
        await system_status_col().update_one(
            {"_id": "monitor_worker"},
            {"$set": {
                "last_cycle_time": _last_cycle_time,
                "last_cycle_duration_ms": round(_last_cycle_duration * 1000, 1),
                "total_cycles": _total_cycles,
                "active_users": active_count,
                "notifications_sent_last_cycle": notifications_sent,
                "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
    except Exception:
        pass


async def _get_active_users() -> list[dict]:
    cursor = users_col().find({"setup_complete": True}, {"user_id": 1})
    ids = [d["user_id"] async for d in cursor]
    out = []
    for uid in ids:
        loc = await locations_col().find_one({"user_id": uid})
        prefs = await preferences_col().find_one({"user_id": uid})
        if loc and prefs:
            out.append({"user_id": uid, "location": loc, "preferences": prefs})
    return out


def _sample(ac, now: float) -> HistorySample:
    raw = getattr(ac, "timestamp", None)
    age = float(getattr(ac, "position_age_s", 0.0) or 0.0)
    try:
        if raw is not None:
            v = float(raw)
            if v > 1_000_000_000:
                age = max(age, max(0.0, now - v))
            elif 0 <= v < 600:
                age = max(age, v)
    except Exception:
        pass
    return HistorySample(
        timestamp=now - age,
        latitude=float(ac.latitude),
        longitude=float(ac.longitude),
        altitude_m=ac.altitude,
        speed_kts=ac.ground_speed,
        heading_deg=ac.heading,
        vertical_rate_mps=getattr(ac, "vertical_rate_mps", None),
        position_age_s=age,
    )


def _spotting_settings(prefs: dict) -> dict:
    raw = prefs.get("spotting") or {}
    return {
        "mode": str(raw.get("mode") or "Standard aviation"),
        "max_auto_iso": int(raw.get("max_auto_iso") or 1600),
        "approach_alerts": bool(raw.get("approach_alerts", True)),
        "camera_ready_alerts": bool(raw.get("camera_ready_alerts", True)),
        "photo_now_alerts": bool(raw.get("photo_now_alerts", True)),
        "contrail_alerts": bool(raw.get("contrail_alerts", True)),
        "moon_crossing_alerts": bool(raw.get("moon_crossing_alerts", True)),
        "solar_crossing_alerts": bool(raw.get("solar_crossing_alerts", False)),
        "min_confidence": str(raw.get("min_confidence") or "Low"),
    }


def _threshold(label: str) -> float:
    return {"High": .78, "Medium": .58, "Low": .36, "Uncertain": 0}.get(label, .36)


def _watched(prefs: dict) -> set[str]:
    base = set(prefs.get("custom_aircraft", []))
    disabled = set(prefs.get("disabled_types", []))
    for cat in prefs.get("selected_categories", []):
        base.update(t for t in AIRCRAFT_CATEGORIES.get(cat, []) if t not in disabled)
    return resolve_match_prefixes(base)


async def _environment(user: dict, ac, pred, spot: dict) -> dict:
    lat = float(user["location"]["latitude"])
    lon = float(user["location"]["longitude"])
    weather = enrichment.get(("weather", round(lat, 3), round(lon, 3)), lambda: get_current_conditions(lat, lon), ttl=120)
    weather = weather or WeatherContext(timezone="UTC", source_errors=["Weather refresh pending or unavailable"])
    solar = get_solar_context(lat, lon, weather.timezone, subject_latitude=ac.latitude, subject_longitude=ac.longitude)
    atm = estimate_atmosphere(
        temperature_c=weather.temperature_c,
        surface_temperature_c=weather.soil_temperature_0cm_c,
        humidity_pct=weather.relative_humidity_pct,
        visibility_m=weather.visibility_m,
        wind_kmh=weather.wind_speed_kmh,
        solar_wm2=weather.shortwave_radiation_wm2,
        sun_elevation_deg=solar.elevation_deg,
        distance_km=pred.current_distance_km,
        pm25=weather.pm2_5_ugm3,
        aerosol_optical_depth=weather.aerosol_optical_depth,
        precipitation_mm=weather.precipitation_mm,
    )
    contrail = None
    upper_error = None
    if ac.altitude is not None:
        upper = enrichment.get(("upper", round(float(ac.latitude)*4)/4, round(float(ac.longitude)*4)/4),
            lambda: get_upper_air_profile(float(ac.latitude), float(ac.longitude), float(ac.altitude)), ttl=900)
        layers, upper_error = upper or ([], "Upper-air refresh pending or unavailable")
        fl = interpolate_flight_level(layers, float(ac.altitude))
        contrail = estimate_contrail(fl, ac.aircraft_type)
    c = celestial_positions(lat, lon)
    sun_cross = moon_cross = None
    if spot["solar_crossing_alerts"] and c["sun_azimuth_deg"] is not None and c["sun_elevation_deg"] is not None:
        sun_cross = detect_crossing(pred.path, lat, lon, c["sun_azimuth_deg"], c["sun_elevation_deg"], threshold_deg=1.2, solar=True)
    if spot["moon_crossing_alerts"] and c["moon_azimuth_deg"] is not None and c["moon_elevation_deg"] is not None and c["moon_elevation_deg"] > 0:
        moon_cross = detect_crossing(pred.path, lat, lon, c["moon_azimuth_deg"], c["moon_elevation_deg"], threshold_deg=1.0)
    return {
        "weather": weather,
        "solar": solar,
        "atmosphere": atm,
        "contrail": contrail,
        "upper_error": upper_error,
        "sun_crossing": sun_cross,
        "moon_crossing": moon_cross,
    }


async def _camera(user_id: int, ac, pred, user: dict, spot: dict, env: dict | None):
    doc = enrichment.get(("camera", user_id), lambda: get_db()["camera_profiles"].find_one({"user_id": user_id}), ttl=15)
    if not doc or not doc.get("camera"):
        return None
    cam = SimpleNamespace(**doc["camera"])
    lens = SimpleNamespace(**doc["lens"]) if doc.get("lens") else None
    solar = env.get("solar") if env else None
    atmosphere = env.get("atmosphere") if env else None
    return recommend_camera(
        camera=cam,
        lens=lens,
        aircraft_type=ac.aircraft_type,
        prediction=pred,
        observer_lat=float(user["location"]["latitude"]),
        observer_lon=float(user["location"]["longitude"]),
        mode=spot["mode"],
        max_auto_iso=spot["max_auto_iso"],
        light_relationship=getattr(solar, "lighting_relationship", "unknown") if solar else "unknown",
        sun_elevation_deg=getattr(solar, "elevation_deg", None) if solar else None,
        heat_haze_level=getattr(atmosphere, "heat_haze", "Low") if atmosphere else "Low",
    )


async def _monitor_cycle() -> None:
    global _last_cycle_time, _last_cycle_duration, _total_cycles
    start = time.time()
    users = await _get_active_users()
    if not users:
        _last_cycle_time = time.time()
        _last_cycle_duration = time.time() - start
        _total_cycles += 1
        await _record_worker_heartbeat(0, 0)
        return
    regions = defaultdict(list)
    for u in users:
        regions[u["location"]["geohash"]].append(u)
    notifications = 0
    for gh, group in regions.items():
        try:
            notifications += await _process_region(gh, group)
        except Exception:
            logger.exception("Error processing region %s", gh)
    _history.prune()
    _last_cycle_time = time.time()
    _last_cycle_duration = time.time() - start
    _total_cycles += 1
    await _record_worker_heartbeat(len(users), notifications)


def _accepted_latest(sample: HistorySample, history: list[HistorySample]) -> bool:
    if not history:
        return False
    latest = history[-1]
    return (
        abs(latest.timestamp - sample.timestamp) <= 0.2
        and haversine(latest.latitude, latest.longitude, sample.latitude, sample.longitude) <= 0.02
    )


async def _process_region(geohash_key: str, region_users: list[dict]) -> int:
    boxes = []
    for u in region_users:
        r = float(u["location"].get("radius_km", settings.default_radius_km)) + 120.0
        boxes.append(bounding_box(u["location"]["latitude"], u["location"]["longitude"], r))
    merged = merge_bounding_boxes(boxes)
    clat = (merged[0] + merged[1]) / 2
    clon = (merged[2] + merged[3]) / 2
    diag = haversine(merged[0], merged[2], merged[1], merged[3])
    radius_nm = min(250, max(70, int(km_to_nautical_miles(diag / 2) + 15)))
    aircraft, by_provider = await _provider_manager.query_providers(latitude=clat, longitude=clon, radius_nm=radius_nm, provider_names=None)
    if not aircraft:
        return 0

    now = time.time()
    accepted_aircraft = []
    for ac in aircraft:
        if not ac.has_position:
            continue
        s = _sample(ac, now)
        before = _history.get(ac.icao24)
        history = _history.add(ac.icao24, s)
        if _accepted_latest(s, history):
            accepted_aircraft.append(ac)
        else:
            previous = before[-1] if before else None
            jump = haversine(previous.latitude, previous.longitude, s.latitude, s.longitude) if previous else -1.0
            logger.warning(
                "adsb_outlier_rejected icao=%s jump_km=%.2f sample_age=%.1f",
                ac.icao24,
                jump,
                s.position_age_s,
            )

    # Persist a bounded local route trace by flight callsign, not by tail/ICAO24.
    # The service self-throttles samples, so this is cheap on 5-second monitor cycles.
    if accepted_aircraft:
        observations = await asyncio.gather(
            *(route_history_service.observe(ac, now=now) for ac in accepted_aircraft),
            return_exceptions=True,
        )
        failures = sum(isinstance(item, Exception) for item in observations)
        if failures:
            logger.warning("flight_route_observation_failures count=%d", failures)

    for u in region_users:
        await provider_learner.record_cycle_observation(
            user_id=u["user_id"],
            geohash=geohash_key,
            results_by_provider=by_provider,
            user_lat=u["location"]["latitude"],
            user_lon=u["location"]["longitude"],
            radius_km=u["location"].get("radius_km", settings.default_radius_km),
        )
    count = 0
    for u in region_users:
        count += await _match_user_aircraft(u, accepted_aircraft, by_provider)
    return count


async def _match_user_aircraft(user: dict, aircraft_list: list, results_by_provider: dict[str, list]) -> int:
    uid = user["user_id"]
    prefs = user["preferences"]
    loc = user["location"]
    spot = _spotting_settings(prefs)
    prefixes = _watched(prefs)
    if not prefixes:
        return 0
    radius = float(loc.get("radius_km", settings.default_radius_km))
    lat = float(loc["latitude"])
    lon = float(loc["longitude"])
    states = get_db()["approach_states"]
    count = 0
    config_key = hashlib.sha256(json.dumps({"location": {k: loc.get(k) for k in ("latitude", "longitude", "radius_km")},
        "profile": prefs.get("active_profile_id"), "filter": {k: v for k, v in prefs.items() if k not in {"_id", "updated_at", "created_at"}}}, sort_keys=True, default=str).encode()).hexdigest()[:16]

    for ac in aircraft_list:
        if not ac.has_position:
            continue
        typ = (ac.aircraft_type or "").upper().strip()
        if not typ or not any(typ.startswith(p) for p in prefixes):
            continue
        current = haversine(lat, lon, ac.latitude, ac.longitude)
        if current > radius + 120:
            continue
        hist = _history.get(ac.icao24)
        try:
            with phase("prediction"):
                pred = predict_trajectory(hist, lat, lon, radius, now=time.time())
        except Exception:
            logger.exception("cpa_calculation_failed icao=%s user=%s", ac.icao24, uid)
            continue

        logger.debug(
            "cpa icao=%s user=%s state=%s current=%.2f cpa=%.2f t=%.1f confidence=%s",
            ac.icao24,
            uid,
            pred.state,
            pred.current_distance_km,
            pred.projected_closest_km,
            pred.time_to_cpa_s or -1,
            pred.confidence,
        )
        old = await states.find_one({"user_id": uid, "aircraft_icao24": ac.icao24})
        old_time = (old or {}).get("updated_at")
        if old_time and old_time.tzinfo is None:
            old_time = old_time.replace(tzinfo=timezone.utc)
        if old and ((old.get("config_key") and old["config_key"] != config_key)
                    or (old.get("route_callsign") and ac.callsign and old["route_callsign"] != ac.callsign)
                    or (old_time and (datetime.now(timezone.utc) - old_time).total_seconds() > 1800)):
            old = None
        observed_at = max((sample.timestamp for sample in hist), default=0.0)
        fresh_observation = observed_at > float((old or {}).get("last_observation_at") or 0.0) + 0.001

        if old and not old.get("active") and old.get("stage") == "passed":
            passed_at = old.get("updated_at")
            if isinstance(passed_at, datetime):
                if passed_at.tzinfo is None:
                    passed_at = passed_at.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - passed_at < timedelta(minutes=3):
                    continue

        stable_previous_cpa = (old or {}).get("projected_closest_km")
        observed_closest = min(float((old or {}).get("observed_closest_km") or current), float(current))
        changed = prediction_changed(stable_previous_cpa, pred.projected_closest_km, radius)
        active = bool(old and old.get("message_id") and old.get("active"))
        live_qualifies = pred.enters_alert_radius and pred.confidence_score >= _threshold(spot["min_confidence"])

        route_gate = None
        route_suppressed = False
        if live_qualifies or active:
            try:
                route_gate = await route_history_service.evaluate(
                    ac,
                    pred,
                    user_lat=lat,
                    user_lon=lon,
                    alert_radius_km=radius,
                    current_samples=hist,
                    notification_sent=bool((old or {}).get("message_id")),
                )
                route_suppressed = route_gate.suppress_alert
            except Exception:
                # An unavailable gate cannot validate a new predictive alert.
                # Preserve active messages through outages; direct observation wins.
                route_suppressed = not active and pred.current_distance_km > radius
                # Route enrichment must never break the deterministic live CPA loop.
                logger.exception("route_gate_failed callsign=%s icao=%s user=%s", ac.callsign, ac.icao24, uid)

        qualifies = live_qualifies and not route_suppressed
        route_state = {"config_key": config_key, "last_observation_at": observed_at,
                       "prediction_version": PREDICTION_VERSION, "route_callsign": ac.callsign}
        if route_gate is not None:
            route_state.update({
                "route_callsign": route_gate.callsign,
                "route_destination": route_gate.destination_code,
                "route_history_days": route_gate.history_days,
                "route_similar_days": route_gate.similar_days,
                "route_similarity_km": route_gate.similarity_km,
                "route_gate_reason": route_gate.reason,
                "route_expected_turn_pending": route_gate.expected_turn_pending,
                **{key: getattr(route_gate, key, None) for key in ("qualification_state", "terminal_arrival_state", "ensemble_pass_score", "live_pass_score", "expected_turn_state", "airport_path_cpa_km")},
            })

        enqueue_snapshot(user_id=uid, aircraft=ac, prediction=pred, alert_radius_km=radius,
            qualifies=qualifies, route_suppressed=route_suppressed,
            route_reason=route_gate.reason if route_gate else "",
            diagnostics={**route_state, "heading": ac.heading, "speed_mps": ac.velocity,
                "turn_rate_deg_s": pred.turn_rate_deg_s, "sample_count": len(hist),
                "sample_age_s": max(0.0, time.time() - observed_at), "fresh_observation": fresh_observation})

        if route_suppressed:
            logger.info(
                "approach_route_veto user=%s callsign=%s icao=%s destination=%s history_days=%d reason=%s",
                uid,
                route_gate.callsign if route_gate else ac.callsign,
                ac.icao24,
                route_gate.destination_code if route_gate else "",
                route_gate.history_days if route_gate else 0,
                route_gate.reason if route_gate else "route gate",
            )

        if old and old.get("message_id") and old.get("active") and (pred.already_passed or pred.state == "Passed"):
            delivered = await send_or_update_approach(
                uid, ac, pred, "passed", old.get("notification_id", "") or "", old.get("message_id"),
                previous_cpa_km=stable_previous_cpa,
                observed_closest_km=observed_closest,
                prediction_changed=changed,
            )
            if not delivered:
                continue
            await states.update_one(
                {"_id": old["_id"]},
                {"$set": {
                    "active": False,
                    "stage": "passed",
                    "message_id": delivered,
                    "updated_at": datetime.now(timezone.utc),
                    "projected_closest_km": pred.projected_closest_km,
                    "observed_closest_km": observed_closest,
                    "cancel_confirmation_count": 0,
                    **route_state,
                }, "$unset": {"candidate_projected_closest_km": "", "candidate_state": ""}},
            )
            enqueue_outcome(user_id=uid, aircraft=ac, outcome="passed", observed_closest_km=observed_closest, final_prediction=pred)
            logger.info("approach_passed user=%s icao=%s observed_distance=%.2f", uid, ac.icao24, observed_closest)
            continue

        if not qualifies:
            candidate = bool(active and not pred.stale and (route_suppressed or should_cancel_active_alert(pred, stable_previous_cpa, radius)))
            confirmed, confirmation_count = advance_cancellation_confirmation(
                int((old or {}).get("cancel_confirmation_count") or 0), candidate
            )

            if not fresh_observation or pred.stale:
                confirmation_count = int((old or {}).get("cancel_confirmation_count") or 0)
                confirmed = candidate and confirmation_count >= 3
            if active and confirmed:
                delivered = await send_or_update_approach(
                    uid, ac, pred, "cancelled", old.get("notification_id", "") or "", old.get("message_id"),
                    previous_cpa_km=stable_previous_cpa,
                    observed_closest_km=observed_closest,
                    prediction_changed=True,
                )
                if not delivered:
                    continue
                await states.update_one(
                    {"_id": old["_id"]},
                    {"$set": {
                        "active": False,
                        "stage": "cancelled",
                        "message_id": delivered,
                        "updated_at": datetime.now(timezone.utc),
                        "projected_closest_km": pred.projected_closest_km,
                        "observed_closest_km": observed_closest,
                        "cancel_confirmation_count": 0,
                        **route_state,
                    }, "$unset": {"candidate_projected_closest_km": "", "candidate_state": ""}},
                )
                enqueue_outcome(user_id=uid, aircraft=ac, outcome="cancelled", observed_closest_km=observed_closest,
                    final_prediction=pred, previous_projected_closest_km=stable_previous_cpa, route_suppressed=route_suppressed,
                    route_reason=route_gate.reason if route_gate else "")
                logger.info(
                    "approach_alert_cancelled user=%s icao=%s old_cpa=%s new_cpa=%.2f confirmations=%d route_veto=%s",
                    uid,
                    ac.icao24,
                    stable_previous_cpa,
                    pred.projected_closest_km,
                    confirmation_count,
                    route_suppressed,
                )
            elif active:
                await states.update_one(
                    {"_id": old["_id"]},
                    {"$set": {
                        "updated_at": datetime.now(timezone.utc),
                        "observed_closest_km": observed_closest,
                        "cancel_confirmation_count": confirmation_count,
                        "candidate_projected_closest_km": pred.projected_closest_km,
                        "candidate_state": pred.state,
                        "candidate_confidence": pred.confidence,
                        "candidate_time_to_cpa_s": pred.time_to_cpa_s,
                        **route_state,
                    }},
                )
                logger.info(
                    "approach_alert_held user=%s icao=%s stable_cpa=%s candidate_cpa=%.2f state=%s confirmation=%d/3 route_veto=%s",
                    uid,
                    ac.icao24,
                    stable_previous_cpa,
                    pred.projected_closest_km,
                    pred.state,
                    confirmation_count,
                    route_suppressed,
                )
            continue

        env = None
        cam = await _camera(uid, ac, pred, user, spot, None)
        decision = decide_lifecycle(pred, cam.best_window_start_s if cam else None, cam.best_window_end_s if cam else None)
        stage = decision.stage
        if stage in {"prepare", "camera_ready", "photo_now"}:
            try:
                env = await _environment(user, ac, pred, spot)
                cam = await _camera(uid, ac, pred, user, spot, env)
                stage = decide_lifecycle(pred, cam.best_window_start_s if cam else None, cam.best_window_end_s if cam else None).stage
            except Exception:
                logger.exception("spotting_environment_failed user=%s icao=%s", uid, ac.icao24)

        nid = (old or {}).get("notification_id") or str(uuid.uuid4())[:12]
        msgid = (old or {}).get("message_id")
        initial_allowed = (
            stage == "prepare"
            or (stage == "camera_ready" and spot["camera_ready_alerts"])
            or (stage == "photo_now" and spot["photo_now_alerts"])
        )
        should_send = spot["approach_alerts"] and (bool(msgid) or initial_allowed)
        if should_send:
            new_msgid = await send_or_update_approach(
                uid, ac, pred, stage, nid, msgid,
                camera=cam,
                environment=env,
                previous_cpa_km=stable_previous_cpa,
                observed_closest_km=observed_closest,
                prediction_changed=changed,
            )
            if new_msgid and not msgid:
                count += 1
            msgid = new_msgid or msgid

        await states.update_one(
            {"user_id": uid, "aircraft_icao24": ac.icao24},
            {"$set": {
                "user_id": uid,
                "aircraft_icao24": ac.icao24,
                "notification_id": nid,
                "message_id": msgid,
                "stage": stage,
                "active": True,
                "projected_closest_km": pred.projected_closest_km,
                "observed_closest_km": observed_closest,
                "time_to_cpa_s": pred.time_to_cpa_s,
                "prediction_at": datetime.now(timezone.utc),
                "confidence": pred.confidence,
                "cancel_confirmation_count": 0,
                "updated_at": datetime.now(timezone.utc),
                "expires_at": datetime.now(timezone.utc) + timedelta(hours=2),
                **route_state,
            }, "$unset": {
                "candidate_projected_closest_km": "",
                "candidate_state": "",
                "candidate_confidence": "",
                "candidate_time_to_cpa_s": "",
            }},
            upsert=True,
        )
    return count
