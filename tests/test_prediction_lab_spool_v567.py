from __future__ import annotations

import json
from collections import namedtuple
from pathlib import Path

from app import prediction_lab_spool_v567 as spool


Usage = namedtuple("Usage", "total used free")


def _usage(free: int) -> Usage:
    total = 500 * 1024 * 1024
    return Usage(total, total - free, free)


def test_v567_routine_prediction_is_shed_below_soft_reserve(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(spool.shutil, "disk_usage", lambda _path: _usage(64 * 1024 * 1024))
    allowed, reason, free = spool.should_write("prediction", root=tmp_path)
    assert allowed is False
    assert reason == "routine_backpressure"
    assert free == 64 * 1024 * 1024


def test_v567_outcome_survives_soft_pressure_above_critical_reserve(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(spool.shutil, "disk_usage", lambda _path: _usage(64 * 1024 * 1024))
    allowed, reason, _ = spool.should_write("outcome", root=tmp_path)
    assert allowed is True
    assert reason == "accepted"


def test_v567_all_optional_evidence_is_shed_below_critical_reserve(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(spool.shutil, "disk_usage", lambda _path: _usage(8 * 1024 * 1024))
    for kind in ("prediction", "outcome", "shadow_evaluation", "sentinel_poll"):
        allowed, reason, _ = spool.should_write(kind, root=tmp_path)
        assert allowed is False
        assert reason == "critical_reserve"


def test_v567_compaction_preserves_every_event_before_unlink(tmp_path: Path, monkeypatch) -> None:
    raw = tmp_path / "raw" / "2026-09-25"
    raw.mkdir(parents=True)
    expected = []
    for index in range(6):
        event = {"schema": "plane-alerts-prediction-evidence-v1", "event_id": f"evt-{index}", "kind": "prediction"}
        expected.append(event)
        (raw / f"evt-{index}.json").write_text(json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")
    (raw / "already.json.synced").write_text("acknowledged", encoding="utf-8")
    monkeypatch.setattr(spool.shutil, "disk_usage", lambda _path: _usage(128 * 1024 * 1024))

    result = spool.compact_unsynced_raw(root=tmp_path, force=True)

    assert result["compacted"] is True
    assert result["files"] == 6
    bundles = list(raw.glob("bundle-*.ndjson"))
    assert len(bundles) == 1
    restored = [json.loads(line) for line in bundles[0].read_text(encoding="utf-8").splitlines()]
    assert restored == expected
    assert not list(raw.glob("evt-*.json"))
    assert (raw / "already.json.synced").exists()


def test_v567_pressure_logging_is_rate_limited(monkeypatch) -> None:
    emitted = []
    monkeypatch.setattr(spool.logger, "warning", lambda *args, **kwargs: emitted.append((args, kwargs)))
    monkeypatch.setattr(spool.time, "monotonic", lambda: 1000.0)
    spool._last_pressure_log_at = 0.0
    spool.log_pressure("prediction", "critical_reserve", 0)
    spool.log_pressure("prediction", "critical_reserve", 0)
    assert len(emitted) == 1
