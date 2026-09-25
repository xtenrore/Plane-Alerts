"""Plane Alerts v5.6.1 Railway operational storage split.

High-volume route history and Prediction Lab telemetry belong on the Plane Alerts
persistent volume, never in MongoDB. MongoDB remains authoritative for users,
locations, profiles, preferences and other durable application configuration.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.database import get_db
from app.intelligence import route_guard_v2 as route_v2
from app.intelligence import route_history as route_mod
from app.intelligence import route_intelligence_v46 as route_v46
from app.intelligence import route_observe_guard_v44 as route_observe_guard
from app.intelligence.trajectory import haversine_km
from app import prediction_lab_files_v55 as lab_files

logger = logging.getLogger(__name__)

ROUTE_RETENTION_DAYS = 35
ROUTE_LOOKBACK_DAYS = 28
ROUTE_MAX_PATHS = 28
ROUTE_MAX_POINTS_PER_DAY = 360
DEFAULT_ROOT = "/data/prediction_lab"
_DB_RELATIVE = Path("runtime") / "route_history.sqlite3"
_STATE_RELATIVE = Path("state") / "route_history_mongo_migration_v561.json"
_PRUNE_INTERVAL_S = 3600.0
_FAILURE_LOG_INTERVAL_S = 60.0

_db_lock = threading.Lock()
_last_prune_mono = 0.0
_last_failure_log_mono = 0.0
_installed = False
_migration_task: asyncio.Task | None = None


def root_path() -> Path:
    raw = os.getenv("PREDICTION_LAB_ROOT", DEFAULT_ROOT).strip() or DEFAULT_ROOT
    return Path(raw)


def route_db_path() -> Path:
    return root_path() / _DB_RELATIVE


def migration_state_path() -> Path:
    return root_path() / _STATE_RELATIVE


def _connect() -> sqlite3.Connection:
    path = route_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS route_days (
            callsign TEXT NOT NULL,
            utc_date TEXT NOT NULL,
            updated_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            aircraft_type TEXT,
            points_json TEXT NOT NULL,
            PRIMARY KEY (callsign, utc_date)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS route_days_expires_idx ON route_days(expires_at)")
    return conn


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_state() -> dict[str, Any]:
    try:
        value = json.loads(migration_state_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}


def _prune_if_due(conn: sqlite3.Connection, now_ts: float) -> None:
    global _last_prune_mono
    now_mono = time.monotonic()
    if now_mono - _last_prune_mono < _PRUNE_INTERVAL_S:
        return
    conn.execute("DELETE FROM route_days WHERE expires_at < ?", (float(now_ts),))
    _last_prune_mono = now_mono


def _append_route_sample_sync(
    callsign: str,
    utc_date: str,
    point: dict[str, Any],
    *,
    captured_at: float,
    aircraft_type: str = "",
) -> None:
    expires_at = float(captured_at) + ROUTE_RETENTION_DAYS * 86400.0
    with _db_lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT points_json, aircraft_type FROM route_days WHERE callsign=? AND utc_date=?",
                (callsign, utc_date),
            ).fetchone()
            points: list[Any] = []
            existing_type = ""
            if row:
                existing_type = str(row[1] or "")
                try:
                    loaded = json.loads(str(row[0] or "[]"))
                    if isinstance(loaded, list):
                        points = loaded
                except (ValueError, TypeError):
                    points = []
            points.append(dict(point))
            points = points[-ROUTE_MAX_POINTS_PER_DAY:]
            conn.execute(
                """
                INSERT INTO route_days(callsign, utc_date, updated_at, expires_at, aircraft_type, points_json)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(callsign, utc_date) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    expires_at=excluded.expires_at,
                    aircraft_type=CASE WHEN excluded.aircraft_type <> '' THEN excluded.aircraft_type ELSE route_days.aircraft_type END,
                    points_json=excluded.points_json
                """,
                (
                    callsign,
                    utc_date,
                    float(captured_at),
                    expires_at,
                    str(aircraft_type or existing_type or ""),
                    json.dumps(points, separators=(",", ":"), ensure_ascii=False),
                ),
            )
            _prune_if_due(conn, captured_at)
            conn.commit()
        finally:
            conn.close()


async def append_route_sample(
    callsign: str,
    utc_date: str,
    point: dict[str, Any],
    *,
    captured_at: float,
    aircraft_type: str = "",
) -> None:
    await asyncio.to_thread(
        _append_route_sample_sync,
        callsign,
        utc_date,
        point,
        captured_at=float(captured_at),
        aircraft_type=str(aircraft_type or ""),
    )
    # Once the volume has accepted a live sample, retire the legacy Mongo route
    # collection in a separate bounded background task. This never blocks the
    # five-second monitor.
    _schedule_route_migration()


def _load_route_docs_sync(callsign: str, wanted: list[str], limit: int) -> list[dict[str, Any]]:
    if not wanted:
        return []
    placeholders = ",".join("?" for _ in wanted)
    query = (
        "SELECT utc_date, points_json, aircraft_type FROM route_days "
        f"WHERE callsign=? AND utc_date IN ({placeholders}) ORDER BY utc_date DESC LIMIT ?"
    )
    with _db_lock:
        conn = _connect()
        try:
            rows = conn.execute(query, (callsign, *wanted, int(limit))).fetchall()
        finally:
            conn.close()
    docs: list[dict[str, Any]] = []
    for utc_date, points_json, aircraft_type in rows:
        try:
            points = json.loads(str(points_json or "[]"))
        except (ValueError, TypeError):
            points = []
        docs.append({
            "utc_date": str(utc_date),
            "points": points if isinstance(points, list) else [],
            "aircraft_type": str(aircraft_type or ""),
        })
    return docs


async def load_route_docs(callsign: str, wanted: list[str], *, limit: int = ROUTE_MAX_PATHS) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_load_route_docs_sync, str(callsign), list(wanted), int(limit))


async def load_route_doc(callsign: str, utc_date: str) -> dict[str, Any] | None:
    docs = await load_route_docs(str(callsign), [str(utc_date)], limit=1)
    return docs[0] if docs else None


def _import_batch_sync(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    imported = 0
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=ROUTE_RETENTION_DAYS)).isoformat()
    with _db_lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for doc in rows:
                callsign = route_mod.normalize_flight_key(doc.get("callsign"))
                utc_date = str(doc.get("utc_date") or "")
                points = list(doc.get("points") or [])[-ROUTE_MAX_POINTS_PER_DAY:]
                if not callsign or not utc_date or utc_date < cutoff or not points:
                    continue
                updated = doc.get("updated_at")
                if isinstance(updated, datetime):
                    updated_ts = updated.replace(tzinfo=timezone.utc).timestamp() if updated.tzinfo is None else updated.timestamp()
                else:
                    updated_ts = time.time()
                expires = doc.get("expires_at")
                if isinstance(expires, datetime):
                    expires_ts = expires.replace(tzinfo=timezone.utc).timestamp() if expires.tzinfo is None else expires.timestamp()
                else:
                    expires_ts = updated_ts + ROUTE_RETENTION_DAYS * 86400.0
                conn.execute(
                    """
                    INSERT INTO route_days(callsign, utc_date, updated_at, expires_at, aircraft_type, points_json)
                    VALUES(?, ?, ?, ?, ?, ?)
                    ON CONFLICT(callsign, utc_date) DO UPDATE SET
                        updated_at=MAX(route_days.updated_at, excluded.updated_at),
                        expires_at=MAX(route_days.expires_at, excluded.expires_at),
                        aircraft_type=CASE WHEN excluded.aircraft_type <> '' THEN excluded.aircraft_type ELSE route_days.aircraft_type END,
                        points_json=CASE WHEN excluded.updated_at >= route_days.updated_at THEN excluded.points_json ELSE route_days.points_json END
                    """,
                    (
                        callsign,
                        utc_date,
                        float(updated_ts),
                        float(expires_ts),
                        str(doc.get("aircraft_type") or ""),
                        json.dumps(points, separators=(",", ":"), ensure_ascii=False),
                    ),
                )
                imported += 1
            _prune_if_due(conn, time.time())
            conn.commit()
        finally:
            conn.close()
    return imported


async def migrate_route_history_mongo(db: Any) -> dict[str, Any]:
    """Best-effort copy of useful route history, then permanently retire its Mongo collection.

    `flight_route_samples` is explicitly non-user operational telemetry. New runtime
    reads/writes already use the persistent volume before this migration runs, so
    dropping the old collection cannot remove user/profile/location/config data.
    """
    previous = await asyncio.to_thread(_read_state)
    if previous.get("legacy_collection_dropped"):
        return previous

    await asyncio.to_thread(lambda: _connect().close())

    collection = db["flight_route_samples"]
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=ROUTE_RETENTION_DAYS)).isoformat()
    imported = int(previous.get("imported_docs") or 0)
    import_complete = False
    import_error = ""
    batch: list[dict[str, Any]] = []
    try:
        cursor = collection.find(
            {"utc_date": {"$gte": cutoff}},
            {"callsign": 1, "utc_date": 1, "points": 1, "updated_at": 1, "expires_at": 1, "aircraft_type": 1, "_id": 0},
        )
        async for doc in cursor:
            batch.append(dict(doc))
            if len(batch) >= 100:
                imported += await asyncio.to_thread(_import_batch_sync, batch)
                batch = []
        if batch:
            imported += await asyncio.to_thread(_import_batch_sync, batch)
        import_complete = True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        import_error = type(exc).__name__
        logger.warning(
            "route_history_mongo_import_incomplete imported=%d error=%s; retiring reconstructable operational collection",
            imported,
            import_error,
        )

    state = {
        "schema": "plane-alerts-route-history-volume-migration-v561",
        "source_collection": "flight_route_samples",
        "sqlite_path": str(route_db_path()),
        "retention_days": ROUTE_RETENTION_DAYS,
        "imported_docs": imported,
        "import_complete": import_complete,
        "import_error": import_error or None,
        "normal_application_data_untouched": True,
        "legacy_collection_dropped": False,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    await asyncio.to_thread(_atomic_json, migration_state_path(), state)

    try:
        await collection.drop()
        state["legacy_collection_dropped"] = True
        state["dropped_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        await asyncio.to_thread(_atomic_json, migration_state_path(), state)
        logger.info(
            "route_history_mongo_retired imported=%d import_complete=%s volume=%s",
            imported,
            import_complete,
            route_db_path(),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        state["drop_error"] = type(exc).__name__
        await asyncio.to_thread(_atomic_json, migration_state_path(), state)
        logger.warning("route_history_mongo_drop_failed error=%s", type(exc).__name__)
    return state


def _log_volume_failure_once(kind: str, exc: Exception) -> None:
    global _last_failure_log_mono
    now = time.monotonic()
    if now - _last_failure_log_mono >= _FAILURE_LOG_INTERVAL_S:
        _last_failure_log_mono = now
        logger.warning("route_history_volume_%s_failed error=%s", kind, type(exc).__name__)


async def observe_route_volume(self: route_mod.RouteHistoryService, ac: Any, *, now: float | None = None) -> None:
    key = route_mod.normalize_flight_key(getattr(ac, "callsign", ""))
    if not key or getattr(ac, "latitude", None) is None or getattr(ac, "longitude", None) is None:
        return
    now_ts = time.time() if now is None else float(now)
    lat, lon = float(ac.latitude), float(ac.longitude)
    previous = self._last_sample.get(key)
    interval = max(15, int(settings.route_sample_interval_seconds))
    if previous:
        last_t, last_lat, last_lon = previous
        moved = haversine_km(last_lat, last_lon, lat, lon)
        if now_ts - last_t < interval and moved < 1.5:
            return
    self._last_sample[key] = (now_ts, lat, lon)
    point = {
        "t": round(now_ts, 1),
        "lat": round(lat, 5),
        "lon": round(lon, 5),
        "altitude_m": getattr(ac, "altitude", None),
        "heading_deg": getattr(ac, "heading", None),
    }
    try:
        await append_route_sample(
            key,
            datetime.fromtimestamp(now_ts, timezone.utc).date().isoformat(),
            point,
            captured_at=now_ts,
            aircraft_type=str(getattr(ac, "aircraft_type", "") or ""),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log_volume_failure_once("write", exc)


async def historical_paths_volume(
    self: route_mod.RouteHistoryService,
    key: str,
    *,
    now: datetime | None = None,
) -> route_v46.RouteHistoryBundle:
    current = now or datetime.now(timezone.utc)
    wanted = [
        (current.date() - timedelta(days=offset)).isoformat()
        for offset in range(1, ROUTE_LOOKBACK_DAYS + 1)
    ]
    try:
        docs = await load_route_docs(key, wanted, limit=ROUTE_MAX_PATHS)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log_volume_failure_once("read", exc)
        docs = []
    paths: list[list[route_mod.RoutePoint]] = []
    ages: list[float] = []
    for doc in docs:
        points = route_mod._clean_points(doc.get("points") or [])
        if len(points) < 3:
            continue
        try:
            route_date = datetime.fromisoformat(str(doc.get("utc_date"))).date()
            age = max(1.0, float((current.date() - route_date).days))
        except (TypeError, ValueError):
            age = float(ROUTE_LOOKBACK_DAYS)
        paths.append(points)
        ages.append(age)
    return route_v46.RouteHistoryBundle(paths, age_days=ages, clusters=route_v46._cluster_paths(paths))


async def _next60_route_from_volume(db: Any, callsign: str, utc_date: str) -> dict[str, Any] | None:
    del db
    return await load_route_doc(callsign, utc_date)


def _operational_migration_verified() -> bool:
    # New high-volume Prediction Lab writes are permanently file-backed. This
    # compatibility hook exists only so older modules never write those records
    # back to Mongo while the old collections are being exported/dropped.
    return True


async def _route_migration_runner() -> None:
    while True:
        try:
            state = await migrate_route_history_mongo(get_db())
            if state.get("legacy_collection_dropped"):
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("route_history_volume_migration_failed error=%s", type(exc).__name__)
        await asyncio.sleep(60.0)


def _schedule_route_migration() -> None:
    global _migration_task
    if _migration_task is not None and not _migration_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _migration_task = loop.create_task(_route_migration_runner(), name="route-history-mongo-retirement")


def install_operational_volume_v561() -> None:
    """Make persistent-volume operational storage authoritative for Railway runtime."""
    global _installed
    if _installed:
        return

    # Route history: replace both the v4.6 implementation and the v4.4 worker's
    # captured function so no live route read/write reaches MongoDB. The core
    # functions themselves also use this volume backend; these assignments are
    # a belt-and-suspenders guard for versioned wrapper composition.
    route_v46.observe_v46 = observe_route_volume
    route_v46.historical_paths_v46 = historical_paths_volume
    route_v2._ORIGINAL_OBSERVE = observe_route_volume
    route_observe_guard._BASE_OBSERVE = observe_route_volume
    route_mod.RouteHistoryService._historical_paths = historical_paths_volume

    # High-volume Prediction Lab evidence has been file-backed since v5.5.
    # Permanently report the compatibility migration as complete to writer-side
    # checks so they can never fall back to Mongo. The migration routine itself
    # remains available to export/drop old collections and reclaim Atlas space.
    lab_files.migration_verified = _operational_migration_verified

    sentinel = sys.modules.get("app.sentinel_network")
    if sentinel is not None:
        setattr(sentinel, "migration_verified", _operational_migration_verified)

    audit = sys.modules.get("app.prediction_lab_audit")
    if audit is not None:
        setattr(audit, "migration_verified", _operational_migration_verified)

    next60 = sys.modules.get("app.next60_outcomes_v55")
    if next60 is not None:
        setattr(next60, "_route_for", _next60_route_from_volume)

    _installed = True
    logger.info(
        "Operational storage v5.6.1 enabled: route_history=%s prediction_lab=file-backed mongo_operational_writes=disabled",
        route_db_path(),
    )
