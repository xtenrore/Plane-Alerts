"""Lossless Prediction Lab schema compatibility guard.

Production v5.8.7 can still contain raw evidence written before the spool envelope
contract was enforced. In particular, v5.2 shadow evaluation rows carry
``plane-alerts-shadow-evaluation-v52`` as their top-level schema. The v5.5 file
writer preserved that caller-supplied value via ``setdefault``; v5.6.7 then
legitimately compacted those rows into NDJSON bundles. The repository sync bridge
therefore rejected otherwise safe bundles as ``schema`` and could not reclaim the
persistent volume.

This guard has two deliberately narrow responsibilities:

* New evidence always uses the canonical repository envelope schema. If a caller
  supplies a different internal record schema it is retained as ``source_schema``.
* The authenticated bridge may export the one known historical shadow-evaluation
  schema *as-is*. Hash, path, extension, UTF-8/JSON and credential checks remain
  unchanged, and acknowledgement still deletes only exact repository-verified
  source bytes.

No trajectory, CPA, ETA, route qualification or notification behavior is touched.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app import prediction_lab_files_v55 as _files
from app import prediction_lab_sync_bridge_v5610 as _bridge

RECOVERY_BRIDGE_VERSION = "5.7.2"
LEGACY_EXPORT_SCHEMAS = frozenset({"plane-alerts-shadow-evaluation-v52"})
ALLOWED_EXPORT_SCHEMAS = frozenset({_files.SCHEMA_VERSION, *LEGACY_EXPORT_SCHEMAS})

_installed = False
_original_write_evidence = _files.write_evidence


def _write_evidence_with_envelope(doc: dict[str, Any], *, root: Path | None = None) -> Path:
    normalized = dict(doc)
    supplied = normalized.get("schema")
    if supplied and str(supplied) != _files.SCHEMA_VERSION:
        normalized.setdefault("source_schema", str(supplied))
    normalized["schema"] = _files.SCHEMA_VERSION
    return _original_write_evidence(normalized, root=root)


def _validate_payload_compat(path: Path) -> bytes:
    """Validate canonical or explicitly-known legacy evidence without rewriting it."""
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _bridge.SyncBridgeError(f"could not read evidence {path.name}") from exc

    # Keep the existing bridge's credential rejection exactly authoritative.
    if _bridge._SECRET_PATTERN.search(text):
        raise _bridge.SyncBridgeError(f"credential-like material rejected: {path.name}")

    def validate_row(payload: Any, *, bundled: bool) -> None:
        if not isinstance(payload, dict) or payload.get("schema") not in ALLOWED_EXPORT_SCHEMAS:
            location = " in bundle" if bundled else ""
            raise _bridge.SyncBridgeError(f"unsupported evidence schema{location}: {path.name}")
        # Every spool event written by the file-backed Prediction Lab has stable
        # identities. Requiring them prevents a generic JSON file with a coincidentally
        # allowed schema string from being treated as repository evidence.
        if not str(payload.get("case_id") or "").strip() or not str(payload.get("event_id") or "").strip():
            location = " in bundle" if bundled else ""
            raise _bridge.SyncBridgeError(f"missing evidence identity{location}: {path.name}")

    try:
        if path.suffix == ".json":
            validate_row(json.loads(text), bundled=False)
        elif path.suffix == ".ndjson":
            rows = 0
            for line in text.splitlines():
                if not line.strip():
                    continue
                validate_row(json.loads(line), bundled=True)
                rows += 1
            if rows == 0:
                raise _bridge.SyncBridgeError(f"empty evidence bundle rejected: {path.name}")
        else:
            raise _bridge.SyncBridgeError(f"unsupported evidence extension: {path.name}")
    except json.JSONDecodeError as exc:
        raise _bridge.SyncBridgeError(f"invalid evidence JSON: {path.name}") from exc
    return raw


def install_prediction_lab_schema_guard() -> None:
    global _installed
    if _installed:
        return

    # append_evidence() resolves write_evidence from its module globals at call time,
    # so replacing that single symbol also covers all existing imported callers.
    _files.write_evidence = _write_evidence_with_envelope

    # build_batch_zip() and status() resolve these bridge globals at execution time.
    # Bump the handshake so the GitHub workflow cannot mistake the old deployed
    # bridge for this recovery-capable runtime during a main-branch rollout.
    _bridge._validate_payload = _validate_payload_compat
    _bridge.BRIDGE_VERSION = RECOVERY_BRIDGE_VERSION
    _installed = True
