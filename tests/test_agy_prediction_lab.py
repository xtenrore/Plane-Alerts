from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from app import agy_worker
from app.agy_console import _chunks


def test_quota_refresh_duration_adds_ten_minute_guard(monkeypatch):
    monkeypatch.setattr(agy_worker, "QUOTA_REFRESH_GUARD_SECONDS", 600)
    before = datetime.now(timezone.utc).timestamp()
    deadline = agy_worker._parse_refresh_deadline("Quota exceeded. Refreshes in 2 hours")
    assert deadline is not None
    delta = deadline.timestamp() - before
    assert 2 * 3600 + 590 <= delta <= 2 * 3600 + 620


def test_quota_refresh_unknown_text_never_schedules_automatic_retry(monkeypatch):
    monkeypatch.setattr(agy_worker, "QUOTA_REFRESH_GUARD_SECONDS", 600)
    assert agy_worker._parse_refresh_deadline("baseline quota exhausted") is None


def test_terminal_cleaner_removes_ansi_sequences():
    assert agy_worker._clean_terminal("\x1b[31mhello\x1b[0m\r\n") == "hello"


def test_telegram_console_chunks_never_exceed_limit():
    chunks = _chunks(["a" * 9000])
    assert len(chunks) >= 3
    assert all(len(chunk) <= 3500 for chunk in chunks)
    assert "".join(chunks) == "a" * 9000


def test_finding_recorder_persists_immediately(tmp_path: Path):
    env = os.environ.copy()
    env["AGY_STATE_DIR"] = str(tmp_path)
    env.pop("MONGO_URI", None)
    result = subprocess.run(
        [
            sys.executable,
            "scripts/agy_record_finding.py",
            "--severity",
            "high",
            "--summary",
            "test false alert",
            "--evidence",
            "predicted 3km, actual 30km",
            "--suggested-fix",
            "tighten route intent gate",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert "AGY_FINDING_JSON " in result.stdout
    inbox = tmp_path / "prediction-lab" / "findings.jsonl"
    rows = inbox.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    finding = json.loads(rows[0])
    assert finding["seq"] == 1
    assert finding["severity"] == "high"
    assert finding["status"] == "new"
