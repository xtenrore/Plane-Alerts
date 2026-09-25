"""Plane Alerts v5.6.3 permanent operational-storage policy.

High-volume/reconstructable telemetry belongs on the Plane Alerts persistent
volume. MongoDB remains for durable user/configuration/application state.
"""
from __future__ import annotations

import logging

from app import database
from app import legacy_mongo_retirement_v562 as legacy_retirement
from app import notification_volume_v563 as notification_volume
from app import prediction_lab_files_v55 as lab_files

logger = logging.getLogger(__name__)

FORBIDDEN_OPERATIONAL_MONGO_COLLECTIONS = frozenset({
    "flight_route_samples",
    "notification_history",
    "prediction_lab_audit",
    "prediction_shadow_evaluations",
    "prediction_sentinel_routes",
})

_installed = False
_original_ensure_indexes = None
_original_volume_authoritative = None


class _NoMongoOperationalIndexCollection:
    def __init__(self, name: str) -> None:
        self.name = name

    async def create_index(self, *args, **kwargs) -> str:
        del args, kwargs
        logger.debug("Skipped retired operational Mongo index collection=%s", self.name)
        return f"retired_{self.name}"


class _IndexPolicyDatabase:
    def __init__(self, db) -> None:
        self._db = db

    def __getitem__(self, name: str):
        key = str(name)
        if key in FORBIDDEN_OPERATIONAL_MONGO_COLLECTIONS:
            return _NoMongoOperationalIndexCollection(key)
        return self._db[key]

    def __getattr__(self, name: str):
        return getattr(self._db, name)


async def _ensure_indexes_without_operational_mongo(db) -> None:
    # Self-host SQLite remains a complete application database and should retain
    # its normal indexes. The split policy is specifically for the Railway Mongo
    # + persistent-volume architecture.
    if bool(getattr(db, "is_plane_alerts_sqlite", False)):
        await _original_ensure_indexes(db)
        return
    await _original_ensure_indexes(_IndexPolicyDatabase(db))


def _volume_authoritative_with_notification_retirement() -> bool:
    result = bool(_original_volume_authoritative())
    notification_volume.schedule_legacy_retirement()
    return result


def install_operational_storage_policy_v563() -> None:
    global _installed, _original_ensure_indexes, _original_volume_authoritative
    if _installed:
        return

    _original_ensure_indexes = database._ensure_indexes
    database._ensure_indexes = _ensure_indexes_without_operational_mongo

    # Any legacy accessor now resolves to the volume store. This protects older
    # call sites that use the named helper instead of importing the v5.6.3 store.
    database.notification_history_col = notification_volume.notification_history_collection

    _original_volume_authoritative = legacy_retirement._volume_authoritative_and_schedule_retirement
    legacy_retirement._volume_authoritative_and_schedule_retirement = _volume_authoritative_with_notification_retirement
    lab_files.migration_verified = _volume_authoritative_with_notification_retirement

    # sentinel_network is already imported by app.main before worker bootstrap.
    # Replace its compatibility check as well so the live event loop schedules
    # notification-history retirement even before the first Telegram delivery.
    try:
        from app import sentinel_network
        sentinel_network.migration_verified = _volume_authoritative_with_notification_retirement
    except Exception:
        logger.debug("sentinel storage-policy binding deferred", exc_info=True)

    notification_volume.schedule_legacy_retirement()
    _installed = True
    logger.info(
        "Operational storage policy v5.6.3 active mongo_forbidden=%s notification_store=%s",
        ",".join(sorted(FORBIDDEN_OPERATIONAL_MONGO_COLLECTIONS)),
        notification_volume.database_path(),
    )
