"""Production recovery for ADS-B source handoffs and Next60 route history.

This module is deliberately narrow. It does not change deterministic trajectory,
CPA, ETA, destination authority, alert radius, or confidence thresholds.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.aircraft.providers import ProviderManager
from app.bot.replay_history import build_history_doc, history_day_keys
from app.operational_volume_v561 import route_db_path

logger = logging.getLogger(__name__)

SOURCE_SWITCH_CONFIRMATIONS = 3
SOURCE_GUARD_RETENTION_S = 1800.0
SOURCE_GUARD_MAX_AIRCRAFT = 8192
NEXT60_CORRIDOR_MARGIN_KM = 40.0

_INSTALLED = False


def _position_source(ac: Any) -> str:
    provenance = getattr(ac, "field_provenance", None) or {}
    return str(provenance.get("latitude") or getattr(ac, "source", "") or "").strip().lower()


def _guard_source_switches(manager: Any, merged: list[Any], *, now: float | None = None) -> list[Any]:
    """Hold a replacement public position source until continuity is established.

    The current canonical source remains immediate. A different public source must
    be selected for three consecutive merged observations before becoming
    canonical. Local receiver data remains immediate. This prevents fast
    adsb.lol/adsb.fi oscillation from feeding alternating motion vectors into the
    trajectory engine while still allowing a healthy failover after a short gap.
    """
    current_time = time.time() if now is None else float(now)
    states: dict[str, dict[str, Any]] = getattr(manager, "_position_source_guard_v589", None)
    if states is None:
        states = {}
        setattr(manager, "_position_source_guard_v589", states)

    accepted: list[Any] = []
    for ac in merged:
        key = str(getattr(ac, "icao24", "") or "").lower().strip()
        if not key:
            accepted.append(ac)
            continue
        source = _position_source(ac)
        state = states.get(key)

        if not source:
            accepted.append(ac)
            continue

        if state is None:
            states[key] = {
                "stable_source": source,
                "pending_source": "",
                "pending_count": 0,
                "last_seen": current_time,
            }
            accepted.append(ac)
            continue

        state["last_seen"] = current_time
        stable = str(state.get("stable_source") or "")

        if source == "local":
            state.update(stable_source=source, pending_source="", pending_count=0)
            accepted.append(ac)
            continue

        if not stable or source == stable:
            state.update(stable_source=source, pending_source="", pending_count=0)
            accepted.append(ac)
            continue

        pending_source = str(state.get("pending_source") or "")
        pending_count = int(state.get("pending_count") or 0)
        if pending_source == source:
            pending_count += 1
        else:
            pending_source = source
            pending_count = 1
        state["pending_source"] = pending_source
        state["pending_count"] = pending_count

        if pending_count >= SOURCE_SWITCH_CONFIRMATIONS:
            state.update(stable_source=source, pending_source="", pending_count=0)
            logger.info(
                "adsb_source_switch_confirmed icao=%s previous=%s selected=%s confirmations=%d",
                key,
                stable,
                source,
                pending_count,
            )
            accepted.append(ac)
        else:
            logger.info(
                "adsb_source_switch_held icao=%s stable=%s candidate=%s confirmation=%d/%d",
                key,
                stable,
                source,
                pending_count,
                SOURCE_SWITCH_CONFIRMATIONS,
            )

    cutoff = current_time - SOURCE_GUARD_RETENTION_S
    for key in list(states):
        if float(states[key].get("last_seen") or 0.0) < cutoff:
            states.pop(key, None)
    while len(states) > SOURCE_GUARD_MAX_AIRCRAFT:
        oldest = min(states, key=lambda item: float(states[item].get("last_seen") or 0.0))
        states.pop(oldest, None)

    return accepted


def _install_provider_source_guard() -> None:
    if getattr(ProviderManager, "_plane_source_guard_v589", False):
        return
    original = ProviderManager._merge_results

    def guarded_merge(self: ProviderManager, results_by_provider: dict[str, list[Any]]) -> list[Any]:
        return _guard_source_switches(self, original(self, results_by_provider))

    ProviderManager._merge_results = guarded_merge
    ProviderManager._plane_source_guard_v589 = True


def _volume_route_rows(
    conn: sqlite3.Connection,
    days: list[str],
    limit: int,
    *,
    observer_lat: float | None = None,
    observer_lon: float | None = None,
    corridor_km: float | None = None,
) -> list[tuple[Any, ...]]:
    placeholders = ",".join("?" for _ in days)
    base = (
        "SELECT callsign, utc_date, aircraft_type, points_json "
        f"FROM route_days WHERE utc_date IN ({placeholders}) "
    )
    params: list[Any] = list(days)

    if observer_lat is not None and observer_lon is not None and corridor_km is not None:
        margin = max(1.0, float(corridor_km))
        lat_delta = margin / 111.0
        cos_lat = max(0.05, abs(math.cos(math.radians(float(observer_lat)))))
        lon_delta = min(180.0, margin / (111.0 * cos_lat))
        min_lat = max(-90.0, float(observer_lat) - lat_delta)
        max_lat = min(90.0, float(observer_lat) + lat_delta)
        min_lon = float(observer_lon) - lon_delta
        max_lon = float(observer_lon) + lon_delta
        if -180.0 <= min_lon <= max_lon <= 180.0:
            base += (
                "AND EXISTS ("
                "SELECT 1 FROM json_each(route_days.points_json) AS point "
                "WHERE CAST(COALESCE(json_extract(point.value, '$.lat'), "
                "json_extract(point.value, '$.latitude')) AS REAL) BETWEEN ? AND ? "
                "AND CAST(COALESCE(json_extract(point.value, '$.lon'), "
                "json_extract(point.value, '$.longitude')) AS REAL) BETWEEN ? AND ?"
                ") "
            )
            params.extend((min_lat, max_lat, min_lon, max_lon))

    base += "ORDER BY utc_date DESC, updated_at DESC LIMIT ?"
    params.append(max(1, int(limit)))
    return conn.execute(base, tuple(params)).fetchall()


def _read_volume_routes(
    path: Path,
    days: list[str],
    limit: int,
    *,
    observer_lat: float | None = None,
    observer_lon: float | None = None,
    corridor_km: float | None = None,
) -> list[dict[str, Any]]:
    """Read bounded route-day rows, preferring an observer-area JSON query.

    SQLite builds without JSON1 fall back to the previous date-bounded query.
    The fallback is conservative: replay qualification still rejects routes that
    did not demonstrate a local transit.
    """
    if not days or not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return []
    try:
        try:
            rows = _volume_route_rows(
                conn,
                days,
                limit,
                observer_lat=observer_lat,
                observer_lon=observer_lon,
                corridor_km=corridor_km,
            )
        except sqlite3.Error:
            rows = _volume_route_rows(conn, days, limit)
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    docs: list[dict[str, Any]] = []
    for callsign, utc_date, aircraft_type, points_json in rows:
        try:
            points = json.loads(str(points_json or "[]"))
        except (TypeError, ValueError):
            points = []
        if not isinstance(points, list) or not points:
            continue
        docs.append(
            {
                "callsign": str(callsign or "").strip().upper(),
                "utc_date": str(utc_date or ""),
                "aircraft_type": str(aircraft_type or "").strip().upper(),
                "points": points,
            }
        )
    return docs


async def _history_docs_from_volume(user_id: int, now: datetime) -> list[dict[str, Any]]:
    """Build conservative Next60 history shadows from volume-backed route days."""
    from app.bot import next60

    db = next60.get_db()
    loc = await db["locations"].find_one(
        {"user_id": int(user_id)},
        {"latitude": 1, "longitude": 1, "radius_km": 1, "_id": 0},
    )
    if not loc:
        return []
    try:
        ulat = float(loc["latitude"])
        ulon = float(loc["longitude"])
        radius = float(loc.get("radius_km") or 15.0)
    except (KeyError, TypeError, ValueError):
        return []

    days = history_day_keys(now, int(next60.HISTORY_DAYS))
    routes = await asyncio.to_thread(
        _read_volume_routes,
        route_db_path(),
        days,
        int(next60.MAX_HISTORY_ROUTES),
        observer_lat=ulat,
        observer_lon=ulon,
        corridor_km=radius + NEXT60_CORRIDOR_MARGIN_KM,
    )
    by_callsign: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for route in routes:
        callsign = str(route.get("callsign") or "").strip().upper()
        if callsign and route.get("points"):
            by_callsign[callsign].append(route)

    docs: list[dict[str, Any]] = []
    for callsign, grouped_routes in by_callsign.items():
        doc = build_history_doc(
            callsign,
            grouped_routes,
            ulat,
            ulon,
            radius,
            now,
            max_horizon_s=3600.0,
        )
        if doc is None:
            continue
        docs.append(doc)
        next60._record_next60_evidence(user_id, doc, now)

    docs.sort(key=lambda item: next60._aware(item.get("predicted_cpa_at")) or now)
    return docs[: int(next60.MAX_ROWS)]


async def _live_docs_route_filtered(user_id: int, now: datetime) -> list[dict[str, Any]]:
    """Keep PR #131 live behavior while hiding the monitor's current route veto."""
    from app.bot import next60

    type_cache: dict[str, str] = {}
    try:
        from app.worker.monitor import get_provider_manager

        type_cache = dict(getattr(get_provider_manager(), "_type_cache", {}) or {})
    except Exception:
        type_cache = {}

    cursor = next60.get_db()["approach_states"].find(
        {
            "user_id": user_id,
            "active": True,
            "time_to_cpa_s": {"$gte": 0, "$lte": 3600},
            "prediction_at": {"$gte": now - timedelta(seconds=next60.LIVE_STATE_MAX_AGE_S)},
        },
        {
            "_id": 0,
            "aircraft_icao24": 1,
            "route_callsign": 1,
            "aircraft_type": 1,
            "projected_closest_km": 1,
            "time_to_cpa_s": 1,
            "prediction_at": 1,
            "confidence": 1,
            "stage": 1,
            "candidate_state": 1,
            "route_expected_turn_pending": 1,
        },
    ).limit(next60.MAX_ROWS)
    docs: list[dict[str, Any]] = []
    async for state in cursor:
        # Canonical route_guard.py writes expected_turn_pending == suppress_alert.
        # Reuse that exact live decision instead of trying to reconstruct route
        # geometry from an incomplete persisted state document.
        if state.get("route_expected_turn_pending") is True:
            continue
        doc = next60._live_state_doc(state, now, type_cache)
        if doc is not None:
            docs.append(doc)
    return docs


def _install_next60_recovery() -> bool:
    next60 = sys.modules.get("app.bot.next60")
    if next60 is None:
        return False
    next60._history_docs = _history_docs_from_volume
    next60._live_docs = _live_docs_route_filtered
    setattr(next60, "_volume_history_recovery_v588", True)
    setattr(next60, "_live_route_veto_filter_v588", True)
    return True


def _install_next60_volume_history() -> bool:
    """Compatibility entry point retained for the v5.8.7 recovery regression."""
    return _install_next60_recovery()


def install_runtime_recovery_hotfix() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _install_provider_source_guard()
    patched_next60 = _install_next60_recovery()
    _INSTALLED = True
    logger.info(
        "Runtime recovery enabled provider_switch_confirmations=%d "
        "next60_volume_history=%s next60_route_veto_filter=%s",
        SOURCE_SWITCH_CONFIRMATIONS,
        patched_next60,
        patched_next60,
    )
