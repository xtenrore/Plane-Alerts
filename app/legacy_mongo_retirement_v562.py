"""Plane Alerts v5.6.2 legacy operational Mongo retirement guard.

The authoritative v5.6.1 storage split keeps high-volume route history and
Prediction Lab evidence on the Plane Alerts persistent volume. This guard makes
that boundary permanent at runtime: it starts retirement of the reconstructable
legacy route collection immediately and prevents the sentinel loop from
re-exporting already-retired Prediction Lab telemetry from Mongo.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from app.operational_volume_v561 import _schedule_route_migration
from app import prediction_lab_files_v55 as lab_files

logger = logging.getLogger(__name__)
_installed = False


async def _migration_already_retired() -> bool:
    """Tell the sentinel loop that operational telemetry migration is complete.

    New Prediction Lab writes are file-backed and must never fall back to Mongo.
    Historical cleanup is deliberately decoupled from live sentinel polling so a
    slow/full Atlas cluster cannot generate repeated export timeouts.
    """
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

    # Start retirement of the reconstructable flight_route_samples collection at
    # process startup instead of waiting for the first route sample. The v5.6.1
    # migrator writes any readable recent history to the volume, then drops only
    # that explicitly non-user collection. It never touches users/locations/
    # profiles/preferences/settings.
    _schedule_route_migration()

    # The file spool is already authoritative. Never retry the old Prediction
    # Lab Mongo export from the live sentinel loop, because Atlas quota/latency
    # must not affect shadow collection or flood production logs.
    lab_files.migrate_prediction_lab_mongo = _retired_prediction_lab_migration

    sentinel = sys.modules.get("app.sentinel_network")
    if sentinel is not None:
        setattr(sentinel, "_ensure_migration", _migration_already_retired)
        setattr(sentinel, "migrate_prediction_lab_mongo", _retired_prediction_lab_migration)

    _installed = True
    logger.info("Legacy operational Mongo migration retries retired; volume-backed telemetry remains authoritative")
