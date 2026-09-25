"""Plane Alerts v5.6.2 persistent operational state.

MongoDB is reserved for durable user/application configuration. Short-lived,
high-churn runtime state lives on the Plane Alerts persistent volume instead:
alert lifecycle restart state, notification delivery telemetry, photo alert
snapshots, worker status and generic analytical records.

The live five-second path never waits for this module's disk I/O. Existing
callers enqueue/coalesce work and these functions use ``asyncio.to_thread``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.prediction_lab_files_v55 import root_path

logger = logging.getLogger(__name__)

DB_RELATIVE = Path("runtime") / "operational_state.sqlite3"
STATE_RELATIVE = Path("state") / "operational_mongo_retirement_v562.json"
DEFAULT_RETENTION_DAYS = 35
NOTIFICATION_RETENTION_S = 86400.0
MAX_APPROACH_STATES = 4096
MAX_NOTIFICATION_IMPORT = 4096
MAX_PHOTO_IMPORT = 2048
MAX_STATUS_IMPORT = 64
PRUNE_INTERVAL_S = 1800.0

_lock = threading.RLock()
_last_prune_mono = 0.0


def db_path() -> Path:
    return root_path() / DB_RELATIVE


def migration_state_path() -> Path:
    return root_path() / STATE_RELATIVE


def _epoch(value: Any) -> float | None:
    if isinstance(value, datetime):
        current = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return current.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            current = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            return current.timestamp()
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return None
    return None


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        current = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return {"__plane_datetime__": current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
    if isinstance(value, date):
        return {"__plane_date__": value.isoformat()}
    if isinstance(value, (set, tuple)):
        return list(value)
    if isinstance(value, bytes):
        return {"__plane_bytes__": value.hex()}
    return str(value)


def _restore(value: Any) -> Any:
    if isinstance(value, list):
        return [_restore(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"__plane_datetime__"}:
            try:
                current = datetime.fromisoformat(str(value["__plane_datetime__"]).replace("Z", "+00:00"))
                return current if current.tzinfo is not None else current.replace(tzinfo=timezone.utc)
            except ValueError:
                return value["__plane_datetime__"]
        if set(value) == {"__plane_date__"}:
            try:
                return date.fromisoformat(str(value["__plane_date__"]))
            except ValueError:
                return value["__plane_date__"]
        if set(value) == {"__plane_bytes__"}:
            try:
                return bytes.fromhex(str(value["__plane_bytes__"]))
            except ValueError:
                return b""
        return {str(key): _restore(item) for key, item in value.items()}
    return value


def _dumps(document: dict[str, Any]) -> str:
    return json.dumps(document, default=_json_default, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _loads(raw: str) -> dict[str, Any]:
    try:
        value = _restore(json.loads(raw))
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS approach_states (
            user_id INTEGER NOT NULL,
            aircraft_icao24 TEXT NOT NULL,
            updated_at REAL NOT NULL,
            expires_at REAL,
            active INTEGER NOT NULL DEFAULT 0,
            doc_json TEXT NOT NULL,
            PRIMARY KEY (user_id, aircraft_icao24)
        );
        CREATE INDEX IF NOT EXISTS approach_states_expiry_idx ON approach_states(expires_at);

        CREATE TABLE IF NOT EXISTS notification_history (
            notification_id TEXT PRIMARY KEY,
            user_id INTEGER,
            last_event_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            doc_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS notification_history_user_idx ON notification_history(user_id, last_event_at DESC);
        CREATE INDEX IF NOT EXISTS notification_history_expiry_idx ON notification_history(expires_at);

        CREATE TABLE IF NOT EXISTS photo_alert_snapshots (
            notification_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at REAL NOT NULL,
            doc_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS photo_alert_snapshots_expiry_idx ON photo_alert_snapshots(expires_at);

        CREATE TABLE IF NOT EXISTS system_status (
            document_id TEXT PRIMARY KEY,
            updated_at REAL NOT NULL,
            doc_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS optional_documents (
            collection_name TEXT NOT NULL,
            document_id TEXT NOT NULL,
            updated_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            doc_json TEXT NOT NULL,
            PRIMARY KEY (collection_name, document_id)
        );
        CREATE INDEX IF NOT EXISTS optional_documents_expiry_idx ON optional_documents(expires_at);
        """
    )
    return conn


def _prune_if_due(conn: sqlite3.Connection, now_ts: float | None = None) -> None:
    global _last_prune_mono
    now_mono = time.monotonic()
    if now_mono - _last_prune_mono < PRUNE_INTERVAL_S:
        return
    now_value = time.time() if now_ts is None else float(now_ts)
    conn.execute("DELETE FROM approach_states WHERE active=0 AND expires_at IS NOT NULL AND expires_at <= ?", (now_value,))
    conn.execute("DELETE FROM notification_history WHERE expires_at <= ?", (now_value,))
    conn.execute("DELETE FROM photo_alert_snapshots WHERE expires_at <= ?", (now_value,))
    conn.execute("DELETE FROM optional_documents WHERE expires_at <= ?", (now_value,))
    _last_prune_mono = now_mono


def _atomic_state(payload: dict[str, Any]) -> None:
    path = migration_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, default=_json_default, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
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
    except (OSError, ValueError, TypeError):
        return {}


def _save_approach_sync(document: dict[str, Any]) -> dict[str, Any]:
    clean = dict(document)
    try:
        user_id = int(clean["user_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("approach state requires user_id") from exc
    icao = str(clean.get("aircraft_icao24") or "").strip()
    if not icao:
        raise ValueError("approach state requires aircraft_icao24")
    clean.pop("_id", None)
    updated_at = _epoch(clean.get("updated_at")) or time.time()
    expires_at = _epoch(clean.get("expires_at"))
    active = 1 if bool(clean.get("active")) else 0

    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT updated_at, doc_json FROM approach_states WHERE user_id=? AND aircraft_icao24=?",
                (user_id, icao),
            ).fetchone()
            if row and float(row[0]) > float(updated_at):
                return _loads(str(row[1]))
            conn.execute(
                """
                INSERT INTO approach_states(user_id, aircraft_icao24, updated_at, expires_at, active, doc_json)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, aircraft_icao24) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    expires_at=excluded.expires_at,
                    active=excluded.active,
                    doc_json=excluded.doc_json
                """,
                (user_id, icao, float(updated_at), expires_at, active, _dumps(clean)),
            )
            _prune_if_due(conn)
            conn.commit()
            return clean
        finally:
            conn.close()


async def persist_approach_state(document: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(_save_approach_sync, dict(document))


def _load_restart_sync() -> list[dict[str, Any]]:
    now_ts = time.time()
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT doc_json FROM approach_states
                WHERE active=1 OR (expires_at IS NOT NULL AND expires_at > ?)
                ORDER BY updated_at DESC LIMIT ?
                """,
                (now_ts, MAX_APPROACH_STATES),
            ).fetchall()
            _prune_if_due(conn, now_ts)
            conn.commit()
        finally:
            conn.close()
    return [_loads(str(row[0])) for row in rows]


async def load_restart_states(db: Any | None = None) -> list[dict[str, Any]]:
    if db is not None:
        await migrate_operational_mongo(db)
    return await asyncio.to_thread(_load_restart_sync)


def _status_write_sync(document_id: str, values: dict[str, Any]) -> None:
    doc = dict(values)
    doc["_id"] = str(document_id)
    updated_at = _epoch(doc.get("updated_at")) or time.time()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO system_status(document_id, updated_at, doc_json) VALUES(?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET updated_at=excluded.updated_at, doc_json=excluded.doc_json
                """,
                (str(document_id), float(updated_at), _dumps(doc)),
            )
            conn.commit()
        finally:
            conn.close()


async def persist_system_status(document_id: str, values: dict[str, Any]) -> None:
    await asyncio.to_thread(_status_write_sync, str(document_id), dict(values))


def _status_read_sync(document_id: str) -> dict[str, Any] | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT doc_json FROM system_status WHERE document_id=?", (str(document_id),)).fetchone()
        finally:
            conn.close()
    return _loads(str(row[0])) if row else None


async def get_system_status(document_id: str) -> dict[str, Any] | None:
    return await asyncio.to_thread(_status_read_sync, str(document_id))


def _optional_write_sync(collection: str, document_id: str, document: dict[str, Any]) -> None:
    payload = dict(document)
    payload["_id"] = str(document_id)
    now_ts = time.time()
    updated_at = _epoch(payload.get("updated_at")) or _epoch(payload.get("storage_queued_at")) or now_ts
    expires_at = _epoch(payload.get("expires_at")) or (now_ts + DEFAULT_RETENTION_DAYS * 86400.0)
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO optional_documents(collection_name, document_id, updated_at, expires_at, doc_json)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(collection_name, document_id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    expires_at=excluded.expires_at,
                    doc_json=excluded.doc_json
                """,
                (str(collection), str(document_id), float(updated_at), float(expires_at), _dumps(payload)),
            )
            _prune_if_due(conn, now_ts)
            conn.commit()
        finally:
            conn.close()


async def persist_optional_document(collection: str, document_id: str, document: dict[str, Any]) -> None:
    await asyncio.to_thread(_optional_write_sync, str(collection), str(document_id), dict(document))


def _photo_write_sync(notification_id: str, user_id: int, values: dict[str, Any]) -> None:
    payload = dict(values)
    payload["_id"] = str(notification_id)
    payload["user_id"] = int(user_id)
    expires_at = _epoch(payload.get("expires_at")) or (time.time() + 6 * 3600.0)
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO photo_alert_snapshots(notification_id, user_id, expires_at, doc_json)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(notification_id) DO UPDATE SET
                    user_id=excluded.user_id,
                    expires_at=excluded.expires_at,
                    doc_json=excluded.doc_json
                """,
                (str(notification_id), int(user_id), float(expires_at), _dumps(payload)),
            )
            _prune_if_due(conn)
            conn.commit()
        finally:
            conn.close()


async def persist_photo_snapshot(notification_id: str, user_id: int, values: dict[str, Any]) -> None:
    await asyncio.to_thread(_photo_write_sync, str(notification_id), int(user_id), dict(values))


def _photo_read_sync(notification_id: str, user_id: int) -> dict[str, Any] | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT doc_json FROM photo_alert_snapshots WHERE notification_id=? AND user_id=? AND expires_at>?",
                (str(notification_id), int(user_id), time.time()),
            ).fetchone()
        finally:
            conn.close()
    return _loads(str(row[0])) if row else None


async def get_photo_snapshot(notification_id: str, user_id: int) -> dict[str, Any] | None:
    return await asyncio.to_thread(_photo_read_sync, str(notification_id), int(user_id))


def _notification_write_sync(payload: dict[str, Any]) -> None:
    notification_id = str(payload["notification_id"])
    occurred_at = payload.get("occurred_at") or datetime.now(timezone.utc)
    occurred_ts = _epoch(occurred_at) or time.time()
    delivered = bool(payload.get("delivered"))
    input_message_id = payload.get("input_message_id")
    output_message_id = payload.get("output_message_id")
    stage = str(payload.get("stage") or "")
    logical_type = "first_notification" if input_message_id is None else ("cancellation_update" if stage == "cancelled" else "passed_update" if stage == "passed" else "message_update")

    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT doc_json FROM notification_history WHERE notification_id=?", (notification_id,)).fetchone()
            existing = _loads(str(row[0])) if row else {}
            prior_attempts = int(existing.get("delivery_attempts", 0) or 0)
            has_first_delivery = existing.get("first_notified_at") is not None
            retrying_same_event = (
                prior_attempts > 0
                and existing.get("last_delivery_success") is False
                and str(existing.get("last_logical_event_type") or "") == logical_type
                and str(existing.get("last_stage") or "") == stage
            )
            event_type = "failed_delivery" if not delivered else "retry" if retrying_same_event else logical_type
            attempt_type = "retry" if retrying_same_event else ("initial" if input_message_id is None else "update")
            event = {
                "event_type": event_type,
                "logical_event_type": logical_type,
                "attempt_type": attempt_type,
                "stage": stage,
                "delivered": delivered,
                "occurred_at": occurred_at,
                "input_message_id": input_message_id,
                "output_message_id": output_message_id,
                "delivery_detail": payload.get("delivery_detail") or "",
            }
            events = list(existing.get("events") or [])
            events.append(event)
            events = events[-64:]
            counts = dict(existing.get("event_counts") or {})
            counts[event_type] = int(counts.get(event_type, 0) or 0) + 1

            document = dict(existing)
            document.update({
                "_id": notification_id,
                "user_id": payload.get("user_id"),
                "aircraft_icao24": payload.get("aircraft_icao24") or "",
                "aircraft_type": payload.get("aircraft_type") or "",
                "distance_km": payload.get("distance_km"),
                "projected_closest_km": payload.get("projected_closest_km"),
                "observed_closest_km": payload.get("observed_closest_km"),
                "trajectory_state": payload.get("trajectory_state") or "",
                "prediction_confidence": payload.get("prediction_confidence") or "",
                "last_event_type": event_type,
                "last_logical_event_type": logical_type,
                "last_stage": stage,
                "last_event_at": occurred_at,
                "last_delivery_success": delivered,
                "last_message_id": output_message_id,
                "delivery_attempts": prior_attempts + 1,
                "failed_delivery_count": int(existing.get("failed_delivery_count", 0) or 0) + (0 if delivered else 1),
                "event_counts": counts,
                "events": events,
            })
            if delivered and logical_type == "first_notification" and not has_first_delivery:
                cooldown_until = datetime.fromtimestamp(occurred_ts, timezone.utc) + timedelta(minutes=settings.cooldown_minutes)
                document.update({
                    "first_notified_at": occurred_at,
                    "notified_at": occurred_at,
                    "cooldown_until": cooldown_until,
                    "first_message_id": output_message_id,
                    "alert_count": int(existing.get("alert_count", 0) or 0) + 1,
                })
            cooldown_ts = _epoch(document.get("cooldown_until"))
            expires_at = (cooldown_ts + NOTIFICATION_RETENTION_S) if cooldown_ts is not None else (occurred_ts + 2 * NOTIFICATION_RETENTION_S)
            conn.execute(
                """
                INSERT INTO notification_history(notification_id, user_id, last_event_at, expires_at, doc_json)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(notification_id) DO UPDATE SET
                    user_id=excluded.user_id,
                    last_event_at=excluded.last_event_at,
                    expires_at=excluded.expires_at,
                    doc_json=excluded.doc_json
                """,
                (notification_id, payload.get("user_id"), float(occurred_ts), float(expires_at), _dumps(document)),
            )
            _prune_if_due(conn, occurred_ts)
            conn.commit()
        finally:
            conn.close()


async def persist_notification_event(payload: dict[str, Any]) -> None:
    await asyncio.to_thread(_notification_write_sync, dict(payload))


def _notification_read_sync(notification_id: str, user_id: int | None = None) -> dict[str, Any] | None:
    with _lock:
        conn = _connect()
        try:
            if user_id is None:
                row = conn.execute("SELECT doc_json FROM notification_history WHERE notification_id=? AND expires_at>?", (str(notification_id), time.time())).fetchone()
            else:
                row = conn.execute("SELECT doc_json FROM notification_history WHERE notification_id=? AND user_id=? AND expires_at>?", (str(notification_id), int(user_id), time.time())).fetchone()
        finally:
            conn.close()
    return _loads(str(row[0])) if row else None


async def get_notification_history(notification_id: str, user_id: int | None = None) -> dict[str, Any] | None:
    return await asyncio.to_thread(_notification_read_sync, str(notification_id), user_id)


def _notification_count_sync() -> int:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT COUNT(*) FROM notification_history WHERE expires_at>?", (time.time(),)).fetchone()
        finally:
            conn.close()
    return int(row[0] if row else 0)


async def count_notification_history() -> int:
    return await asyncio.to_thread(_notification_count_sync)


def _recent_notifications_sync(limit: int) -> list[dict[str, Any]]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT doc_json FROM notification_history WHERE expires_at>? ORDER BY last_event_at DESC LIMIT ?",
                (time.time(), max(1, min(500, int(limit)))),
            ).fetchall()
        finally:
            conn.close()
    return [_loads(str(row[0])) for row in rows]


async def recent_notifications(limit: int = 50) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_recent_notifications_sync, int(limit))


def _import_document_sync(kind: str, document: dict[str, Any]) -> None:
    if kind == "approach_states":
        _save_approach_sync(document)
        return
    if kind == "notification_history":
        notification_id = str(document.get("_id") or "")
        if not notification_id:
            return
        last_event = _epoch(document.get("last_event_at")) or _epoch(document.get("notified_at")) or time.time()
        cooldown = _epoch(document.get("cooldown_until"))
        expires = (cooldown + NOTIFICATION_RETENTION_S) if cooldown is not None else last_event + 2 * NOTIFICATION_RETENTION_S
        with _lock:
            conn = _connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO notification_history(notification_id,user_id,last_event_at,expires_at,doc_json) VALUES(?,?,?,?,?)",
                    (notification_id, document.get("user_id"), float(last_event), float(expires), _dumps(document)),
                )
                conn.commit()
            finally:
                conn.close()
        return
    if kind == "photo_alert_snapshots":
        notification_id = str(document.get("_id") or "")
        try:
            user_id = int(document.get("user_id"))
        except (TypeError, ValueError):
            return
        if notification_id:
            _photo_write_sync(notification_id, user_id, document)
        return
    if kind == "system_status":
        document_id = str(document.get("_id") or "")
        if document_id:
            values = dict(document)
            values.pop("_id", None)
            _status_write_sync(document_id, values)


async def _fetch_docs(collection: Any, query: dict[str, Any], *, limit: int, sort_field: str | None = None) -> list[dict[str, Any]]:
    cursor = collection.find(query)
    if sort_field and callable(getattr(cursor, "sort", None)):
        cursor = cursor.sort(sort_field, -1)
    if callable(getattr(cursor, "limit", None)):
        cursor = cursor.limit(int(limit))
    if callable(getattr(cursor, "to_list", None)):
        rows = await cursor.to_list(length=int(limit))
        return [dict(row) for row in rows]
    rows: list[dict[str, Any]] = []
    async for row in cursor:
        rows.append(dict(row))
        if len(rows) >= limit:
            break
    return rows


async def _migrate_collection(
    db: Any,
    name: str,
    *,
    query: dict[str, Any],
    limit: int,
    sort_field: str | None = None,
    preserve_on_import_failure: bool,
) -> dict[str, Any]:
    collection = db[name]
    imported = 0
    import_complete = False
    import_error: str | None = None
    try:
        rows = await asyncio.wait_for(
            _fetch_docs(collection, query, limit=limit, sort_field=sort_field),
            timeout=5.0,
        )
        for row in rows:
            await asyncio.to_thread(_import_document_sync, name, row)
            imported += 1
        import_complete = True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        import_error = type(exc).__name__
        logger.warning("operational_mongo_import_incomplete collection=%s imported=%d error=%s", name, imported, import_error)

    dropped = False
    drop_error: str | None = None
    if import_complete or not preserve_on_import_failure:
        try:
            await asyncio.wait_for(collection.drop(), timeout=5.0)
            dropped = True
            logger.info("operational_mongo_retired collection=%s imported=%d complete=%s", name, imported, import_complete)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            drop_error = type(exc).__name__
            logger.warning("operational_mongo_drop_failed collection=%s error=%s", name, drop_error)
    return {
        "imported": imported,
        "import_complete": import_complete,
        "import_error": import_error,
        "dropped": dropped,
        "drop_error": drop_error,
    }


async def migrate_operational_mongo(db: Any) -> dict[str, Any]:
    previous = await asyncio.to_thread(_read_state)
    collections = dict(previous.get("collections") or {})
    if all(bool((collections.get(name) or {}).get("dropped")) for name in ("approach_states", "notification_history", "photo_alert_snapshots", "system_status", "flight_route_samples")):
        return previous

    now = datetime.now(timezone.utc)
    specs = (
        ("approach_states", {"$or": [{"active": True}, {"expires_at": {"$gt": now}}]}, MAX_APPROACH_STATES, "updated_at", True),
        ("notification_history", {}, MAX_NOTIFICATION_IMPORT, "last_event_at", False),
        ("photo_alert_snapshots", {"expires_at": {"$gt": now}}, MAX_PHOTO_IMPORT, "expires_at", False),
        ("system_status", {}, MAX_STATUS_IMPORT, "updated_at", False),
    )
    for name, query, limit, sort_field, preserve_on_failure in specs:
        if bool((collections.get(name) or {}).get("dropped")):
            continue
        collections[name] = await _migrate_collection(
            db,
            name,
            query=query,
            limit=limit,
            sort_field=sort_field,
            preserve_on_import_failure=preserve_on_failure,
        )
        state = {
            "schema": "plane-alerts-operational-volume-v562",
            "sqlite_path": str(db_path()),
            "normal_application_data_untouched": True,
            "collections": collections,
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        await asyncio.to_thread(_atomic_state, state)

    if not bool((collections.get("flight_route_samples") or {}).get("dropped")):
        try:
            await asyncio.wait_for(db["flight_route_samples"].drop(), timeout=5.0)
            collections["flight_route_samples"] = {"imported": 0, "import_complete": True, "dropped": True}
            logger.info("operational_mongo_retired collection=flight_route_samples imported=0 complete=true")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            collections["flight_route_samples"] = {"imported": 0, "import_complete": True, "dropped": False, "drop_error": type(exc).__name__}
            logger.warning("operational_mongo_drop_failed collection=flight_route_samples error=%s", type(exc).__name__)

    state = {
        "schema": "plane-alerts-operational-volume-v562",
        "sqlite_path": str(db_path()),
        "normal_application_data_untouched": True,
        "collections": collections,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    await asyncio.to_thread(_atomic_state, state)
    return state
