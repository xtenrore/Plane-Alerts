from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from app import prediction_lab_files_v55 as files
from app import prediction_lab_sync_bridge_v5610 as bridge
from app.prediction_lab_schema_guard import (
    LEGACY_EXPORT_SCHEMAS,
    RECOVERY_BRIDGE_VERSION,
    _validate_payload_compat,
    _write_evidence_with_envelope,
)

LEGACY_SCHEMA = "plane-alerts-shadow-evaluation-v52"


def _event(schema: str, event_id: str) -> dict:
    return {
        "schema": schema,
        "kind": "shadow_evaluation",
        "case_id": "case-backlog-recovery",
        "event_id": event_id,
        "captured_at": "2026-09-27T09:00:00Z",
        "model_id": "test-shadow",
    }


def test_new_legacy_shaped_event_is_wrapped_in_canonical_spool_schema(tmp_path: Path) -> None:
    path = _write_evidence_with_envelope(_event(LEGACY_SCHEMA, "evt-input"), root=tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["schema"] == files.SCHEMA_VERSION
    assert payload["source_schema"] == LEGACY_SCHEMA
    assert payload["case_id"] == "case-backlog-recovery"
    assert payload["event_id"]


def test_known_legacy_schema_is_explicit_and_narrow() -> None:
    assert LEGACY_EXPORT_SCHEMAS == frozenset({LEGACY_SCHEMA})
    assert RECOVERY_BRIDGE_VERSION == "5.7.2"


def test_legacy_json_is_exportable_without_rewriting_source_bytes(tmp_path: Path) -> None:
    path = tmp_path / "legacy.json"
    raw = (json.dumps(_event(LEGACY_SCHEMA, "evt-legacy"), sort_keys=True) + "\n").encode()
    path.write_bytes(raw)

    assert _validate_payload_compat(path) == raw
    assert path.read_bytes() == raw


def test_mixed_canonical_and_legacy_ndjson_bundle_is_exportable(tmp_path: Path) -> None:
    rows = [
        _event(files.SCHEMA_VERSION, "evt-current"),
        _event(LEGACY_SCHEMA, "evt-legacy"),
    ]
    path = tmp_path / "bundle.ndjson"
    raw = b"".join((json.dumps(row, sort_keys=True) + "\n").encode() for row in rows)
    path.write_bytes(raw)

    assert _validate_payload_compat(path) == raw


def test_unknown_schema_remains_rejected(tmp_path: Path) -> None:
    path = tmp_path / "unknown.json"
    path.write_text(json.dumps(_event("not-an-approved-schema", "evt-bad")), encoding="utf-8")

    with pytest.raises(bridge.SyncBridgeError, match="unsupported evidence schema"):
        _validate_payload_compat(path)


def test_missing_stable_identity_remains_rejected(tmp_path: Path) -> None:
    payload = _event(LEGACY_SCHEMA, "evt-legacy")
    payload.pop("case_id")
    path = tmp_path / "missing-id.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(bridge.SyncBridgeError, match="missing evidence identity"):
        _validate_payload_compat(path)


def test_credential_like_content_remains_rejected(tmp_path: Path) -> None:
    payload = _event(LEGACY_SCHEMA, "evt-secret")
    payload["diagnostic"] = "api_key=must-not-export"
    path = tmp_path / "secret.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(bridge.SyncBridgeError, match="credential-like"):
        _validate_payload_compat(path)


def test_bridge_batch_can_archive_legacy_bundle_with_exact_hash_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw_dir = tmp_path / "raw" / "2026-09-27"
    raw_dir.mkdir(parents=True)
    source = raw_dir / "bundle.ndjson"
    source_bytes = b"".join(
        (json.dumps(row, sort_keys=True) + "\n").encode()
        for row in (
            _event(files.SCHEMA_VERSION, "evt-current"),
            _event(LEGACY_SCHEMA, "evt-legacy"),
        )
    )
    source.write_bytes(source_bytes)

    monkeypatch.setattr(bridge, "_validate_payload", _validate_payload_compat)
    monkeypatch.setattr(bridge, "BRIDGE_VERSION", RECOVERY_BRIDGE_VERSION)
    archive, manifest = bridge.build_batch_zip(root=tmp_path, max_files=10, max_bytes=1024 * 1024)
    try:
        assert manifest["bridge_version"] == RECOVERY_BRIDGE_VERSION
        assert manifest["total_files"] == 1
        assert manifest["rejected_files"] == 0
        with zipfile.ZipFile(archive, "r") as bundle:
            assert bundle.read("raw/2026-09-27/bundle.ndjson") == source_bytes
    finally:
        archive.unlink(missing_ok=True)
        bridge.clear_sync_guard()


def test_workflow_accepts_only_canonical_plus_known_legacy_and_chains_small_batches() -> None:
    workflow = Path(".github/workflows/prediction-lab-sync.yml").read_text(encoding="utf-8")

    assert 'REQUIRED_BRIDGE_VERSION: "5.7.2"' in workflow
    assert "'plane-alerts-prediction-evidence-v1'" in workflow
    assert "'plane-alerts-shadow-evaluation-v52'" in workflow
    assert 'continue_drain={"true" if removed > 0 else "false"}' in workflow
    assert "removed >= 250" not in workflow


def test_production_worker_installs_schema_guard() -> None:
    worker_init = Path("app/worker/__init__.py").read_text(encoding="utf-8")
    assert "install_prediction_lab_schema_guard" in worker_init
    assert "install_prediction_lab_schema_guard()" in worker_init
