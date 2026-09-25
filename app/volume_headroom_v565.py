"""Plane Alerts persistent-volume headroom guard.

Normal cleanup may delete only repository-acknowledged ``*.synced`` evidence.
Emergency v5.6.6 recovery additionally removes an *unverified* historical
``archive/mongo-import`` export when the volume is critically full. That archive
contains only sanitized legacy operational telemetry and is authoritative only
when its own manifest says ``verified: true``. Raw unsynced evidence, SQLite
route/notification stores, state files and application/user data are never
emergency-cleanup candidates.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_ROOT = "/data/prediction_lab"
TARGET_FREE_BYTES = 128 * 1024 * 1024
CRITICAL_FREE_BYTES = 32 * 1024 * 1024
MAX_PRUNE_FILES = 100_000


def root_path() -> Path:
    raw = os.getenv("PREDICTION_LAB_ROOT", DEFAULT_ROOT).strip() or DEFAULT_ROOT
    return Path(raw)


def _safe_stat_size(path: Path) -> int:
    try:
        return int(path.stat().st_size) if path.is_file() else 0
    except OSError:
        return 0


def _tree_stats(path: Path) -> tuple[int, int]:
    files = 0
    total = 0
    if not path.exists():
        return files, total
    try:
        iterator = path.rglob("*")
    except OSError:
        return files, total
    for item in iterator:
        try:
            if item.is_file():
                files += 1
                total += int(item.stat().st_size)
        except OSError:
            continue
    return files, total


def volume_inventory(*, root: Path | None = None, largest_limit: int = 12) -> dict[str, Any]:
    """Return a read-only size inventory suitable for production diagnostics."""
    base = root or root_path()
    categories: dict[str, dict[str, int]] = {}
    for name, path in (
        ("raw", base / "raw"),
        ("archive", base / "archive"),
        ("runtime", base / "runtime"),
        ("state", base / "state"),
    ):
        files, total = _tree_stats(path)
        categories[name] = {"files": files, "bytes": total}

    largest: list[tuple[int, str]] = []
    if base.exists():
        for item in base.rglob("*"):
            try:
                if item.is_file():
                    largest.append((int(item.stat().st_size), str(item.relative_to(base))))
            except OSError:
                continue
    largest.sort(reverse=True)
    try:
        usage = shutil.disk_usage(base)
        disk = {"total": int(usage.total), "used": int(usage.used), "free": int(usage.free)}
    except OSError:
        disk = {"total": -1, "used": -1, "free": -1}
    return {
        "root": str(base),
        "disk": disk,
        "categories": categories,
        "largest_files": [{"path": path, "bytes": size} for size, path in largest[: max(1, int(largest_limit))]],
    }


def _archive_manifest_verified(base: Path) -> bool:
    manifest = base / "archive" / "mongo-import" / "manifest.json"
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and value.get("verified") is True


def purge_unverified_legacy_export(*, root: Path | None = None) -> dict[str, Any]:
    """Delete only an incomplete legacy operational Mongo export.

    The archive is never touched when its manifest is verified. It contains no
    user/profile/location/config collections; those were never Prediction Lab
    migration inputs.
    """
    base = root or root_path()
    archive = base / "archive" / "mongo-import"
    if not archive.exists():
        return {"removed": False, "files": 0, "bytes": 0, "reason": "archive_missing"}
    if _archive_manifest_verified(base):
        return {"removed": False, "files": 0, "bytes": 0, "reason": "verified_archive_protected"}

    files, total = _tree_stats(archive)
    try:
        shutil.rmtree(archive)
        archive.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("prediction_lab_unverified_archive_cleanup_failed error=%s", type(exc).__name__)
        return {"removed": False, "files": files, "bytes": total, "reason": type(exc).__name__}

    logger.warning(
        "prediction_lab_unverified_archive_removed files=%d bytes=%d user_data_untouched=true runtime_sqlite_untouched=true raw_unsynced_untouched=true",
        files,
        total,
    )
    return {"removed": True, "files": files, "bytes": total, "reason": "unverified_legacy_export"}


def _synced_candidates(base: Path) -> list[tuple[float, int, Path]]:
    candidates: list[tuple[float, int, Path]] = []
    for parent in (base / "raw", base / "archive" / "mongo-import"):
        if not parent.exists():
            continue
        for path in parent.rglob("*.synced"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if path.is_file():
                candidates.append((float(stat.st_mtime), int(stat.st_size), path))
    candidates.sort(key=lambda item: (item[0], str(item[2])))
    return candidates


def _remove_empty_parents(path: Path, stop: Path) -> None:
    parent = path.parent
    while parent != stop and stop in parent.parents:
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent


def prune_acknowledged_evidence(
    *,
    root: Path | None = None,
    target_free_bytes: int = TARGET_FREE_BYTES,
    max_files: int = MAX_PRUNE_FILES,
) -> dict[str, Any]:
    """Recover volume headroom by deleting only repository-acknowledged evidence."""
    base = root or root_path()
    if not base.exists():
        return {
            "root": str(base),
            "removed_files": 0,
            "removed_bytes": 0,
            "free_before": None,
            "free_after": None,
            "target_free_bytes": int(target_free_bytes),
            "reason": "root_missing",
        }

    try:
        free_before = int(shutil.disk_usage(base).free)
    except OSError:
        free_before = None

    target = max(0, int(target_free_bytes))
    limit = max(1, int(max_files))
    if free_before is not None and free_before >= target:
        return {
            "root": str(base),
            "removed_files": 0,
            "removed_bytes": 0,
            "free_before": free_before,
            "free_after": free_before,
            "target_free_bytes": target,
            "reason": "headroom_ok",
        }

    removed_files = 0
    removed_bytes = 0
    for _mtime, size, path in _synced_candidates(base):
        if removed_files >= limit:
            break
        try:
            path.unlink()
        except OSError:
            continue
        removed_files += 1
        removed_bytes += max(0, int(size))
        _remove_empty_parents(path, base)
        if free_before is not None and free_before + removed_bytes >= target:
            break

    try:
        free_after = int(shutil.disk_usage(base).free)
    except OSError:
        free_after = None

    if removed_files:
        logger.warning(
            "prediction_lab_volume_headroom_recovered removed_files=%d removed_bytes=%d free_before=%s free_after=%s unsynced_untouched=true",
            removed_files,
            removed_bytes,
            free_before,
            free_after,
        )
    elif free_before is not None and free_before < target:
        logger.error(
            "prediction_lab_volume_headroom_low free_bytes=%d target_bytes=%d no_acknowledged_files_available=true unsynced_untouched=true",
            free_before,
            target,
        )

    return {
        "root": str(base),
        "removed_files": removed_files,
        "removed_bytes": removed_bytes,
        "free_before": free_before,
        "free_after": free_after,
        "target_free_bytes": target,
        "reason": "pruned" if removed_files else "no_acknowledged_files",
    }


def recover_critical_headroom(*, root: Path | None = None) -> dict[str, Any]:
    """Run startup recovery before any operational SQLite store is opened."""
    base = root or root_path()
    before = volume_inventory(root=base)
    free_before = int(before["disk"].get("free", -1))
    archive_result: dict[str, Any] = {"removed": False, "files": 0, "bytes": 0, "reason": "not_needed"}

    if 0 <= free_before < CRITICAL_FREE_BYTES:
        archive_result = purge_unverified_legacy_export(root=base)

    synced_result = prune_acknowledged_evidence(root=base)
    after = volume_inventory(root=base)
    logger.warning(
        "prediction_lab_volume_inventory before=%s archive_cleanup=%s synced_cleanup=%s after=%s",
        before,
        archive_result,
        synced_result,
        after,
    )
    return {"before": before, "archive_cleanup": archive_result, "synced_cleanup": synced_result, "after": after}
