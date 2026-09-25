"""File-backed Prediction Lab evidence spool for Plane Alerts v5.5.

The live prediction path never waits for GitHub or network synchronization. Evidence
is written to a Railway/self-host persistent volume in small atomic files. A separate
GitHub Actions sync job moves completed files to the prediction-lab-data branch.
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import os
import secrets
import tempfile
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from app.prediction_lab_spool_v567 import compact_unsynced_raw, log_pressure, pressure_snapshot, should_write

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "plane-alerts-prediction-evidence-v1"
MIGRATION_VERSION = "v5.5.0-file-backed-prediction-lab"
LAB_COLLECTIONS = (
    "prediction_lab_audit",
    "prediction_shadow_evaluations",
    "prediction_sentinel_routes",
)
DEFAULT_ROOT = "/data/prediction_lab"
MAX_ARCHIVE_CHUNK_RECORDS = 250
MAX_ARCHIVE_CHUNK_BYTES = 2 * 1024 * 1024

_lock = threading.Lock()
_stats = {"written": 0, "deduplicated": 0, "write_failures": 0, "migrated_records": 0}


def root_path() -> Path:
    raw = os.getenv("PREDICTION_LAB_ROOT", DEFAULT_ROOT).strip() or DEFAULT_ROOT
    return Path(raw)


def _directories(root: Path | None = None) -> tuple[Path, ...]:
    base = root or root_path()
    return tuple(base / name for name in ("raw", "unchecked", "reviewed", "error_museum", "archive/mongo-import", "state", "schemas"))


def ensure_layout(root: Path | None = None) -> Path:
    base = root or root_path()
    for path in _directories(base):
        path.mkdir(parents=True, exist_ok=True)
    return base


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            dir_fd = os.open(path.parent, os.O_DIRECTORY)
        except (AttributeError, OSError):
            return
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        current = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (set, tuple)):
        return list(value)
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, default=_json_default, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _state_file(name: str, root: Path | None = None) -> Path:
    return ensure_layout(root) / "state" / name


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None


def _privacy_salt(root: Path | None = None) -> bytes:
    path = _state_file("privacy_salt", root)
    with _lock:
        try:
            value = path.read_bytes().strip()
            if len(value) >= 32:
                return value
        except OSError:
            pass
        value = secrets.token_hex(32).encode("ascii")
        _atomic_write(path, value + b"\n")
        return value


def observer_ref(user_id: Any, root: Path | None = None) -> str:
    salt = _privacy_salt(root)
    digest = hashlib.sha256(salt + b":" + str(user_id).encode("utf-8")).hexdigest()
    return f"observer-{digest[:20]}"


_SECRET_KEYS = ("password", "secret", "token", "authorization", "api_key", "apikey", "mongo_uri", "mongodb_uri")
_USER_KEYS = {"user_id", "target_user_id", "admin_user_id"}


def sanitize_for_repository(value: Any, *, root: Path | None = None) -> Any:
    """Remove credentials and direct user identifiers from repository evidence."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if any(marker in lowered for marker in _SECRET_KEYS):
                continue
            if lowered in _USER_KEYS:
                if raw_value is not None:
                    out["observer_ref"] = observer_ref(raw_value, root)
                continue
            if lowered == "expectation_key" and isinstance(raw_value, str):
                parts = raw_value.split(":", 2)
                if len(parts) == 3 and parts[0].lstrip("-").isdigit():
                    out[key] = f"{observer_ref(parts[0], root)}:{parts[1]}:{parts[2]}"
                else:
                    out[key] = hashlib.sha256(raw_value.encode("utf-8")).hexdigest()[:32]
                continue
            if key == "_id":
                out["source_record_id"] = str(raw_value)
                continue
            out[key] = sanitize_for_repository(raw_value, root=root)
        return out
    if isinstance(value, list):
        return [sanitize_for_repository(item, root=root) for item in value]
    if isinstance(value, tuple):
        return [sanitize_for_repository(item, root=root) for item in value]
    if isinstance(value, datetime):
        return _json_default(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def migration_state(root: Path | None = None) -> dict[str, Any]:
    return _read_json(_state_file("mongo_migration_v55.json", root)) or {}


def migration_verified(root: Path | None = None) -> bool:
    return bool(migration_state(root).get("verified"))


def _captured_day(doc: dict[str, Any]) -> str:
    raw = doc.get("captured_at") or doc.get("evaluated_at") or doc.get("snapshot_at")
    if isinstance(raw, str) and len(raw) >= 10:
        return raw[:10]
    if isinstance(raw, datetime):
        current = raw if raw.tzinfo is not None else raw.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).date().isoformat()
    return datetime.now(timezone.utc).date().isoformat()


def _stable_case_id(doc: dict[str, Any]) -> str:
    explicit = str(doc.get("case_id") or "").strip()
    if explicit:
        return explicit[:96]
    fields = (doc.get("kind"), doc.get("observer_ref"), doc.get("aircraft_icao24"), doc.get("callsign"), doc.get("expectation_key"), doc.get("evaluation_id"), doc.get("utc_date"))
    material = "|".join("" if value is None else str(value) for value in fields)
    if not material.replace("|", ""):
        material = hashlib.sha256(_canonical_json(doc)).hexdigest()
    return "case-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _stable_event_id(doc: dict[str, Any], case_id: str) -> str:
    explicit = str(doc.get("event_id") or "").strip()
    if explicit:
        return explicit[:96]
    material = _canonical_json({"case_id": case_id, "payload": doc})
    return "evt-" + hashlib.sha256(material).hexdigest()[:32]


def write_evidence(doc: dict[str, Any], *, root: Path | None = None) -> Path:
    """Atomically persist one sanitized raw evidence event with deterministic identity."""
    base = ensure_layout(root)
    clean = sanitize_for_repository(dict(doc), root=base)
    clean.setdefault("schema", SCHEMA_VERSION)
    clean.setdefault("pipeline_stage", "raw")
    clean.setdefault("captured_at", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    clean.setdefault("coverage_resolution", "inconclusive" if clean.get("coverage_missing") else "observed")
    clean.setdefault("shadow_only", False)
    case_id = _stable_case_id(clean)
    clean["case_id"] = case_id
    event_id = _stable_event_id(clean, case_id)
    clean["event_id"] = event_id
    day = _captured_day(clean)
    path = base / "raw" / day / f"{event_id}.json"
    payload = _canonical_json(clean)
    with _lock:
        if path.exists():
            try:
                if path.read_bytes() == payload:
                    _stats["deduplicated"] += 1
                    return path
            except OSError:
                pass
        try:
            _atomic_write(path, payload)
            _stats["written"] += 1
        except Exception:
            _stats["write_failures"] += 1
            raise
    return path


async def append_evidence(doc: dict[str, Any], *, root: Path | None = None) -> Path | None:
    base = root_path() if root is None else root
    kind = doc.get("kind")
    allowed, reason, free = await asyncio.to_thread(should_write, kind, root=base)
    if not allowed:
        # Compaction is evidence-preserving and bounded. At zero headroom it is a
        # no-op; once the repository sync frees a small amount of space it starts
        # turning thousands of tiny raw files into efficiently drainable NDJSON.
        await asyncio.to_thread(compact_unsynced_raw, root=base)
        log_pressure(kind, reason, free)
        return None
    try:
        return await asyncio.to_thread(write_evidence, doc, root=base)
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            await asyncio.to_thread(compact_unsynced_raw, root=base)
            log_pressure(kind, "enospc", 0)
            return None
        logger.exception("prediction_lab_file_write_failed kind=%s", kind)
        return None
    except Exception:
        logger.exception("prediction_lab_file_write_failed kind=%s", kind)
        return None


def spool_snapshot(root: Path | None = None) -> dict[str, Any]:
    base = root_path() if root is None else root
    state = migration_state(base)
    result = dict(_stats)
    result.update(pressure_snapshot())
    result.update({"root": str(base), "migration_verified": bool(state.get("verified")), "migration_dropped": bool(state.get("legacy_collections_dropped")), "migration_counts": state.get("collections", {})})
    return result


def _archive_chunk_path(root: Path, collection: str, index: int) -> Path:
    return root / "archive" / "mongo-import" / collection / f"part-{index:06d}.ndjson"


def _verify_chunk(path: Path, expected_count: int, expected_sha256: str) -> bool:
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return data.count(b"\n") == expected_count and hashlib.sha256(data).hexdigest() == expected_sha256


async def _export_collection(db: Any, collection_name: str, root: Path) -> dict[str, Any]:
    collection = db[collection_name]
    source_count = int(await collection.count_documents({}))
    cursor = collection.find({}).sort("_id", 1).batch_size(100)
    chunk_index = 0
    chunk_rows: list[bytes] = []
    chunk_bytes = 0
    exported = 0
    chunks: list[dict[str, Any]] = []

    async def flush() -> None:
        nonlocal chunk_index, chunk_rows, chunk_bytes
        if not chunk_rows:
            return
        data = b"".join(chunk_rows)
        path = _archive_chunk_path(root, collection_name, chunk_index)
        digest = hashlib.sha256(data).hexdigest()
        await asyncio.to_thread(_atomic_write, path, data)
        if not await asyncio.to_thread(_verify_chunk, path, len(chunk_rows), digest):
            raise RuntimeError(f"Prediction Lab archive verification failed for {collection_name} chunk {chunk_index}")
        chunks.append({"path": str(path.relative_to(root)).replace(os.sep, "/"), "records": len(chunk_rows), "bytes": len(data), "sha256": digest})
        chunk_index += 1
        chunk_rows = []
        chunk_bytes = 0

    async for raw in cursor:
        clean = sanitize_for_repository(raw, root=root)
        envelope = {"schema": SCHEMA_VERSION, "kind": "mongo_import", "source_collection": collection_name, "migration_version": MIGRATION_VERSION, "migrated_at": datetime.now(timezone.utc), "record": clean}
        encoded = _canonical_json(envelope)
        if chunk_rows and (len(chunk_rows) >= MAX_ARCHIVE_CHUNK_RECORDS or chunk_bytes + len(encoded) > MAX_ARCHIVE_CHUNK_BYTES):
            await flush()
        chunk_rows.append(encoded)
        chunk_bytes += len(encoded)
        exported += 1
    await flush()

    if exported != source_count:
        raise RuntimeError(f"Prediction Lab migration count mismatch for {collection_name}: source={source_count} exported={exported}")
    if sum(int(chunk["records"]) for chunk in chunks) != source_count:
        raise RuntimeError(f"Prediction Lab migration manifest mismatch for {collection_name}")
    return {"source_count": source_count, "exported_count": exported, "archive_bytes": sum(int(chunk["bytes"]) for chunk in chunks), "chunks": chunks, "verified": True}


async def _drop_legacy_collections(db: Any) -> bool:
    ok = True
    for collection_name in LAB_COLLECTIONS:
        try:
            await db[collection_name].drop()
        except Exception as exc:
            ok = False
            logger.warning("prediction_lab_legacy_drop_failed collection=%s error=%s", collection_name, type(exc).__name__)
    return ok


def prune_synced(*, root: Path | None = None, older_than_hours: float = 24.0, max_files: int = 200) -> int:
    """Delete only files explicitly acknowledged by the sync workflow."""
    base = ensure_layout(root)
    cutoff = datetime.now(timezone.utc).timestamp() - max(1.0, float(older_than_hours)) * 3600.0
    removed = 0
    for parent in (base / "raw", base / "archive" / "mongo-import"):
        if not parent.exists():
            continue
        for path in parent.rglob("*.synced"):
            if removed >= max(1, int(max_files)):
                return removed
            try:
                if path.stat().st_mtime <= cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
    return removed


async def migrate_prediction_lab_mongo(db: Any, *, root: Path | None = None) -> dict[str, Any]:
    """Export/verify legacy high-volume Mongo evidence, then retire only those collections."""
    base = ensure_layout(root)
    state_path = _state_file("mongo_migration_v55.json", base)
    previous = migration_state(base)
    if previous.get("verified"):
        dropped = await _drop_legacy_collections(db)
        if dropped and not previous.get("legacy_collections_dropped"):
            previous["legacy_collections_dropped"] = True
            previous["drop_verified_at"] = datetime.now(timezone.utc)
            await asyncio.to_thread(_atomic_write, state_path, _canonical_json(previous))
        return previous

    collections: dict[str, Any] = {}
    started = datetime.now(timezone.utc)
    for collection_name in LAB_COLLECTIONS:
        collections[collection_name] = await _export_collection(db, collection_name, base)
        _stats["migrated_records"] += int(collections[collection_name]["exported_count"])

    manifest = {
        "schema": SCHEMA_VERSION,
        "migration_version": MIGRATION_VERSION,
        "started_at": started,
        "verified_at": datetime.now(timezone.utc),
        "verified": True,
        "legacy_collections_dropped": False,
        "collections": collections,
        "normal_application_data_untouched": True,
    }
    manifest_path = base / "archive" / "mongo-import" / "manifest.json"
    await asyncio.to_thread(_atomic_write, manifest_path, _canonical_json(manifest))
    await asyncio.to_thread(_atomic_write, state_path, _canonical_json(manifest))

    dropped = await _drop_legacy_collections(db)
    manifest["legacy_collections_dropped"] = dropped
    manifest["drop_verified_at"] = datetime.now(timezone.utc) if dropped else None
    await asyncio.to_thread(_atomic_write, state_path, _canonical_json(manifest))
    await asyncio.to_thread(_atomic_write, manifest_path, _canonical_json(manifest))
    logger.info("prediction_lab_mongo_migration verified=true dropped=%s counts=%s", dropped, {name: details["source_count"] for name, details in collections.items()})
    return manifest