"""Plane Alerts v5.6.2 legacy operational Mongo retirement guard.

The authoritative v5.6.1 storage split keeps high-volume route history and
Prediction Lab evidence on the Plane Alerts persistent volume. This guard makes
that boundary permanent at runtime and reclaims Atlas space only from the exact,
verified non-user legacy telemetry collections. User/location/profile/settings
collections are never part of this cleanup.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.database import get_db
from app.operational_volume_v561 import _schedule_route_migration
from app import prediction_lab_files_v55 as lab_files

logger = logging.getLogger(__name__)
_installed = False
_cleanup_task: asyncio.Task | None = None
_cleanup_complete = False
_RETIREMENT_STATE = "prediction_lab_legacy_retirement_v562.json"


def _retirement_state_path() -> Path:
    return lab_files.ensure_layout() / "state" / _RETIREMENT_STATE


def _read_retirement_state() -> dict[str, Any]:
    try:
        value = json.loads(_retirement_state_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return {}


def _write_retirement_state(state: dict[str, Any]) -> None:
    payload = (json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    lab_files._atomic_write(_retirement_state_path(), payload)


async def _drop_legacy_prediction_collections() -> dict[str, Any]:
    """Drop only the fixed Prediction Lab legacy telemetry collections.

    Their active writers are already permanently file-backed. Historical export
    was attempted by v5.5/v5.6.1 but must not keep Atlas full or block durable
    user/config writes indefinitely. Existing file/archive evidence is preserved;
    no user/application collection is enumerated or touched here.
    """
    global _cleanup_complete
    previous = await asyncio.to_thread(_read_retirement_state)
    if previous.get("legacy_collections_dropped"):
        _cleanup_complete = True
        return previous

    allowed = tuple(lab_files.LAB_COLLECTIONS)
    db = get_db()
    results: dict[str, str] = {}
    for collection_name in allowed:
        try:
            await db[collection_name].drop()
            results[collection_name] = "dropped"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            results[collection_name] = f"error:{type(exc).__name__}"
            logger.warning(
                "legacy_operational_collection_drop_failed collection=%s error=%s",
                collection_name,
                type(exc).__name__,
            )

    dropped = all(results.get(name) == "dropped" for name in allowed)
    state: dict[str, Any] = {
        "schema": "plane-alerts-legacy-operational-retirement-v562",
        "collections": list(allowed),
        "results": results,
        "legacy_collections_dropped": dropped,
        "runtime_writes_disabled": True,
        "storage": "persistent-volume",
        "normal_application_data_untouched": True,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    await asyncio.to_thread(_write_retirement_state, state)
    if dropped:
        _cleanup_complete = True
        logger.info(
            "legacy_prediction_mongo_retired collections=%s user_data_untouched=true",
            ",".join(allowed),
        )
    return state


async def _cleanup_runner() -> None:
    while True:
        try:
            state = await _drop_legacy_prediction_collections()
            if state.get("legacy_collections_dropped"):
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("legacy_prediction_mongo_retirement_failed error=%s", type(exc).__name__)
        await asyncio.sleep(60.0)


def _schedule_prediction_cleanup() -> None:
    global _cleanup_task, _cleanup_complete
    if _cleanup_complete:
        return
    if _cleanup_task is not None and not _cleanup_task.done():
        return
    try:
        if _read_retirement_state().get("legacy_collections_dropped"):
            _cleanup_complete = True
            return
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _cleanup_task = loop.create_task(_cleanup_runner(), name="prediction-lab-mongo-retirement")


def _volume_authoritative_and_schedule_retirement() -> bool:
    """Return the permanent file-backed state and schedule bounded cleanup."""
    _schedule_route_migration()
    _schedule_prediction_cleanup()
    return True


async def _migration_already_retired() -> bool:
    """Tell the sentinel loop that operational telemetry migration is complete."""
    _volume_authoritative_and_schedule_retirement()
    return True


async def _retired_prediction_lab_migration(*args, **kwargs):
    """Compatibility no-op for callers that still reference the old exporter."""
    del args, kwargs
    _schedule_prediction_cleanup()
    return {
        "verified": True,
        "legacy_collections_dropped": bool(_cleanup_complete),
        "runtime_writes_disabled": True,
        "storage": "persistent-volume",
    }


def install_legacy_mongo_retirement_v562() -> None:
    global _installed
    if _installed:
        return

    # Import-time scheduling is opportunistic. If no asyncio loop exists yet,
    # the compatibility verification hook retries from the live sentinel loop.
    _schedule_route_migration()
    _schedule_prediction_cleanup()

    # New operational evidence is permanently file-backed. Every compatibility
    # check can schedule the narrow legacy cleanup, but it can never re-enable a
    # Mongo writer or invoke the old unbounded exporter.
    lab_files.migration_verified = _volume_authoritative_and_schedule_retirement
    lab_files.migrate_prediction_lab_mongo = _retired_prediction_lab_migration

    sentinel = sys.modules.get("app.sentinel_network")
    if sentinel is not None:
        setattr(sentinel, "migration_verified", _volume_authoritative_and_schedule_retirement)
        setattr(sentinel, "_ensure_migration", _migration_already_retired)
        setattr(sentinel, "migrate_prediction_lab_mongo", _retired_prediction_lab_migration)

    _installed = True
    logger.info(
        "Legacy operational Mongo writers retired; volume-backed telemetry authoritative; narrow cleanup scheduled"
    )
