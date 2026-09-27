"""Production recovery for ADS-B source handoffs and Next60 route history.

This module is deliberately narrow. It does not change deterministic trajectory,
CPA, ETA, destination authority, alert radius, or confidence thresholds.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.aircraft.providers import ProviderManager
from app.operational_volume_v561 import route_db_path

logger = logging.getLogger(__name__)

SOURCE_SWITCH_CONFIRMATIONS = 3
SOURCE_GUARD_RETENTION_S = 1800.0
SOURCE_GUARD_MAX_AIRCRAFT = 8192

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


def _read_volume_routes(path: Path, days: list[str], limit: int) -> list[dict[str, Any]]:
    """Read bounded route-day rows from the authoritative persistent volume."""
    if not days or not path.exists():
        return []
    placeholders = ",".join("?" for _ in days)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT callsign, utc_date, aircraft_type, points_json "
            f"FROM route_days WHERE utc_date IN ({placeholders}) "
            "ORDER BY utc_date DESC LIMIT ?",
            (*days, max(1, int(limit))),
        ).fetchall()
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
    """Build the existing Next60 history shadow from volume-backed route days."""
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

    days = [
        (now.date() - timedelta(days=i)).isoformat()
        for i in range(1, int(next60.HISTORY_DAYS) + 1)
    ]
    routes = await asyncio.to_thread(
        _read_volume_routes,
        route_db_path(),
        days,
        int(next60.MAX_HISTORY_ROUTES),
    )
    by_callsign: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for route in routes:
        callsign = str(route.get("callsign") or "").strip().upper()
        if callsign and route.get("points"):
            by_callsign[callsign].append(route)

    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    docs: list[dict[str, Any]] = []
    for callsign, grouped_routes in by_callsign.items():
        pass_times: list[float] = []
        closest_distances: list[float] = []
        used_days: set[str] = set()
        aircraft_type = ""
        for route in grouped_routes:
            closest = next60._closest_point(list(route.get("points") or []), ulat, ulon)
            if closest is None:
                continue
            distance_km, timestamp = closest
            if distance_km > radius + max(2.0, radius * 0.15):
                continue
            utc_date = str(route.get("utc_date") or "")
            if not utc_date or utc_date in used_days:
                continue
            used_days.add(utc_date)
            dt = datetime.fromtimestamp(timestamp, timezone.utc)
            pass_times.append(
                dt.hour * 3600.0
                + dt.minute * 60.0
                + dt.second
                + dt.microsecond / 1_000_000.0
            )
            closest_distances.append(float(distance_km))
            aircraft_type = aircraft_type or str(route.get("aircraft_type") or "").strip().upper()

        if not pass_times:
            continue
        predicted_sod, spread_s = next60._median_time_of_day(pass_times)
        predicted_at = midnight + timedelta(seconds=predicted_sod)
        horizon_s = (predicted_at - now).total_seconds()
        if horizon_s < 0 or horizon_s > 3600:
            continue
        half_window = max(600.0, min(1800.0, spread_s + 300.0))
        doc = {
            "callsign": callsign,
            "aircraft_type": aircraft_type,
            "predicted_cpa_at": predicted_at,
            "window_start": predicted_at - timedelta(seconds=half_window),
            "window_end": predicted_at + timedelta(seconds=half_window),
            "prediction_horizon_s": round(horizon_s, 1),
            "predicted_closest_km": round(float(statistics.median(closest_distances)), 3),
            "historical_days": len(pass_times),
            "historical_time_spread_s": round(spread_s, 1),
            "confidence": next60._history_confidence(len(pass_times), spread_s, horizon_s),
            "source": "history",
        }
        docs.append(doc)
        next60._record_next60_evidence(user_id, doc, now)

    docs.sort(key=lambda item: next60._aware(item.get("predicted_cpa_at")) or now)
    return docs[: int(next60.MAX_ROWS)]


def _install_next60_volume_history() -> bool:
    next60 = sys.modules.get("app.bot.next60")
    if next60 is None:
        return False
    next60._history_docs = _history_docs_from_volume
    setattr(next60, "_volume_history_recovery_v589", True)
    return True


def install_runtime_recovery_hotfix() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _install_provider_source_guard()
    patched_next60 = _install_next60_volume_history()
    _INSTALLED = True
    logger.info(
        "Runtime recovery enabled provider_switch_confirmations=%d next60_volume_history=%s",
        SOURCE_SWITCH_CONFIRMATIONS,
        patched_next60,
    )
