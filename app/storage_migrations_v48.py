"""Small deterministic schema-migration framework for Plane Alerts v4.8."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.storage_metrics_v48 import storage_metrics

EXPECTED_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    migration_id: str
    description: str
    apply: Callable[[Any], Awaitable[None]]
    verify: Callable[[Any], Awaitable[bool]]


async def _apply_v1(db: Any) -> None:
    await db["schema_migrations"].create_index("version", unique=True)
    await db["approach_states"].create_index([("active", 1), ("expires_at", 1)])
    await db["profiles"].create_index([("user_id", 1), ("updated_at", 1)])


async def _verify_v1(db: Any) -> bool:
    await db.command("ping")
    return True


MIGRATIONS = (
    Migration(
        1,
        "v48-storage-baseline",
        "Track Plane Alerts schema version and restart-safe storage indexes",
        _apply_v1,
        _verify_v1,
    ),
)


async def run_migrations(db: Any) -> int:
    """Apply pending migrations once and verify already-applied migrations."""
    current = 0
    collection = db["schema_migrations"]
    async for row in collection.find({}, {"version": 1, "migration_id": 1, "_id": 0}):
        try:
            current = max(current, int(row.get("version", 0) or 0))
        except (TypeError, ValueError):
            continue

    for migration in MIGRATIONS:
        existing = await collection.find_one({"version": migration.version})
        if existing:
            if str(existing.get("migration_id") or "") != migration.migration_id:
                raise RuntimeError(f"Schema migration {migration.version} id mismatch")
            if not await migration.verify(db):
                raise RuntimeError(f"Schema migration {migration.version} verification failed")
            current = max(current, migration.version)
            continue

        await migration.apply(db)
        if not await migration.verify(db):
            raise RuntimeError(f"Schema migration {migration.version} verification failed")
        await collection.update_one(
            {"version": migration.version},
            {"$setOnInsert": {
                "version": migration.version,
                "migration_id": migration.migration_id,
                "description": migration.description,
                "applied_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
        current = migration.version

    if current != EXPECTED_SCHEMA_VERSION:
        raise RuntimeError(
            f"Plane Alerts schema mismatch: expected {EXPECTED_SCHEMA_VERSION}, found {current}"
        )
    storage_metrics.set_schema_version(current)
    return current
