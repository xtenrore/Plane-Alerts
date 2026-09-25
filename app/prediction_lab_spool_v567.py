"""Plane Alerts v5.6.7 Prediction Lab spool pressure controls.

This module protects the alert-critical persistent volume without deleting unsynced
evidence. Routine analytical writes may be shed when free space is low, and existing
raw JSON evidence may be losslessly compacted into verified NDJSON bundles so the
repository sync can drain the backlog efficiently.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_ROOT = "/data/prediction_lab"
SOFT_FREE_BYTES = 96 * 1024 * 1024
CRITICAL_FREE_BYTES = 32 * 1024 * 1024
COMPACT_MAX_BYTES = 8 * 1024 * 1024
COMPACT_MAX_FILES = 2000
COMPACT_MIN_FILES = 2
COMPACT_INTERVAL_S = 60.0
LOG_INTERVAL_S = 300.0

# These are frequent sampling/coverage records. Outcomes and evaluations are kept
# while the volume remains above the critical reserve.
ROUTINE_KINDS = {
    "prediction",
    "sentinel_poll",
    "sentinel_route",
    "next60_shadow",
    "next60_expectation",
}

_lock = threading.Lock()
_last_compact_at = 0.0
_last_pressure_log_at = 0.0
_stats = {
    "shed_routine": 0,
    "shed_critical": 0,
    "compactions": 0,
    "compacted_files": 0,
    "compacted_bytes": 0,
}


def root_path(root: Path | None = None) -> Path:
    if root is not None:
        return root
    raw = os.getenv("PREDICTION_LAB_ROOT", DEFAULT_ROOT).strip() or DEFAULT_ROOT
    return Path(raw)


def free_bytes(root: Path | None = None) -> int:
    base = root_path(root)
    try:
        base.mkdir(parents=True, exist_ok=True)
        return int(shutil.disk_usage(base).free)
    except OSError:
        return -1


def pressure_snapshot() -> dict[str, int]:
    with _lock:
        return dict(_stats)


def should_write(kind: Any, *, root: Path | None = None) -> tuple[bool, str, int]:
    """Return whether optional evidence may consume disk at the current pressure."""
    free = free_bytes(root)
    if free < 0:
        # Unknown disk state is not proof of pressure. Let the normal write path
        # report the real filesystem error rather than silently losing evidence.
        return True, "disk_unknown", free
    if free < CRITICAL_FREE_BYTES:
        with _lock:
            _stats["shed_critical"] += 1
        return False, "critical_reserve", free
    if free < SOFT_FREE_BYTES and str(kind or "") in ROUTINE_KINDS:
        with _lock:
            _stats["shed_routine"] += 1
        return False, "routine_backpressure", free
    return True, "accepted", free


def log_pressure(kind: Any, reason: str, free: int) -> None:
    """Rate-limit pressure warnings so an optional telemetry outage cannot flood logs."""
    global _last_pressure_log_at
    now = time.monotonic()
    with _lock:
        if now - _last_pressure_log_at < LOG_INTERVAL_S:
            return
        _last_pressure_log_at = now
    logger.warning(
        "prediction_lab_backpressure kind=%s reason=%s free_bytes=%d soft_reserve=%d critical_reserve=%d",
        str(kind or ""),
        reason,
        free,
        SOFT_FREE_BYTES,
        CRITICAL_FREE_BYTES,
    )


def _atomic_bundle(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        if path.read_bytes() != payload:
            raise OSError("Prediction Lab compacted bundle verification failed")
        try:
            dir_fd = os.open(path.parent, os.O_DIRECTORY)
        except (AttributeError, OSError):
            return
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        tmp.unlink(missing_ok=True)


def _candidate_day(raw_root: Path) -> Path | None:
    try:
        days = sorted(path for path in raw_root.iterdir() if path.is_dir())
    except OSError:
        return None
    for day in days:
        try:
            for item in day.iterdir():
                if item.is_file() and item.name.endswith(".json") and not item.name.endswith(".synced"):
                    return day
        except OSError:
            continue
    return None


def compact_unsynced_raw(*, root: Path | None = None, force: bool = False) -> dict[str, Any]:
    """Losslessly replace many raw JSON files with one verified NDJSON bundle.

    The operation never includes acknowledged ``*.synced`` files. Originals are
    unlinked only after the complete bundle has been atomically written and read
    back byte-for-byte. A single invocation compacts one day directory and is
    bounded by both free-space headroom and fixed file/byte limits.
    """
    global _last_compact_at
    base = root_path(root)
    raw_root = base / "raw"
    now = time.monotonic()
    with _lock:
        if not force and now - _last_compact_at < COMPACT_INTERVAL_S:
            return {"compacted": False, "reason": "rate_limited", "files": 0, "bytes": 0}
        _last_compact_at = now

    free = free_bytes(base)
    if free <= 0:
        return {"compacted": False, "reason": "no_headroom", "files": 0, "bytes": 0}
    day = _candidate_day(raw_root)
    if day is None:
        return {"compacted": False, "reason": "no_raw_json", "files": 0, "bytes": 0}

    # Never duplicate more than one quarter of currently free space while making
    # a bundle. At least 64 KiB of temporary headroom is required.
    byte_budget = min(COMPACT_MAX_BYTES, max(0, free // 4))
    if byte_budget < 64 * 1024:
        return {"compacted": False, "reason": "insufficient_headroom", "files": 0, "bytes": 0}

    chosen: list[Path] = []
    chunks: list[bytes] = []
    total = 0
    try:
        candidates = sorted(
            (path for path in day.iterdir() if path.is_file() and path.name.endswith(".json") and not path.name.endswith(".synced")),
            key=lambda path: (path.stat().st_mtime, path.name),
        )
    except OSError:
        return {"compacted": False, "reason": "list_failed", "files": 0, "bytes": 0}

    for path in candidates:
        if len(chosen) >= COMPACT_MAX_FILES:
            break
        try:
            data = path.read_bytes()
            # Validate the source before it can participate in a destructive
            # replacement. The event remains unchanged inside the NDJSON line.
            json.loads(data.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not data.endswith(b"\n"):
            data += b"\n"
        if chosen and total + len(data) > byte_budget:
            break
        if len(data) > byte_budget:
            continue
        chosen.append(path)
        chunks.append(data)
        total += len(data)

    if len(chosen) < COMPACT_MIN_FILES:
        return {"compacted": False, "reason": "insufficient_candidates", "files": len(chosen), "bytes": total}

    payload = b"".join(chunks)
    digest = hashlib.sha256(payload).hexdigest()[:24]
    bundle = day / f"bundle-{digest}-{len(chosen):05d}.ndjson"
    if bundle.exists():
        try:
            if bundle.read_bytes() != payload:
                return {"compacted": False, "reason": "bundle_conflict", "files": 0, "bytes": 0}
        except OSError:
            return {"compacted": False, "reason": "bundle_read_failed", "files": 0, "bytes": 0}
    else:
        try:
            _atomic_bundle(bundle, payload)
        except OSError as exc:
            return {"compacted": False, "reason": type(exc).__name__, "files": 0, "bytes": 0}

    removed = 0
    for path in chosen:
        try:
            path.unlink()
            removed += 1
        except OSError:
            # A partial unlink is safe: duplicate source events simply remain
            # alongside the verified bundle and the repository sync can dedupe.
            continue

    with _lock:
        _stats["compactions"] += 1
        _stats["compacted_files"] += removed
        _stats["compacted_bytes"] += total
    logger.warning(
        "prediction_lab_spool_compacted files=%d bytes=%d bundle=%s free_bytes=%d",
        removed,
        total,
        bundle.name,
        free_bytes(base),
    )
    return {"compacted": True, "reason": "verified_bundle", "files": removed, "bytes": total, "bundle": str(bundle)}
