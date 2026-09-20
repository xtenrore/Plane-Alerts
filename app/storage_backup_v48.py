"""Validated Plane Alerts v4.8 export/import helpers.

Exports are allow-listed, secret-free and location-safe by default. Imports use
natural record identities and never blindly replace a newer persisted record.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

EXPORT_FORMAT = "plane-alerts-storage-export"
EXPORT_SCHEMA = 1

_BASE_COLLECTIONS = (
    "users",
    "preferences",
    "profiles",
    "camera_profiles",
    "provider_learning",
    "approach_states",
    "flight_route_samples",
    "prediction_lab_audit",
    "error_museum",
)
_SECRET_NAMES = {
    "token", "telegram_bot_token", "api_key", "apikey", "secret", "password",
    "mongo_uri", "mongodb_uri", "credential", "credentials",
}


def _sensitive_name(name: str) -> bool:
    lowered = str(name).lower()
    return lowered in _SECRET_NAMES or any(part in lowered for part in ("api_key", "token", "password", "secret", "credential"))


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return {"$plane_datetime": aware.isoformat()}
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items() if not _sensitive_name(str(key)) and key != "_id"}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _decode(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"$plane_datetime"}:
        raw = str(value["$plane_datetime"])
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    if isinstance(value, dict):
        return {str(key): _decode(item) for key, item in value.items() if not _sensitive_name(str(key)) and key != "_id"}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


def _identity(collection: str, doc: dict[str, Any]) -> dict[str, Any] | None:
    fields = {
        "users": ("user_id",),
        "preferences": ("user_id",),
        "profiles": ("user_id", "profile_id"),
        "camera_profiles": ("user_id",),
        "provider_learning": ("user_id", "geohash"),
        "approach_states": ("user_id", "aircraft_icao24"),
        "flight_route_samples": ("callsign", "utc_date"),
        "prediction_lab_audit": ("kind", "user_id", "aircraft_icao24", "captured_at"),
        "error_museum": ("case_id",),
        "locations": ("user_id",),
    }.get(collection)
    if not fields or any(doc.get(field) is None for field in fields):
        return None
    return {field: doc[field] for field in fields}


def _record_time(doc: dict[str, Any]) -> datetime | None:
    for name in ("updated_at", "captured_at", "created_at"):
        value = doc.get(name)
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return None


async def export_storage(db: Any, *, include_exact_locations: bool = False) -> dict[str, Any]:
    collections = list(_BASE_COLLECTIONS)
    if include_exact_locations:
        collections.append("locations")
    payload: dict[str, Any] = {
        "format": EXPORT_FORMAT,
        "schema": EXPORT_SCHEMA,
        "release": "4.8.0",
        "includes_exact_locations": bool(include_exact_locations),
        "exported_at": _encode(datetime.now(timezone.utc)),
        "collections": {},
    }
    for name in collections:
        rows: list[dict[str, Any]] = []
        try:
            async for doc in db[name].find({}):
                clean = _encode(doc)
                if isinstance(clean, dict) and _identity(name, _decode(clean)) is not None:
                    rows.append(clean)
        except Exception:
            continue
        payload["collections"][name] = rows
    return payload


async def import_storage(db: Any, payload: dict[str, Any], *, allow_exact_locations: bool = False) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("format") != EXPORT_FORMAT or payload.get("schema") != EXPORT_SCHEMA:
        raise ValueError("Unsupported Plane Alerts export format/schema")
    raw_collections = payload.get("collections")
    if not isinstance(raw_collections, dict):
        raise ValueError("Malformed export: collections must be an object")
    if payload.get("includes_exact_locations") and not allow_exact_locations:
        raise ValueError("Export contains exact locations; explicit allow_exact_locations is required")

    allowed = set(_BASE_COLLECTIONS)
    if allow_exact_locations:
        allowed.add("locations")
    report = {"inserted": 0, "updated": 0, "skipped": 0, "rejected": 0, "collections": {}}

    for name, raw_rows in raw_collections.items():
        if name not in allowed or not isinstance(raw_rows, list):
            report["rejected"] += len(raw_rows) if isinstance(raw_rows, list) else 1
            continue
        stats = {"inserted": 0, "updated": 0, "skipped": 0, "rejected": 0}
        for raw in raw_rows:
            if not isinstance(raw, dict):
                stats["rejected"] += 1
                continue
            doc = _decode(raw)
            if not isinstance(doc, dict) or any(_sensitive_name(key) for key in doc):
                stats["rejected"] += 1
                continue
            identity = _identity(name, doc)
            if identity is None:
                stats["rejected"] += 1
                continue
            existing = await db[name].find_one(identity)
            if not existing:
                await db[name].insert_one(doc)
                stats["inserted"] += 1
                continue
            source_time = _record_time(doc)
            existing_time = _record_time(existing)
            if source_time is not None and existing_time is not None and existing_time > source_time:
                stats["skipped"] += 1
                continue
            comparable_existing = {key: value for key, value in existing.items() if key != "_id"}
            if comparable_existing == doc:
                stats["skipped"] += 1
                continue
            await db[name].update_one(identity, {"$set": doc}, upsert=False)
            stats["updated"] += 1
        report["collections"][name] = stats
        for key in ("inserted", "updated", "skipped", "rejected"):
            report[key] += stats[key]
    return report
