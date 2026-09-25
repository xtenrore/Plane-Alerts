"""Plane Alerts v5.6.5 persistent-volume headroom guard.

Only evidence files suffixed with ``.synced`` are eligible for automatic removal.
The Prediction Lab sync workflow creates that suffix only after the corresponding
content has been validated, committed and pushed to the evidence branch. Unsynced
evidence, SQLite route/notification stores, state files and application/user data
are never candidates.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_ROOT = "/data/prediction_lab"
TARGET_FREE_BYTES = 128 * 1024 * 1024
MAX_PRUNE_FILES = 100_000


def root_path() -> Path:
    raw = os.getenv("PREDICTION_LAB_ROOT", DEFAULT_ROOT).strip() or DEFAULT_ROOT
    return Path(raw)


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
