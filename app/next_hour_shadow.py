"""Deterministic next-hour spotting expectation-vs-reality shadow audit.

This runs only inside the private AGY parent worker. It never sends alerts and
never influences the production predictor. Expectations are inferred from the
same flight-number's recent route timing (last three UTC days) and are later
resolved against today's actually observed route.

Missing today's route is explicitly *unresolved coverage*, never scored as a
correct prediction or as a miss. Exact observer coordinates never leave this
parent process; the AGY context bridge redacts user identity/location.
"""
from __future__ import annotations

import math
import os
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo import MongoClient

_MONGO_URI = os.getenv("MONGO_URI", "").strip()
_DB_NAME = os.getenv("DATABASE_NAME", "aircraft_bot").strip() or "aircraft_bot"
_client: MongoClient | None = None

HISTORY_DAYS = 3
FORECAST_HORIZON_S = 3600
MIN_WINDOW_HALF_S = 600
MAX_WINDOW_HALF_S = 1800
OUTCOME_GRACE_S = 900
UNRESOLVED_AFTER_S = 2700
OUTCOME_MATCH_PADDING_S = OUTCOME_GRACE_S
LEGACY_OUTCOME_VALIDATION_LIMIT = 500


def _db():
    global _client
    if not _MONGO_URI:
        return None
    if _client is None:
        _client = MongoClient(
            _MONGO_URI,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=8000,
        )
    return _client[_DB_NAME]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * r * math.asin(min(1.0, math.sqrt(a)))


def _point_timestamp(point: dict[str, Any]) -> float | None:
    try:
        ts = float(point.get("t", point.get("timestamp", 0.0)) or 0.0)
    except (TypeError, ValueError):
        return None
    return ts if ts > 0 else None


def _closest_point(points: list[dict[str, Any]], lat: float, lon: float) -> tuple[float, float] | None:
    best: tuple[float, float] | None = None
    for point in points:
        try:
            plat = float(point.get("lat", point.get("latitude")))
            plon = float(point.get("lon", point.get("longitude")))
        except (TypeError, ValueError):
            continue
        ts = _point_timestamp(point)
        if ts is None:
            continue
        distance = _haversine_km(lat, lon, plat, plon)
        if best is None or distance < best[0]:
            best = (distance, ts)
    return best


def _as_utc_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _outcome_match_bounds(expectation: dict[str, Any]) -> tuple[datetime, datetime] | None:
    """Return the bounded observation interval for one shadow expectation.

    The stored expectation window already captures historical timing variance.
    We add the existing outcome grace on both sides so moderately early/late
    passes remain measurable while a different same-day occurrence several
    hours away can never be selected as the actual CPA.
    """
    start = _as_utc_datetime(expectation.get("window_start"))
    end = _as_utc_datetime(expectation.get("window_end"))

    if start is None or end is None:
        # Older expectation records may pre-date explicit window fields. Keep
        # them bounded around predicted CPA rather than falling back to an
        # unsafe whole-day match.
        predicted = _as_utc_datetime(expectation.get("predicted_cpa_at"))
        if predicted is None:
            return None
        start = predicted - timedelta(seconds=MAX_WINDOW_HALF_S)
        end = predicted + timedelta(seconds=MAX_WINDOW_HALF_S)

    if end < start:
        return None
    padding = timedelta(seconds=OUTCOME_MATCH_PADDING_S)
    return start - padding, end + padding


def _closest_point_in_expectation_window(
    points: list[dict[str, Any]],
    lat: float,
    lon: float,
    expectation: dict[str, Any],
) -> tuple[tuple[float, float] | None, int, tuple[datetime, datetime] | None]:
    bounds = _outcome_match_bounds(expectation)
    if bounds is None:
        return None, 0, None
    start, end = bounds
    start_ts = start.timestamp()
    end_ts = end.timestamp()
    bounded = [
        point
        for point in points
        if (ts := _point_timestamp(point)) is not None and start_ts <= ts <= end_ts
    ]
    return _closest_point(bounded, lat, lon), len(bounded), bounds


def _seconds_of_day(ts: float) -> float:
    dt = datetime.fromtimestamp(ts, timezone.utc)
    return dt.hour * 3600.0 + dt.minute * 60.0 + dt.second + dt.microsecond / 1_000_000.0


def _median_time_of_day(values: list[float]) -> tuple[float, float]:
    """Return circular-ish median seconds-of-day and max absolute spread.

    Flights around midnight are normalized by shifting early-day samples by one
    day when the raw span crosses 12 hours.
    """
    if not values:
        raise ValueError("no time samples")
    normalized = list(values)
    if max(normalized) - min(normalized) > 12 * 3600:
        normalized = [value + 86400.0 if value < 12 * 3600 else value for value in normalized]
    median = float(statistics.median(normalized))
    spread = max(abs(value - median) for value in normalized)
    return median % 86400.0, float(spread)


def _confidence(days: int, spread_s: float) -> str:
    if days >= 3 and spread_s <= 600:
        return "High"
    if days >= 2 and spread_s <= 1200:
        return "Medium"
    return "Low"


def _today_at_seconds(now: datetime, seconds: float) -> datetime:
    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return midnight + timedelta(seconds=seconds)


def _expectation_key(user_id: int, callsign: str, utc_date: str) -> str:
    return f"{int(user_id)}:{callsign}:{utc_date}"


def _validate_legacy_outcomes(database, now: datetime) -> int:
    """Quarantine old outcomes that were matched to the wrong same-day leg.

    Old resolver versions searched every point for the callsign on that UTC
    date. Preserve the old values for forensics, but remove them from scoring
    when their selected CPA falls outside the bounded expectation interval.
    """
    audit = database["prediction_lab_audit"]
    cursor = audit.find(
        {
            "kind": "next_hour_outcome",
            "match_validation": {"$exists": False},
            "actual_cpa_at": {"$ne": None},
        },
        {
            "_id": 1,
            "expectation_key": 1,
            "actual_observed": 1,
            "actual_pass": 1,
            "actual_closest_km": 1,
            "actual_cpa_at": 1,
            "timing_error_s": 1,
        },
    ).limit(LEGACY_OUTCOME_VALIDATION_LIMIT)

    quarantined = 0
    for outcome in cursor:
        key = str(outcome.get("expectation_key") or "")
        if not key:
            continue
        expectation = audit.find_one(
            {"kind": "next_hour_expectation", "expectation_key": key},
            {
                "_id": 1,
                "window_start": 1,
                "window_end": 1,
                "predicted_cpa_at": 1,
            },
        )
        if not expectation:
            continue
        bounds = _outcome_match_bounds(expectation)
        actual_at = _as_utc_datetime(outcome.get("actual_cpa_at"))
        if bounds is None or actual_at is None:
            continue
        start, end = bounds
        common = {
            "match_window_start": start,
            "match_window_end": end,
        }
        if start <= actual_at <= end:
            audit.update_one(
                {"_id": outcome["_id"]},
                {"$set": {**common, "match_validation": "bounded_window_legacy_validated"}},
            )
            continue

        audit.update_one(
            {"_id": outcome["_id"]},
            {
                "$set": {
                    **common,
                    "match_validation": "quarantined_wrong_occurrence",
                    "resolution": "unresolved_occurrence_match",
                    "actual_observed": False,
                    "actual_pass": None,
                    "actual_closest_km": None,
                    "actual_cpa_at": None,
                    "timing_error_s": None,
                    "legacy_actual_observed": outcome.get("actual_observed"),
                    "legacy_actual_pass": outcome.get("actual_pass"),
                    "legacy_actual_closest_km": outcome.get("actual_closest_km"),
                    "legacy_actual_cpa_at": actual_at,
                    "legacy_timing_error_s": outcome.get("timing_error_s"),
                    "quarantined_at": now,
                    "note": (
                        "Previously resolved against a same-day route point outside the bounded expectation window; "
                        "excluded from accuracy scoring."
                    ),
                }
            },
        )
        audit.update_one(
            {"_id": expectation["_id"]},
            {
                "$set": {
                    "status": "unresolved_occurrence_match",
                    "resolved_at": now,
                    "note": (
                        "Prior outcome used the wrong same-day occurrence and was quarantined before scoring."
                    ),
                }
            },
        )
        quarantined += 1
    return quarantined


def update_next_hour_shadow(now: datetime | None = None) -> dict[str, int]:
    """Create due next-hour expectations and resolve matured ones.

    Returns counters for logs/health checks. No AI is involved.
    """
    database = _db()
    if database is None:
        return {"created": 0, "resolved": 0, "unresolved": 0, "quarantined": 0}

    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    wanted_days = [(now.date() - timedelta(days=i)).isoformat() for i in range(1, HISTORY_DAYS + 1)]

    users = list(database["users"].find({"setup_complete": True}, {"user_id": 1, "_id": 0}))
    if not users:
        return {"created": 0, "resolved": 0, "unresolved": 0, "quarantined": 0}
    user_ids = [int(doc["user_id"]) for doc in users if doc.get("user_id") is not None]
    locations = {
        int(doc["user_id"]): doc
        for doc in database["locations"].find(
            {"user_id": {"$in": user_ids}},
            {"user_id": 1, "latitude": 1, "longitude": 1, "radius_km": 1, "_id": 0},
        )
    }

    history_docs = list(
        database["flight_route_samples"].find(
            {"utc_date": {"$in": wanted_days}},
            {"callsign": 1, "utc_date": 1, "points": 1, "_id": 0},
        )
    )
    by_callsign: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for doc in history_docs:
        callsign = str(doc.get("callsign") or "").strip().upper()
        if callsign and doc.get("points"):
            by_callsign[callsign].append(doc)

    created = 0
    audit = database["prediction_lab_audit"]
    expires = now + timedelta(days=8)

    for user_id, loc in locations.items():
        try:
            ulat = float(loc["latitude"])
            ulon = float(loc["longitude"])
            radius = float(loc.get("radius_km") or 15.0)
        except (KeyError, TypeError, ValueError):
            continue

        for callsign, routes in by_callsign.items():
            pass_times: list[float] = []
            closest_distances: list[float] = []
            used_days: set[str] = set()
            for route in routes:
                closest = _closest_point(list(route.get("points") or []), ulat, ulon)
                if closest is None:
                    continue
                distance_km, ts = closest
                # A small historical margin prevents one day's sampling jitter
                # at the configured alert-radius edge from dropping the route.
                if distance_km > radius + max(2.0, radius * 0.15):
                    continue
                day = str(route.get("utc_date") or "")
                if day in used_days:
                    continue
                used_days.add(day)
                pass_times.append(_seconds_of_day(ts))
                closest_distances.append(distance_km)

            if not pass_times:
                continue

            predicted_sod, spread_s = _median_time_of_day(pass_times)
            predicted_at = _today_at_seconds(now, predicted_sod)
            horizon_s = (predicted_at - now).total_seconds()
            if horizon_s < 0 or horizon_s > FORECAST_HORIZON_S:
                continue

            half_window = max(MIN_WINDOW_HALF_S, min(MAX_WINDOW_HALF_S, spread_s + 300.0))
            key = _expectation_key(user_id, callsign, today)
            payload = {
                "kind": "next_hour_expectation",
                "expectation_key": key,
                "captured_at": now,
                "expires_at": expires,
                "user_id": user_id,
                "callsign": callsign,
                "utc_date": today,
                "prediction_horizon_s": round(horizon_s, 1),
                "horizon_bucket": "30-60m" if horizon_s > 1800 else ("10-30m" if horizon_s > 600 else "0-10m"),
                "predicted_pass": True,
                "predicted_cpa_at": predicted_at,
                "window_start": predicted_at - timedelta(seconds=half_window),
                "window_end": predicted_at + timedelta(seconds=half_window),
                "predicted_closest_km": round(float(statistics.median(closest_distances)), 3),
                "historical_days": len(pass_times),
                "historical_time_spread_s": round(spread_s, 1),
                "confidence": "Low" if horizon_s > 1800 else _confidence(len(pass_times), spread_s),
                "alert_radius_km": radius,
                "status": "awaiting_outcome",
                "coverage_mode": "historical_flight_number_timing_shadow",
                "note": "Shadow-only. Does not create, cancel, or modify production alerts.",
            }
            result = audit.update_one(
                {"kind": "next_hour_expectation", "expectation_key": key},
                {"$setOnInsert": payload, "$set": {"last_refreshed_at": now}},
                upsert=True,
            )
            if result.upserted_id is not None:
                created += 1

    quarantined = _validate_legacy_outcomes(database, now)
    resolved = 0
    unresolved = 0
    matured = list(
        audit.find(
            {
                "kind": "next_hour_expectation",
                "status": "awaiting_outcome",
                "window_end": {"$lte": now - timedelta(seconds=OUTCOME_GRACE_S)},
            }
        ).limit(250)
    )
    for expectation in matured:
        user_id = int(expectation.get("user_id") or 0)
        loc = locations.get(user_id)
        if not loc:
            continue
        callsign = str(expectation.get("callsign") or "")
        route = database["flight_route_samples"].find_one(
            {"callsign": callsign, "utc_date": str(expectation.get("utc_date") or today)},
            {"points": 1, "_id": 0},
        )
        closest = None
        match_sample_count = 0
        match_bounds = _outcome_match_bounds(expectation)
        if route and route.get("points"):
            try:
                closest, match_sample_count, match_bounds = _closest_point_in_expectation_window(
                    list(route.get("points") or []),
                    float(loc["latitude"]),
                    float(loc["longitude"]),
                    expectation,
                )
            except (KeyError, TypeError, ValueError):
                closest = None
                match_sample_count = 0

        key = str(expectation.get("expectation_key"))
        if closest is not None:
            distance_km, actual_ts = closest
            actual_at = datetime.fromtimestamp(actual_ts, timezone.utc)
            radius = float(expectation.get("alert_radius_km") or loc.get("radius_km") or 15.0)
            from app.forecast_outcomes import observed_outcome, OUTCOME_VERSION
            evidence = observed_outcome(list(route.get("points") or []), float(loc["latitude"]), float(loc["longitude"]), expectation, radius)
            actual_pass = evidence["actual_pass"]
            predicted_at = _as_utc_datetime(expectation.get("predicted_cpa_at"))
            timing_error_s = (actual_at - predicted_at).total_seconds() if predicted_at is not None and evidence["timing_scoreable"] else None
            outcome = {
                "kind": "next_hour_outcome",
                "expectation_key": key,
                "captured_at": now,
                "expires_at": expires,
                "user_id": user_id,
                "callsign": callsign,
                "utc_date": str(expectation.get("utc_date") or today),
                "actual_observed": True,
                "actual_pass": actual_pass,
                "scoreable": evidence["scoreable"],
                "timing_scoreable": evidence["timing_scoreable"],
                "outcome_version": OUTCOME_VERSION,
                "actual_closest_km": round(distance_km, 3),
                "actual_cpa_at": actual_at,
                "timing_error_s": round(timing_error_s, 1) if timing_error_s is not None else None,
                "prediction_horizon_s": expectation.get("prediction_horizon_s"),
                "confidence": expectation.get("confidence"),
                "coverage_mode": "historical_flight_number_timing_shadow",
                "match_validation": "bounded_window",
                "match_sample_count": match_sample_count,
                "match_window_start": match_bounds[0] if match_bounds else None,
                "match_window_end": match_bounds[1] if match_bounds else None,
                "resolution": "resolved_bounded_window" if actual_pass else "unresolved_coverage",
            }
            audit.update_one(
                {"kind": "next_hour_outcome", "expectation_key": key},
                {"$set": outcome},
                upsert=True,
            )
            audit.update_one(
                {"_id": expectation["_id"]},
                {"$set": {"status": "resolved", "resolved_at": now}},
            )
            resolved += 1
            continue

        window_end = _as_utc_datetime(expectation.get("window_end"))
        if window_end is not None and (now - window_end).total_seconds() >= UNRESOLVED_AFTER_S:
            audit.update_one(
                {"kind": "next_hour_outcome", "expectation_key": key},
                {"$set": {
                    "kind": "next_hour_outcome",
                    "expectation_key": key,
                    "captured_at": now,
                    "expires_at": expires,
                    "user_id": user_id,
                    "callsign": callsign,
                    "utc_date": str(expectation.get("utc_date") or today),
                    "actual_observed": False,
                    "actual_pass": None,
                    "resolution": "unresolved_coverage",
                    "note": "No route observation inside the bounded expectation match window; excluded from accuracy scoring.",
                    "coverage_mode": "historical_flight_number_timing_shadow",
                    "match_validation": "bounded_window_no_observation",
                    "match_sample_count": match_sample_count,
                    "match_window_start": match_bounds[0] if match_bounds else None,
                    "match_window_end": match_bounds[1] if match_bounds else None,
                }},
                upsert=True,
            )
            audit.update_one(
                {"_id": expectation["_id"]},
                {"$set": {"status": "unresolved_coverage", "resolved_at": now}},
            )
            unresolved += 1

    return {"created": created, "resolved": resolved, "unresolved": unresolved, "quarantined": quarantined}
