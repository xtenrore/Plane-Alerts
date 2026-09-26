"""Authenticated, bounded Prediction Lab repository sync bridge for v5.6.10+.

The bridge is deliberately outside the alert-critical path. It exports only sanitized
raw Prediction Lab evidence and deletes an evidence file only after an authenticated
caller presents the exact repository-acknowledged path, size and SHA-256.

v5.6.11 makes export resilient to isolated malformed/legacy/sensitive raw objects:
those objects remain untouched on the volume and no longer block valid evidence behind
them from being synchronized. v5.7.1 promotes that verified behavior unchanged as the
cumulative Bug Fixes Update.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from app.prediction_lab_files_v55 import SCHEMA_VERSION, root_path

BRIDGE_VERSION = "5.7.1"
MAX_BATCH_FILES = 500
MAX_BATCH_BYTES = 25 * 1024 * 1024
MAX_SCAN_FILES = 5000
SYNC_GUARD_TTL_S = 20 * 60.0
_SYNC_GUARD = Path(tempfile.gettempdir()) / "plane-alerts-prediction-lab-sync.guard"
_SECRET_PATTERN = re.compile(
    r"(mongodb\+srv://|telegram[_-]?token|authorization\s*[=:]|api[_-]?key\s*[=:]|password\s*[=:])",
    re.IGNORECASE,
)
_bridge_lock = threading.Lock()


class SyncBridgeError(RuntimeError):
    """Raised when a requested sync operation would violate the bridge contract."""


@dataclass(frozen=True)
class EvidenceItem:
    relative: str
    bytes: int
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {"relative": self.relative, "bytes": self.bytes, "sha256": self.sha256}


def _base(root: Path | None = None) -> Path:
    return (root or root_path()).resolve()


def _raw_root(root: Path | None = None) -> Path:
    return (_base(root) / "raw").resolve()


def _safe_raw_path(relative: str, *, root: Path | None = None) -> Path:
    rel = str(relative or "").replace("\\", "/").strip()
    posix = PurePosixPath(rel)
    if posix.is_absolute() or not rel.startswith("raw/") or ".." in posix.parts:
        raise SyncBridgeError(f"invalid raw evidence path: {relative!r}")
    base = _base(root)
    raw = _raw_root(root)
    path = (base / Path(*posix.parts)).resolve()
    if path == raw or raw not in path.parents:
        raise SyncBridgeError(f"raw evidence path escaped Prediction Lab root: {relative!r}")
    return path


def _validate_payload(path: Path) -> bytes:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SyncBridgeError(f"could not read evidence {path.name}") from exc
    if _SECRET_PATTERN.search(text):
        raise SyncBridgeError(f"credential-like material rejected: {path.name}")
    try:
        if path.suffix == ".json":
            payload = json.loads(text)
            if not isinstance(payload, dict) or payload.get("schema") != SCHEMA_VERSION:
                raise SyncBridgeError(f"unsupported evidence schema: {path.name}")
        elif path.suffix == ".ndjson":
            rows = 0
            for line in text.splitlines():
                if not line.strip():
                    continue
                payload = json.loads(line)
                if not isinstance(payload, dict) or payload.get("schema") != SCHEMA_VERSION:
                    raise SyncBridgeError(f"unsupported evidence schema in bundle: {path.name}")
                rows += 1
            if rows == 0:
                raise SyncBridgeError(f"empty evidence bundle rejected: {path.name}")
        else:
            raise SyncBridgeError(f"unsupported evidence extension: {path.name}")
    except json.JSONDecodeError as exc:
        raise SyncBridgeError(f"invalid evidence JSON: {path.name}") from exc
    return raw


def _rejection_class(exc: SyncBridgeError) -> str:
    message = str(exc).casefold()
    if "credential-like" in message:
        return "credential_like"
    if "unsupported evidence schema" in message:
        return "schema"
    if "empty evidence bundle" in message:
        return "empty_bundle"
    if "invalid evidence json" in message:
        return "invalid_json"
    if "could not read evidence" in message:
        return "read_error"
    return "validation_error"


def _iter_candidates(raw: Path) -> Iterable[Path]:
    if not raw.exists():
        return
    for dirpath, dirnames, filenames in os.walk(raw, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        parent = Path(dirpath)
        for name in sorted(filenames):
            if name.startswith(".") or name.endswith(".synced"):
                continue
            if not (name.endswith(".json") or name.endswith(".ndjson")):
                continue
            candidate = parent / name
            if candidate.is_symlink():
                continue
            yield candidate


def _touch_sync_guard() -> None:
    _SYNC_GUARD.write_text(str(time.time()), encoding="ascii")


def clear_sync_guard() -> None:
    _SYNC_GUARD.unlink(missing_ok=True)


def sync_guard_active() -> bool:
    try:
        age = time.time() - _SYNC_GUARD.stat().st_mtime
    except OSError:
        return False
    if age < 0 or age <= SYNC_GUARD_TTL_S:
        return True
    clear_sync_guard()
    return False


def build_batch_zip(
    *,
    root: Path | None = None,
    max_files: int = MAX_BATCH_FILES,
    max_bytes: int = MAX_BATCH_BYTES,
) -> tuple[Path, dict[str, Any]]:
    """Build a bounded validated ZIP in ephemeral storage without mutating the spool.

    Invalid/legacy/sensitive objects are deliberately left untouched and skipped so
    one bad raw object cannot prevent valid evidence behind it from being archived.
    """
    count_limit = max(1, min(MAX_BATCH_FILES, int(max_files)))
    byte_limit = max(1, min(MAX_BATCH_BYTES, int(max_bytes)))
    base = _base(root)
    raw_root = _raw_root(root)
    selected: list[tuple[Path, EvidenceItem, bytes]] = []
    total = 0
    scanned = 0
    deferred_for_batch_limit = 0
    rejected: Counter[str] = Counter()

    with _bridge_lock:
        _touch_sync_guard()
        try:
            for path in _iter_candidates(raw_root):
                if len(selected) >= count_limit or scanned >= MAX_SCAN_FILES:
                    break
                scanned += 1
                resolved = path.resolve()
                if raw_root not in resolved.parents:
                    rejected["path_escape"] += 1
                    continue
                try:
                    size = int(resolved.stat().st_size)
                except OSError:
                    rejected["stat_error"] += 1
                    continue
                if size > byte_limit:
                    rejected["oversized"] += 1
                    continue
                if selected and total + size > byte_limit:
                    deferred_for_batch_limit += 1
                    continue
                try:
                    data = _validate_payload(resolved)
                except SyncBridgeError as exc:
                    rejected[_rejection_class(exc)] += 1
                    continue
                digest = hashlib.sha256(data).hexdigest()
                relative = resolved.relative_to(base).as_posix()
                selected.append((resolved, EvidenceItem(relative=relative, bytes=len(data), sha256=digest), data))
                total += len(data)
        except Exception:
            clear_sync_guard()
            raise

        if not selected and rejected:
            summary=",".join(f"{key}:{rejected[key]}" for key in sorted(rejected))
            clear_sync_guard()
            raise SyncBridgeError(
                f"no exportable raw evidence in bounded scan; scanned={scanned}; rejected={summary}"
            )

        fd, name = tempfile.mkstemp(prefix="plane-alerts-sync-", suffix=".zip")
        os.close(fd)
        archive = Path(name)
        manifest = {
            "schema": "plane-alerts-prediction-sync-batch-v1",
            "bridge_version": BRIDGE_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "files": [item.as_dict() for _, item, _ in selected],
            "total_files": len(selected),
            "total_bytes": total,
            "scanned_files": scanned,
            "rejected_files": sum(rejected.values()),
            "rejected_by_reason": dict(sorted(rejected.items())),
            "deferred_for_batch_limit": deferred_for_batch_limit,
        }
        try:
            with zipfile.ZipFile(archive, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
                bundle.writestr("manifest.json", json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
                for _, item, data in selected:
                    bundle.writestr(item.relative, data)
        except Exception:
            archive.unlink(missing_ok=True)
            clear_sync_guard()
            raise
        return archive, manifest


def acknowledge(items: list[dict[str, Any]], *, root: Path | None = None) -> dict[str, Any]:
    """Delete only exact raw files whose bytes match repository acknowledgement."""
    if len(items) > MAX_BATCH_FILES:
        raise SyncBridgeError(f"acknowledgement exceeds maximum file count {MAX_BATCH_FILES}")
    verified: list[tuple[Path, EvidenceItem]] = []
    base = _base(root)

    with _bridge_lock:
        try:
            for raw_item in items:
                if not isinstance(raw_item, dict):
                    raise SyncBridgeError("acknowledgement item must be an object")
                relative = str(raw_item.get("relative") or "")
                expected_sha = str(raw_item.get("sha256") or "").lower()
                try:
                    expected_bytes = int(raw_item.get("bytes"))
                except (TypeError, ValueError) as exc:
                    raise SyncBridgeError(f"invalid byte count for {relative!r}") from exc
                if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
                    raise SyncBridgeError(f"invalid SHA-256 for {relative!r}")
                path = _safe_raw_path(relative, root=base)
                if not path.is_file() or path.is_symlink():
                    raise SyncBridgeError(f"acknowledged evidence is missing: {relative}")
                data = path.read_bytes()
                if len(data) != expected_bytes or hashlib.sha256(data).hexdigest() != expected_sha:
                    raise SyncBridgeError(f"acknowledged evidence changed before deletion: {relative}")
                verified.append((path, EvidenceItem(relative, expected_bytes, expected_sha)))

            removed_files = 0
            removed_bytes = 0
            for path, item in verified:
                # Recheck immediately before each destructive operation. A mismatch
                # leaves that file intact rather than treating acknowledgement as a wildcard.
                data = path.read_bytes()
                if len(data) != item.bytes or hashlib.sha256(data).hexdigest() != item.sha256:
                    raise SyncBridgeError(f"acknowledged evidence changed during deletion: {item.relative}")
                path.unlink()
                removed_files += 1
                removed_bytes += item.bytes
        finally:
            clear_sync_guard()

    try:
        free = int(shutil.disk_usage(base).free)
    except OSError:
        free = -1
    return {
        "bridge_version": BRIDGE_VERSION,
        "removed_files": removed_files,
        "removed_bytes": removed_bytes,
        "free_bytes": free,
    }


def status(*, root: Path | None = None) -> dict[str, Any]:
    base = _base(root)
    try:
        usage = shutil.disk_usage(base)
        total_bytes, used_bytes, free_bytes = int(usage.total), int(usage.used), int(usage.free)
    except OSError:
        total_bytes = used_bytes = free_bytes = -1
    return {
        "bridge_version": BRIDGE_VERSION,
        "root": str(base),
        "raw_exists": (_raw_root(root)).exists(),
        "sync_guard_active": sync_guard_active(),
        "total_bytes": total_bytes,
        "used_bytes": used_bytes,
        "free_bytes": free_bytes,
    }
