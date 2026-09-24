"""Plane Alerts v5.5 file-backed high-risk Prediction Lab sentinel network.

Sentinels are shadow telemetry only. Fixed European high-risk areas are augmented
with privacy-safe regions centred on every currently delegated admin location.
No sentinel result can create/cancel an alert or modify the deterministic predictor.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.database import connect_db, get_db, system_status_col
from app.intelligence.route_history import normalize_flight_key
from app.prediction_lab_files_v55 import append_evidence, migrate_prediction_lab_mongo, migration_verified, observer_ref, prune_synced, spool_snapshot
from app.worker import monitor
from app.worker.geo import haversine

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = max(20.0, float(os.getenv("SENTINEL_POLL_INTERVAL_SECONDS", "30") or 30.0))
RADIUS_NM = max(55, min(140, int(os.getenv("SENTINEL_RADIUS_NM", "90") or 90)))
PUBLIC_PROVIDERS = ("adsb.lol", "adsb.fi", "airplanes.live", "adsb.one")
ROUTE_TTL_DAYS = 8
MAX_POINTS_PER_ROUTE_DAY = 480
MAX_POINTS_PER_POLL = 80
ADMIN_REFRESH_S = 600.0


@dataclass(frozen=True, slots=True)
class SentinelRegion:
    id: str
    name: str
    latitude: float
    longitude: float
    privacy_scope: str = "public-sentinel"


EUROPE_SENTINELS: tuple[SentinelRegion, ...] = (
    SentinelRegion("istanbul", "Istanbul / Marmara", 41.10, 28.75),
    SentinelRegion("london", "London / South East", 51.35, -0.20),
    SentinelRegion("frankfurt", "Frankfurt / Rhine-Main", 50.05, 8.55),
    SentinelRegion("paris", "Paris / Île-de-France", 49.00, 2.35),
    SentinelRegion("amsterdam", "Amsterdam / Benelux", 52.30, 4.80),
    SentinelRegion("madrid", "Madrid / Central Spain", 40.45, -3.55),
    SentinelRegion("rome", "Rome / Central Italy", 41.85, 12.45),
    SentinelRegion("vienna", "Vienna / Central Europe", 48.15, 16.35),
)

_last_saved: dict[tuple[str, str], tuple[float, float, float]] = {}
_admin_regions: tuple[SentinelRegion, ...] = ()
_admin_regions_refreshed = 0.0


def _sample_point(ac: Any, now: float) -> dict[str, Any]:
    return {
        "t": round(now, 1), "lat": round(float(ac.latitude), 5), "lon": round(float(ac.longitude), 5),
        "altitude_m": getattr(ac, "altitude", None), "heading_deg": getattr(ac, "heading", None),
        "speed_kts": getattr(ac, "ground_speed", None), "vertical_rate_mps": getattr(ac, "vertical_rate_mps", None),
        "position_age_s": getattr(ac, "position_age_s", None), "source": str(getattr(ac, "source", "") or ""),
        "source_freshness": str(getattr(ac, "source_freshness", "") or ""), "data_quality": str(getattr(ac, "data_quality", "") or ""),
        "icao24": str(getattr(ac, "icao24", "") or "").lower(),
    }


def _should_store(region_id: str, callsign: str, point: dict[str, Any]) -> bool:
    key = (region_id, callsign)
    now = float(point["t"]); lat = float(point["lat"]); lon = float(point["lon"])
    previous = _last_saved.get(key)
    if previous is not None:
        last_t, last_lat, last_lon = previous
        if now - last_t < 45.0 and haversine(last_lat, last_lon, lat, lon) < 1.2:
            return False
    _last_saved[key] = (now, lat, lon)
    return True


async def _refresh_admin_regions(now_mono: float) -> tuple[SentinelRegion, ...]:
    global _admin_regions, _admin_regions_refreshed
    if now_mono - _admin_regions_refreshed < ADMIN_REFRESH_S:
        return _admin_regions
    try:
        db = get_db()
        admin_docs = [doc async for doc in db["users"].find({"is_admin": True, "setup_complete": True}, {"user_id": 1, "_id": 0})]
        ids = [int(doc["user_id"]) for doc in admin_docs if doc.get("user_id") is not None]
        if not ids:
            _admin_regions = ()
            _admin_regions_refreshed = now_mono
            return _admin_regions
        locations = [doc async for doc in db["locations"].find({"user_id": {"$in": ids}}, {"user_id": 1, "latitude": 1, "longitude": 1, "_id": 0})]
        regions: list[SentinelRegion] = []
        for loc in locations:
            try:
                user_id = int(loc["user_id"]); lat = float(loc["latitude"]); lon = float(loc["longitude"])
            except (KeyError, TypeError, ValueError):
                continue
            ref = observer_ref(user_id)
            region_id = "admin-" + hashlib.sha256(ref.encode("utf-8")).hexdigest()[:12]
            regions.append(SentinelRegion(region_id, "Admin configured location", lat, lon, "private-admin-sentinel"))
        _admin_regions = tuple(regions)
        _admin_regions_refreshed = now_mono
    except Exception:
        logger.debug("prediction_sentinel_admin_refresh_failed", exc_info=True)
    return _admin_regions


async def _legacy_record_region(region: SentinelRegion, rows: list[tuple[str, dict[str, Any]]], provider_name: str, now: float) -> int:
    if migration_verified() or not rows:
        return 0
    try:
        db = get_db()
    except Exception:
        return 0
    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    expires_at = datetime.now(timezone.utc) + timedelta(days=ROUTE_TTL_DAYS)
    stored = 0
    for callsign, point in rows:
        try:
            await db["prediction_sentinel_routes"].update_one(
                {"sentinel_id": region.id, "callsign": callsign, "utc_date": day},
                {"$set": {"sentinel_id": region.id, "sentinel_name": region.name if region.privacy_scope == "public-sentinel" else "private-admin-sentinel", "callsign": callsign, "utc_date": day, "provider": provider_name, "updated_at": datetime.now(timezone.utc), "expires_at": expires_at}, "$push": {"points": {"$each": [point], "$slice": -MAX_POINTS_PER_ROUTE_DAY}}},
                upsert=True,
            )
            stored += 1
        except Exception:
            logger.debug("sentinel_legacy_store_failed region=%s callsign=%s", region.id, callsign, exc_info=True)
    return stored


async def _record_region(region: SentinelRegion, aircraft: list[Any], provider_name: str, now: float) -> int:
    rows: list[tuple[str, dict[str, Any]]] = []
    for ac in aircraft:
        if not getattr(ac, "has_position", False):
            continue
        callsign = normalize_flight_key(getattr(ac, "callsign", ""))
        if not callsign:
            continue
        try:
            point = _sample_point(ac, now)
        except (TypeError, ValueError):
            continue
        if not _should_store(region.id, callsign, point):
            continue
        rows.append((callsign, point))
        if len(rows) >= MAX_POINTS_PER_POLL:
            break
    if not rows:
        return 0
    captured = datetime.fromtimestamp(now, timezone.utc)
    payload = {
        "kind": "sentinel_poll", "case_id": f"sentinel-{region.id}-{captured.strftime('%Y%m%d%H%M')}", "captured_at": captured,
        "sentinel_id": region.id, "sentinel_name": region.name if region.privacy_scope == "public-sentinel" else "admin-location",
        "privacy_scope": region.privacy_scope, "provider": provider_name, "coverage_missing": False,
        "coverage_mode": "public_adsb_rotating_high_risk_sentinel", "shadow_only": True,
        "points": [{"callsign": callsign, **point} for callsign, point in rows], "route_keys": sorted({callsign for callsign, _ in rows}),
    }
    await append_evidence(payload)
    await _legacy_record_region(region, rows, provider_name, now)
    return len(rows)


async def _ensure_migration() -> bool:
    try:
        state = await migrate_prediction_lab_mongo(get_db())
        verified = bool(state.get("verified")); dropped = bool(state.get("legacy_collections_dropped"))
        logger.info("prediction_lab_migration_state verified=%s dropped=%s", verified, dropped)
        if verified and dropped:
            try:
                await connect_db(ensure_indexes=True)
            except Exception:
                logger.exception("prediction_lab_post_migration_index_retry_failed")
                return False
        return verified and dropped
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("prediction_lab_mongo_migration_failed")
        return False


async def run_sentinel_network() -> None:
    region_cursor = 0; provider_cursor = 0
    await asyncio.sleep(12.0)
    migration_done = await _ensure_migration()
    last_migration_attempt = time.monotonic(); last_spool_cleanup = 0.0
    while True:
        started = time.monotonic()
        if not migration_done and started - last_migration_attempt >= 60.0:
            migration_done = await _ensure_migration(); last_migration_attempt = started
        if started - last_spool_cleanup >= 3600.0:
            removed = await asyncio.to_thread(prune_synced, older_than_hours=24.0, max_files=200)
            if removed:
                logger.info("prediction_lab_spool_cleanup removed=%d", removed)
            last_spool_cleanup = started
        dynamic = await _refresh_admin_regions(started)
        regions = EUROPE_SENTINELS + dynamic
        region = regions[region_cursor % len(regions)]; provider = PUBLIC_PROVIDERS[provider_cursor % len(PUBLIC_PROVIDERS)]
        region_cursor += 1; provider_cursor += 1
        stored = 0; aircraft_count = 0
        try:
            manager = monitor.get_provider_manager()
            aircraft, _ = await manager.query_providers(latitude=region.latitude, longitude=region.longitude, radius_nm=RADIUS_NM, provider_names=[provider])
            aircraft_count = len(aircraft)
            if aircraft:
                stored = await _record_region(region, aircraft, provider, time.time())
            try:
                snap = spool_snapshot()
                await system_status_col().update_one(
                    {"_id": "prediction_lab_sentinels"},
                    {"$set": {"enabled": True, "mode": "shadow-only-file-backed", "regions": len(regions), "public_regions": len(EUROPE_SENTINELS), "admin_regions": len(dynamic), "last_region": region.id, "last_provider": provider, "last_aircraft": aircraft_count, "last_points_stored": stored, "poll_interval_seconds": POLL_INTERVAL_S, "spool_written": snap.get("written", 0), "migration_verified": snap.get("migration_verified", False), "updated_at": datetime.now(timezone.utc)}}, upsert=True,
                )
            except Exception:
                logger.debug("sentinel_status_store_failed", exc_info=True)
            logger.info("prediction_sentinel_poll region=%s provider=%s aircraft=%d stored=%d file_backed=true", region.id, provider, aircraft_count, stored)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("prediction_sentinel_poll_failed region=%s provider=%s", region.id, provider)
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(5.0, POLL_INTERVAL_S - elapsed))
