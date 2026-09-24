"""Deterministic Europe/adversarial Prediction Lab shadow evaluator.

Runs in the parent retired external agent service with MongoDB access. It consumes the low-rate
Europe sentinel route traces collected by the production service and creates two
kinds of evidence:

1. Europe next-hour expectations from the same flight-number's recent timing.
2. Adversarial turn-away traps placed where a straight continuation would look
   threatening but the observed route actually turns away.

Everything is shadow-only. Missing coverage is unresolved, never a hit/miss.
No AI or paid API is used here.
"""
from __future__ import annotations

import hashlib
import math
import os
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo import MongoClient
from app.forecast_outcomes import observed_outcome, OUTCOME_VERSION

_MONGO_URI = os.getenv("MONGO_URI", "").strip()
_DB_NAME = os.getenv("DATABASE_NAME", "aircraft_bot").strip() or "aircraft_bot"
_client: MongoClient | None = None
_indexes_ready = False

HISTORY_DAYS = 3
FORECAST_HORIZON_S = 3600
WINDOW_HALF_S = 900
OUTCOME_GRACE_S = 900
UNRESOLVED_AFTER_S = 2700
ALERT_RADIUS_KM = 15.0
TURN_THRESHOLD_DEG = 14.0
TRAP_FORWARD_KM = 24.0
TRAP_OUTSIDE_MARGIN_KM = 3.0

EUROPE_OBSERVERS: dict[str, tuple[str, float, float]] = {
    "istanbul": ("Istanbul / Marmara", 41.10, 28.75),
    "london": ("London / South East", 51.35, -0.20),
    "frankfurt": ("Frankfurt / Rhine-Main", 50.05, 8.55),
    "paris": ("Paris / Île-de-France", 49.00, 2.35),
    "amsterdam": ("Amsterdam / Benelux", 52.30, 4.80),
    "madrid": ("Madrid / Central Spain", 40.45, -3.55),
    "rome": ("Rome / Central Italy", 41.85, 12.45),
    "vienna": ("Vienna / Central Europe", 48.15, 16.35),
}


def _db():
    global _client, _indexes_ready
    if not _MONGO_URI:
        return None
    if _client is None:
        _client = MongoClient(
            _MONGO_URI,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=8000,
        )
    database = _client[_DB_NAME]
    if not _indexes_ready:
        try:
            database["prediction_lab_audit"].create_index("expires_at", expireAfterSeconds=0)
            database["prediction_lab_audit"].create_index([("kind", 1), ("expectation_key", 1)])
            database["prediction_sentinel_routes"].create_index(
                [("sentinel_id", 1), ("callsign", 1), ("utc_date", 1)], unique=True
            )
            database["prediction_sentinel_routes"].create_index("expires_at", expireAfterSeconds=0)
        except Exception:
            pass
        _indexes_ready = True
    return database


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * r * math.asin(min(1.0, math.sqrt(a)))


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _angle_delta(a: float, b: float) -> float:
    return abs((b - a + 180.0) % 360.0 - 180.0)


def _destination_point(lat: float, lon: float, bearing_deg: float, distance_km: float) -> tuple[float, float]:
    r = 6371.0088
    d = distance_km / r
    brg = math.radians(bearing_deg)
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(brg))
    l2 = l1 + math.atan2(
        math.sin(brg) * math.sin(d) * math.cos(p1),
        math.cos(d) - math.sin(p1) * math.sin(p2),
    )
    return math.degrees(p2), ((math.degrees(l2) + 540.0) % 360.0) - 180.0


def _clean_points(raw: list[dict[str, Any]]) -> list[dict[str, float]]:
    points: list[dict[str, float]] = []
    for item in raw:
        try:
            points.append({
                "t": float(item.get("t") or 0.0),
                "lat": float(item.get("lat")),
                "lon": float(item.get("lon")),
            })
        except (TypeError, ValueError):
            continue
    points = [p for p in points if p["t"] > 0]
    points.sort(key=lambda p: p["t"])
    return points


def _closest(points: list[dict[str, float]], lat: float, lon: float) -> tuple[float, float] | None:
    if not points:
        return None
    best = min(points, key=lambda p: _haversine_km(lat, lon, p["lat"], p["lon"]))
    return _haversine_km(lat, lon, best["lat"], best["lon"]), best["t"]


def _seconds_of_day(ts: float) -> float:
    dt = datetime.fromtimestamp(ts, timezone.utc)
    return dt.hour * 3600.0 + dt.minute * 60.0 + dt.second


def _median_time(values: list[float]) -> tuple[float, float]:
    if max(values) - min(values) > 12 * 3600:
        values = [v + 86400 if v < 12 * 3600 else v for v in values]
    median = float(statistics.median(values))
    spread = max(abs(v - median) for v in values)
    return median % 86400, float(spread)


def _today_at(now: datetime, seconds: float) -> datetime:
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc) + timedelta(seconds=seconds)


def derive_turn_away_traps(points: list[dict[str, Any]]) -> list[dict[str, float]]:
    """Return virtual observers likely to expose straight-line false alerts."""
    clean = _clean_points(points)
    if len(clean) < 5:
        return []
    traps: list[dict[str, float]] = []
    # Sample every interior point, but keep only strong turns with enough route
    # after the turn to prove the aircraft really stayed away from the trap.
    for i in range(2, len(clean) - 2):
        before = clean[i - 2]
        pivot = clean[i]
        after = clean[i + 2]
        if _haversine_km(before["lat"], before["lon"], pivot["lat"], pivot["lon"]) < 3.0:
            continue
        pre_bearing = _bearing_deg(before["lat"], before["lon"], pivot["lat"], pivot["lon"])
        post_bearing = _bearing_deg(pivot["lat"], pivot["lon"], after["lat"], after["lon"])
        turn = _angle_delta(pre_bearing, post_bearing)
        if turn < TURN_THRESHOLD_DEG:
            continue
        trap_lat, trap_lon = _destination_point(pivot["lat"], pivot["lon"], pre_bearing, TRAP_FORWARD_KM)
        future = clean[i:]
        actual = _closest(future, trap_lat, trap_lon)
        if actual is None or actual[0] <= ALERT_RADIUS_KM + TRAP_OUTSIDE_MARGIN_KM:
            continue
        traps.append({
            "turn_index": float(i),
            "turn_angle_deg": round(turn, 1),
            "trap_lat": round(trap_lat, 5),
            "trap_lon": round(trap_lon, 5),
            "actual_closest_km": round(actual[0], 3),
            "pivot_time": pivot["t"],
        })
        if len(traps) >= 3:
            break
    return traps


def _record_adversarial_cases(database: Any, routes: list[dict[str, Any]], now: datetime) -> int:
    written = 0
    expires = now + timedelta(days=8)
    audit = database["prediction_lab_audit"]
    for route in routes:
        callsign = str(route.get("callsign") or "").upper()
        sentinel_id = str(route.get("sentinel_id") or "")
        utc_date = str(route.get("utc_date") or "")
        points = list(route.get("points") or [])
        for trap in derive_turn_away_traps(points):
            fingerprint = (
                f"{sentinel_id}:{callsign}:{utc_date}:"
                f"{trap['turn_index']}:{trap['trap_lat']:.3f}:{trap['trap_lon']:.3f}"
            )
            case_id = hashlib.sha1(fingerprint.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
            doc = {
                "kind": "sentinel_adversarial_turn_away",
                "case_id": case_id,
                "captured_at": now,
                "expires_at": expires,
                "sentinel_id": sentinel_id,
                "sentinel_name": route.get("sentinel_name"),
                "callsign": callsign,
                "utc_date": utc_date,
                "trap_latitude": trap["trap_lat"],
                "trap_longitude": trap["trap_lon"],
                "alert_radius_km": ALERT_RADIUS_KM,
                "straight_continuation_would_hit": True,
                "actual_pass": False,
                "actual_closest_km": trap["actual_closest_km"],
                "turn_angle_deg": trap["turn_angle_deg"],
                "horizon_bucket": "adversarial-replay",
                "status": "resolved_shadow_case",
                "scoreable": True,
                "purpose": "Virtual observer placed beyond a real turn to expose likely straight-line false alerts.",
            }
            result = audit.update_one(
                {"kind": doc["kind"], "case_id": case_id},
                {"$setOnInsert": doc, "$set": {"last_seen_at": now}},
                upsert=True,
            )
            if result.upserted_id is not None:
                written += 1
    return written


def _update_europe_expectations(database: Any, routes: list[dict[str, Any]], now: datetime) -> dict[str, int]:
    today = now.date().isoformat()
    wanted = {(now.date() - timedelta(days=i)).isoformat() for i in range(1, HISTORY_DAYS + 1)}
    by_pair: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for route in routes:
        day = str(route.get("utc_date") or "")
        if day not in wanted:
            continue
        sid = str(route.get("sentinel_id") or "")
        callsign = str(route.get("callsign") or "").upper()
        if sid in EUROPE_OBSERVERS and callsign:
            by_pair[(sid, callsign)].append(route)

    audit = database["prediction_lab_audit"]
    expires = now + timedelta(days=8)
    created = 0
    for (sid, callsign), history in by_pair.items():
        name, lat, lon = EUROPE_OBSERVERS[sid]
        times: list[float] = []
        distances: list[float] = []
        days: set[str] = set()
        for route in history:
            day = str(route.get("utc_date") or "")
            if day in days:
                continue
            closest = _closest(_clean_points(list(route.get("points") or [])), lat, lon)
            if closest is None:
                continue
            days.add(day)
            distances.append(closest[0])
            times.append(_seconds_of_day(closest[1]))
        if not times:
            continue
        sod, spread = _median_time(times)
        predicted_at = _today_at(now, sod)
        horizon_s = (predicted_at - now).total_seconds()
        if horizon_s < 0 or horizon_s > FORECAST_HORIZON_S:
            continue
        half_window = max(WINDOW_HALF_S, min(1800.0, spread + 300.0))
        key = f"sentinel:{sid}:{callsign}:{today}"
        payload = {
            "kind": "sentinel_next_hour_expectation",
            "expectation_key": key,
            "captured_at": now,
            "expires_at": expires,
            "sentinel_id": sid,
            "sentinel_name": name,
            "callsign": callsign,
            "utc_date": today,
            "prediction_horizon_s": round(horizon_s, 1),
            "horizon_bucket": "30-60m" if horizon_s > 1800 else ("10-30m" if horizon_s > 600 else "0-10m"),
            "predicted_cpa_at": predicted_at,
            "window_start": predicted_at - timedelta(seconds=half_window),
            "window_end": predicted_at + timedelta(seconds=half_window),
            "predicted_closest_km": round(float(statistics.median(distances)), 3),
            "historical_days": len(times),
            "historical_time_spread_s": round(spread, 1),
            "confidence": "Low" if horizon_s > 1800 else ("Medium" if len(times) >= 2 and spread <= 1200 else "Low"),
            "alert_radius_km": ALERT_RADIUS_KM,
            "status": "awaiting_outcome",
            "scoreable": False,
            "coverage_mode": "europe-sentinel-shadow",
        }
        result = audit.update_one(
            {"kind": payload["kind"], "expectation_key": key},
            {"$setOnInsert": payload, "$set": {"last_refreshed_at": now}},
            upsert=True,
        )
        if result.upserted_id is not None:
            created += 1

    resolved = 0
    unresolved = 0
    matured = list(audit.find({
        "kind": "sentinel_next_hour_expectation",
        "status": "awaiting_outcome",
        "window_end": {"$lte": now - timedelta(seconds=OUTCOME_GRACE_S)},
    }).limit(250))
    for expectation in matured:
        sid = str(expectation.get("sentinel_id") or "")
        callsign = str(expectation.get("callsign") or "")
        info = EUROPE_OBSERVERS.get(sid)
        if info is None:
            continue
        _, lat, lon = info
        route = database["prediction_sentinel_routes"].find_one(
            {"sentinel_id": sid, "callsign": callsign, "utc_date": str(expectation.get("utc_date") or today)},
            {"points": 1, "_id": 0},
        )
        evidence = observed_outcome(list((route or {}).get("points") or []), lat, lon, expectation,
                                    float(expectation.get("alert_radius_km") or ALERT_RADIUS_KM))
        closest = evidence["closest"]
        key = str(expectation.get("expectation_key") or "")
        if closest is not None:
            distance, actual_ts = closest
            actual_at = datetime.fromtimestamp(actual_ts, timezone.utc)
            predicted_at = expectation.get("predicted_cpa_at")
            timing_error = None
            if isinstance(predicted_at, datetime) and evidence["timing_scoreable"]:
                if predicted_at.tzinfo is None:
                    predicted_at = predicted_at.replace(tzinfo=timezone.utc)
                timing_error = (actual_at - predicted_at).total_seconds()
            audit.update_one(
                {"kind": "sentinel_next_hour_outcome", "expectation_key": key},
                {"$set": {
                    "kind": "sentinel_next_hour_outcome",
                    "expectation_key": key,
                    "captured_at": now,
                    "expires_at": expires,
                    "sentinel_id": sid,
                    "sentinel_name": expectation.get("sentinel_name"),
                    "callsign": callsign,
                    "utc_date": expectation.get("utc_date"),
                    "actual_observed": True,
                    "actual_pass": evidence["actual_pass"],
                    "timing_scoreable": evidence["timing_scoreable"],
                    "outcome_version": OUTCOME_VERSION,
                    "actual_closest_km": round(distance, 3),
                    "actual_cpa_at": actual_at,
                    "timing_error_s": round(timing_error, 1) if timing_error is not None else None,
                    "prediction_horizon_s": expectation.get("prediction_horizon_s"),
                    "coverage_mode": "europe-sentinel-shadow",
                    "scoreable": evidence["scoreable"],
                    "resolution": "observed_pass" if evidence["actual_pass"] else "unresolved_coverage",
                }},
                upsert=True,
            )
            audit.update_one({"_id": expectation["_id"]}, {"$set": {"status": "resolved" if evidence["actual_pass"] else "unresolved_coverage", "resolved_at": now, "scoreable": evidence["scoreable"]}})
            resolved += 1
            continue

        window_end = expectation.get("window_end")
        if isinstance(window_end, datetime):
            if window_end.tzinfo is None:
                window_end = window_end.replace(tzinfo=timezone.utc)
            if (now - window_end).total_seconds() >= UNRESOLVED_AFTER_S:
                audit.update_one(
                    {"kind": "sentinel_next_hour_outcome", "expectation_key": key},
                    {"$set": {
                        "kind": "sentinel_next_hour_outcome",
                        "expectation_key": key,
                        "captured_at": now,
                        "expires_at": expires,
                        "sentinel_id": sid,
                        "callsign": callsign,
                        "actual_observed": False,
                        "resolution": "unresolved_coverage",
                        "scoreable": False,
                        "note": "No sentinel route observation available; excluded from accuracy scoring.",
                        "coverage_mode": "europe-sentinel-shadow",
                    }},
                    upsert=True,
                )
                audit.update_one({"_id": expectation["_id"]}, {"$set": {"status": "unresolved_coverage", "resolved_at": now, "scoreable": False}})
                unresolved += 1

    return {"created": created, "resolved": resolved, "unresolved": unresolved}


def update_sentinel_shadow(now: datetime | None = None) -> dict[str, int]:
    database = _db()
    if database is None:
        return {"adversarial": 0, "created": 0, "resolved": 0, "unresolved": 0}
    now = now or datetime.now(timezone.utc)
    earliest = (now.date() - timedelta(days=HISTORY_DAYS)).isoformat()
    routes = list(database["prediction_sentinel_routes"].find(
        {"utc_date": {"$gte": earliest}},
        {"sentinel_id": 1, "sentinel_name": 1, "callsign": 1, "utc_date": 1, "points": 1, "_id": 0},
    ).limit(1000))
    adversarial = _record_adversarial_cases(database, routes, now)
    forecast = _update_europe_expectations(database, routes, now)
    return {"adversarial": adversarial, **forecast}
