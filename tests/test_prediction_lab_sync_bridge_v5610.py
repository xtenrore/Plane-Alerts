from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from app.prediction_lab_sync_bridge_v5610 import (
    BRIDGE_VERSION,
    MAX_BATCH_BYTES,
    SyncBridgeError,
    acknowledge,
    build_batch_zip,
    clear_sync_guard,
    status,
)

SCHEMA = "plane-alerts-prediction-evidence-v1"


def _event(event_id: str) -> bytes:
    return (json.dumps({"schema": SCHEMA, "event_id": event_id, "case_id": "case-x"}, sort_keys=True) + "\n").encode()


def test_bridge_builds_bounded_hash_manifest_without_mutating_raw(tmp_path: Path) -> None:
    raw = tmp_path / "raw" / "2026-09-25"
    raw.mkdir(parents=True)
    one = raw / "one.json"; one.write_bytes(_event("one"))
    two = raw / "two.json"; two.write_bytes(_event("two"))

    archive, manifest = build_batch_zip(root=tmp_path, max_files=1, max_bytes=MAX_BATCH_BYTES)
    try:
        assert manifest["bridge_version"] == BRIDGE_VERSION
        assert manifest["total_files"] == 1
        assert len(list(raw.glob("*.json"))) == 2
        item = manifest["files"][0]
        source = tmp_path / item["relative"]
        assert item["bytes"] == source.stat().st_size
        assert item["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        with zipfile.ZipFile(archive) as bundle:
            assert set(bundle.namelist()) == {"manifest.json", item["relative"]}
            assert bundle.read(item["relative"]) == source.read_bytes()
    finally:
        archive.unlink(missing_ok=True)
        clear_sync_guard()


def test_bridge_ack_requires_exact_hash_and_frees_only_acknowledged_raw(tmp_path: Path) -> None:
    raw = tmp_path / "raw" / "2026-09-25"; raw.mkdir(parents=True)
    first = raw / "first.json"; first.write_bytes(_event("first"))
    second = raw / "second.json"; second.write_bytes(_event("second"))
    item = {
        "relative": first.relative_to(tmp_path).as_posix(),
        "bytes": first.stat().st_size,
        "sha256": hashlib.sha256(first.read_bytes()).hexdigest(),
    }
    result = acknowledge([item], root=tmp_path)
    assert result["removed_files"] == 1
    assert not first.exists()
    assert second.exists()


def test_bridge_ack_rejects_hash_mismatch_without_deleting(tmp_path: Path) -> None:
    raw = tmp_path / "raw" / "2026-09-25"; raw.mkdir(parents=True)
    path = raw / "event.json"; path.write_bytes(_event("event"))
    with pytest.raises(SyncBridgeError, match="changed before deletion"):
        acknowledge([{"relative": "raw/2026-09-25/event.json", "bytes": path.stat().st_size, "sha256": "0" * 64}], root=tmp_path)
    assert path.exists()


@pytest.mark.parametrize("relative", ["../route_history.sqlite", "/data/prediction_lab/raw/x.json", "state/x.json", "raw/../../x.json"])
def test_bridge_ack_cannot_escape_raw_tree(tmp_path: Path, relative: str) -> None:
    with pytest.raises(SyncBridgeError):
        acknowledge([{"relative": relative, "bytes": 1, "sha256": "0" * 64}], root=tmp_path)


def test_bridge_export_rejects_credential_like_payload(tmp_path: Path) -> None:
    raw = tmp_path / "raw" / "2026-09-25"; raw.mkdir(parents=True)
    (raw / "bad.json").write_text(json.dumps({"schema": SCHEMA, "password": "secret"}) + "\n", encoding="utf-8")
    with pytest.raises(SyncBridgeError, match="credential-like"):
        build_batch_zip(root=tmp_path)
    clear_sync_guard()


def test_bridge_validates_ndjson_rows(tmp_path: Path) -> None:
    raw = tmp_path / "raw" / "2026-09-25"; raw.mkdir(parents=True)
    bundle = raw / "bundle.ndjson"
    bundle.write_bytes(_event("a") + _event("b"))
    archive, manifest = build_batch_zip(root=tmp_path)
    try:
        assert manifest["total_files"] == 1
        assert manifest["files"][0]["relative"].endswith("bundle.ndjson")
    finally:
        archive.unlink(missing_ok=True)
        clear_sync_guard()


def test_bridge_status_reports_version_and_disk_headroom(tmp_path: Path) -> None:
    (tmp_path / "raw").mkdir()
    result = status(root=tmp_path)
    assert result["bridge_version"] == "5.6.10"
    assert result["raw_exists"] is True
    assert result["total_bytes"] > 0
    assert result["free_bytes"] >= 0
