from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pymongo.errors import NetworkTimeout

from app import agy_prediction_bridge as bridge
from app import agy_tool_recovery_v431 as recovery
from app import agy_worker


def _entrypoint_python() -> str:
    text = Path("scripts/agy-worker-entrypoint.sh").read_text(encoding="utf-8")
    marker = "python - <<'PY'\n"
    start = text.index(marker) + len(marker)
    end = text.index("\nPY\n", start)
    return text[start:end]


@pytest.mark.parametrize(
    ("persisted", "force_token", "expected_deadline"),
    [
        (
            {"enabled": True, "last_status": "quota_wait", "next_run_at": 9_999_999_999, "last_force_run_token": "old"},
            "new-force-token",
            9_999_999_999,
        ),
        (
            {"enabled": False, "last_status": "quota_wait", "next_run_at": 9_999_999_999},
            "",
            9_999_999_999,
        ),
        (
            {"enabled": True, "last_status": "quota_wait_unverified", "next_run_at": 0, "last_force_run_token": "old"},
            "new-force-token",
            0,
        ),
    ],
)
def test_entrypoint_overrides_never_erase_quota_hold(tmp_path: Path, persisted: dict, force_token: str, expected_deadline: int):
    state_dir = tmp_path / "state"
    supervisor_path = state_dir / "prediction-lab" / "supervisor.json"
    supervisor_path.parent.mkdir(parents=True)
    supervisor_path.write_text(json.dumps(persisted), encoding="utf-8")

    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["AGY_STATE_DIR"] = str(state_dir)
    env["AGY_GOAL_ENABLED"] = "true"
    env["AGY_FORCE_RUN_TOKEN"] = force_token
    env.pop("AGY_GOAL", None)

    subprocess.run([sys.executable, "-c", _entrypoint_python()], env=env, check=True, capture_output=True, text=True)

    result = json.loads(supervisor_path.read_text(encoding="utf-8"))
    assert result["enabled"] is True
    assert result["last_status"] == persisted["last_status"]
    assert result["next_run_at"] == expected_deadline
    if force_token:
        assert result["last_force_run_token"] == force_token


def test_compound_quota_duration_uses_complete_duration_and_guard(monkeypatch):
    now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(agy_worker, "_utcnow", lambda: now)
    monkeypatch.setattr(agy_worker, "QUOTA_REFRESH_GUARD_SECONDS", 600)

    deadline = agy_worker._parse_refresh_deadline("Quota exceeded; resets in 2 hours 30 minutes")

    assert deadline == now + timedelta(hours=2, minutes=40)


def test_multiple_quota_windows_choose_latest_verified_recovery(monkeypatch):
    now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(agy_worker, "_utcnow", lambda: now)
    monkeypatch.setattr(agy_worker, "QUOTA_REFRESH_GUARD_SECONDS", 600)

    deadline = agy_worker._parse_refresh_deadline(
        "Five-hour quota resets in 1 hour. Weekly quota resets in 3 hours 15 minutes."
    )

    assert deadline == now + timedelta(hours=3, minutes=25)


def test_quota_timestamp_requires_future_timezone_aware_deadline(monkeypatch):
    now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(agy_worker, "_utcnow", lambda: now)
    monkeypatch.setattr(agy_worker, "QUOTA_REFRESH_GUARD_SECONDS", 600)

    assert agy_worker._parse_refresh_deadline("resets at 2026-09-21T18:00:00+03:00") == datetime(
        2026, 9, 21, 15, 10, tzinfo=timezone.utc
    )
    assert agy_worker._parse_refresh_deadline("resets at 2026-09-21T10:00:00+00:00") is None
    assert agy_worker._parse_refresh_deadline("resets sometime later today") is None


def test_supervisor_future_quota_hold_blocks_subprocess_launch(tmp_path: Path, monkeypatch):
    state = tmp_path / "supervisor.json"
    state.write_text(
        json.dumps({"enabled": True, "last_status": "quota_wait", "next_run_at": time.time() + 3600}),
        encoding="utf-8",
    )
    monkeypatch.setattr(agy_worker, "SUPERVISOR_STATE_FILE", state)

    async def forbidden_launch(*args, **kwargs):
        raise AssertionError("AGY subprocess launch must not occur during quota hold")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_launch)
    supervisor = agy_worker.GoalSupervisor()
    asyncio.run(supervisor._run_goal())
    assert supervisor.last_status == "quota_wait"


def test_interactive_console_cannot_bypass_quota_hold(tmp_path: Path, monkeypatch):
    state = tmp_path / "supervisor.json"
    state.write_text(
        json.dumps({"last_status": "quota_wait", "next_run_at": time.time() + 3600}),
        encoding="utf-8",
    )
    monkeypatch.setattr(agy_worker, "SUPERVISOR_STATE_FILE", state)

    async def forbidden_launch(*args, **kwargs):
        raise AssertionError("interactive AGY launch must not occur during quota hold")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_launch)
    console = agy_worker.InteractiveConsole()
    with pytest.raises(RuntimeError, match="quota guard active"):
        asyncio.run(console.start())


def test_tool_recovery_wrapper_cannot_bypass_unverified_quota_hold(tmp_path: Path, monkeypatch):
    state = tmp_path / "supervisor.json"
    state.write_text(
        json.dumps({"enabled": True, "last_status": "quota_wait_unverified", "next_run_at": 0}),
        encoding="utf-8",
    )
    monkeypatch.setattr(agy_worker, "SUPERVISOR_STATE_FILE", state)
    supervisor = agy_worker.GoalSupervisor()

    async def forbidden_original(_self):
        raise AssertionError("normal AGY run must not be entered while quota recovery is unverified")

    monkeypatch.setattr(recovery, "_ORIGINAL_RUN_GOAL", forbidden_original)
    asyncio.run(recovery._run_goal_with_same_conversation_recovery(supervisor))

    assert supervisor.last_status == "quota_wait_unverified"
    assert supervisor.next_run_at == 0


def test_tool_recovery_unknown_reset_becomes_indefinite_hold(tmp_path: Path, monkeypatch):
    state = tmp_path / "supervisor.json"
    state.write_text(json.dumps({"enabled": True, "last_status": "idle", "next_run_at": 0}), encoding="utf-8")
    monkeypatch.setattr(agy_worker, "SUPERVISOR_STATE_FILE", state)
    supervisor = agy_worker.GoalSupervisor()

    async def fake_original(self):
        self.last_status = "completed"
        self.output_tail.clear()
        self.output_tail.append(
            '[OUT] {"conversation_id":"offline-test","denied_actions":[{"reason":"permission check failed"}]}'
        )

    async def fake_resume(_self, _conversation_id, _attempt):
        return recovery.RecoveryTurn(
            exit_code=1,
            denied=False,
            quota_limited=True,
            watchdog_restart=False,
            conversation_id="offline-test",
            combined_output="baseline quota exceeded; reset time unavailable",
        )

    monkeypatch.setattr(recovery, "_ORIGINAL_RUN_GOAL", fake_original)
    monkeypatch.setattr(recovery, "_run_resume_turn", fake_resume)
    asyncio.run(recovery._run_goal_with_same_conversation_recovery(supervisor))

    assert supervisor.last_status == "quota_wait_unverified"
    assert supervisor.next_run_at == 0
    assert supervisor.quota_hold_active()[0] is True


class _RowsCursor:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def sort(self, *args, **kwargs):
        return self

    def limit(self, count: int):
        self.rows = self.rows[:count]
        return self

    def max_time_ms(self, *args, **kwargs):
        return self

    def __iter__(self):
        return iter(self.rows)


class _TimeoutCursor(_RowsCursor):
    def __iter__(self):
        raise NetworkTimeout("simulated notification timeout")


class _FakeCollection:
    def __init__(self, db: "_FakeDB", name: str):
        self.db = db
        self.name = name

    def find(self, _query, projection=None):
        self.db.reads.append(self.name)
        if self.name == "notification_history":
            return _TimeoutCursor([])
        rows = [dict(row) for row in self.db.rows.get(self.name, [])]
        if projection and projection.get("name") == 0:
            for row in rows:
                row.pop("name", None)
        return _RowsCursor(rows)


class _FakeDB:
    def __init__(self):
        self.reads: list[str] = []
        self.rows = {
            "approach_states": [{"stage": "passed", "updated_at": datetime.now(timezone.utc)}],
            "profiles": [
                {
                    "user_id": 42,
                    "name": "Private profile name",
                    "radius_km": 15,
                    "latitude": 41.0,
                    "longitude": 29.0,
                    "token": "never expose this",
                    "updated_at": datetime.now(timezone.utc),
                }
            ],
            "prediction_lab_audit": [{"callsign": "TEST1", "horizon_bucket": "0-10m", "captured_at": datetime.now(timezone.utc)}],
        }

    def __getitem__(self, name: str):
        return _FakeCollection(self, name)


def test_one_failing_collection_does_not_starve_later_agy_inputs(tmp_path: Path, monkeypatch):
    bridge._reset_resilience_state_for_tests()
    database = _FakeDB()
    monkeypatch.setattr(bridge, "_db", lambda: database)
    monkeypatch.setattr(bridge, "_MONGO_URI", "mongodb://offline-test")
    monkeypatch.setattr(bridge, "CONTEXT_FILE", tmp_path / "context.json")
    bridge._recent_cache["notification_history"] = [{"kind": "cached"}]
    bridge._cache_updated_at["notification_history"] = time.time() - 120
    bridge._cache_source["notification_history"] = "mongo"

    for _ in range(3):
        payload = bridge.build_context_snapshot()
        assert payload["prediction_audit"][0]["callsign"] == "TEST1"
        bridge._operation_state("read:notification_history")["open_until"] = 0.0

    assert database.reads.count("notification_history") == 3
    assert database.reads.count("prediction_lab_audit") == 3
    status = payload["bridge_status"]
    assert status["collections"]["notification_history"]["last_error"] == "NetworkTimeout"
    assert status["collections"]["notification_history"]["stale_age_s"] >= 120
    assert status["collections"]["prediction_lab_audit"]["state"] == "fresh"


def test_agy_context_reads_authoritative_profiles_and_sanitizes_them(tmp_path: Path, monkeypatch):
    bridge._reset_resilience_state_for_tests()
    database = _FakeDB()
    monkeypatch.setattr(bridge, "_db", lambda: database)
    monkeypatch.setattr(bridge, "_MONGO_URI", "mongodb://offline-test")
    monkeypatch.setattr(bridge, "CONTEXT_FILE", tmp_path / "context.json")

    payload = bridge.build_context_snapshot()

    assert "profiles" in database.reads
    assert "alert_profiles" not in database.reads
    assert bridge._CONTEXT_COLLECTION_KEYS["profile_configuration"] == "profiles"
    profile = payload["profile_configuration"][0]
    assert profile["radius_km"] == 15
    assert profile["user_ref"].startswith("u-")
    assert "name" not in profile
    assert "latitude" not in profile
    assert "longitude" not in profile
    assert "token" not in profile


def test_findings_pagination_returns_earliest_next_page_without_skips(tmp_path: Path, monkeypatch):
    findings_file = tmp_path / "findings.jsonl"
    lines = [json.dumps({"seq": seq, "summary": f"finding-{seq}"}) for seq in range(250, 0, -1)]
    lines.insert(50, "{malformed json")
    findings_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(agy_worker, "FINDINGS_FILE", findings_file)

    delivered: list[int] = []
    cursor = 0
    for _ in range(3):
        page = asyncio.run(agy_worker.findings(after=cursor, limit=100))
        seqs = [int(row["seq"]) for row in page["findings"]]
        delivered.extend(seqs)
        cursor = int(page["cursor"])

    assert delivered == list(range(1, 251))
    assert cursor == 250
    assert asyncio.run(agy_worker.findings(after=cursor, limit=100)) == {"findings": [], "cursor": 250}
