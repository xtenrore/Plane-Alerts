"""Plane Alerts v5.6.2 legacy operational Mongo retirement guard.

The authoritative v5.6.1 storage split keeps high-volume route history and
Prediction Lab evidence on the Plane Alerts persistent volume. This guard makes
that boundary permanent at runtime: it starts retirement of the reconstructable
legacy route collection from the live event loop and prevents the sentinel loop
from re-exporting already-retired Prediction Lab telemetry from Mongo.
"""
from __future__ import annotations

import logging
import sys

from app.operational_volume_v561 import _schedule_route_migration
from app import prediction_lab_files_v55 as lab_files

logger = logging.getLogger(__name__)
_installed = False


def _volume_authoritative_and_schedule_retirement() -> bool:
    """Return the permanent file-backed state and start route cleanup when possible."""
    _schedule_route_migration()
    return True


async def _migration_already_retired() -> bool:
    """Tell the sentinel loop that operational telemetry migration is complete."""
    _volume_authoritative_and_schedule_retirement()
    return True


async def _retired_prediction_lab_migration(*args, **kwargs):
    """Compatibility no-op for callers that still reference the old migrator."""
    del args, kwargs
    return {
        "verified": True,
        "legacy_collections_dropped": False,
        "runtime_writes_disabled": True,
        "storage": "persistent-volume",
    }


def install_legacy_mongo_retirement_v562() -> None:
    global _installed
    if _installed:
        return

    # This import-time call is opportunistic. If no asyncio loop exists yet the
    # v5.6.1 scheduler safely does nothing; the migration_verified wrapper below
    # calls it again from the live sentinel event loop.
    _schedule_route_migration()

    # New operational evidence is permanently file-backed. Any compatibility
    # check also starts retirement of the reconstructable route collection from
    # the running loop, so cleanup cannot depend on a later aircraft sample.
    lab_files.migration_verified = _volume_authoritative_and_schedule_retirement
    lab_files.migrate_prediction_lab_mongo = _retired_prediction_lab_migration

    sentinel = sys.modules.get("app.sentinel_network")
    if sentinel is not None:
        setattr(sentinel, "migration_verified", _volume_authoritative_and_schedule_retirement)
        setattr(sentinel, "_ensure_migration", _migration_already_retired)
        setattr(sentinel, "migrate_prediction_lab_mongo", _retired_prediction_lab_migration)

    _installed = True
    logger.info("Legacy operational Mongo migration retries retired; volume-backed telemetry remains authoritative")
