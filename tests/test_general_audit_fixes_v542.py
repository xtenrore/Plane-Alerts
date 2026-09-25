from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from telegram.ext import ApplicationHandlerStop


def _valid_profile_config() -> dict:
    return {
        "location": {"latitude": 41.0, "longitude": 29.0, "radius_km": 15.0},
        "preferences": {
            "aircraft_filter": {
                "mode": "all",
                "selected_categories": [],
                "selected_types": [],
                "excluded_types": [],
            },
            "filter_rules": {"profile": {}, "categories": {}, "aircraft": {}},
        },
    }


def test_pa_a05_blank_optional_admin_id_is_valid_and_garbage_is_rejected():
    from app.config import Settings

    assert Settings(_env_file=None, admin_telegram_id="").admin_telegram_id is None
    assert Settings(_env_file=None, admin_telegram_id="123").admin_telegram_id == 123
    with pytest.raises(ValidationError):
        Settings(_env_file=None, admin_telegram_id="not-a-number")


@pytest.mark.asyncio
async def test_pa_a06_blank_admin_password_fails_closed(monkeypatch):
    from app.admin import auth, routes

    monkeypatch.setattr(routes.settings, "admin_password", "")
    with pytest.raises(HTTPException) as exc:
        await routes._check_auth(None)
    assert exc.value.status_code == 401

    monkeypatch.setattr(auth.settings, "admin_password", "")
    request = SimpleNamespace(state=SimpleNamespace(), headers={})
    with pytest.raises(HTTPException) as exc:
        await auth.get_admin_actor(request)
    assert exc.value.status_code == 401


def test_pa_a07_compose_healthcheck_uses_readiness_endpoint():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert "127.0.0.1:8000/ready" in compose
    assert "127.0.0.1:8000/health >/dev/null" not in compose


@pytest.mark.asyncio
async def test_pa_a01_initial_setup_enables_only_after_successful_profile_save(monkeypatch):
    from app.bot import profile_handlers as legacy
    import app.bot.profile_legacy

    events: list[str] = []
    config = _valid_profile_config()

    async def raw_state(_uid):
        return "profile:edit", {
            "profile_flow": "setup",
            "editing_profile_id": "abc123def0",
            "profile_draft": config,
        }

    async def save_profile(_uid, _pid, *, config):
        assert config is not None
        events.append("save")
        return {"profile_id": "abc123def0"}

    class Users:
        async def update_one(self, *_args, **_kwargs):
            events.append("activate")

    async def clear(_uid):
        events.append("clear")

    async def render(*_args, **_kwargs):
        events.append("render")

    monkeypatch.setattr(legacy, "_raw_state", raw_state)
    monkeypatch.setattr(legacy, "save_profile", save_profile)
    monkeypatch.setattr(legacy, "users_col", lambda: Users())
    monkeypatch.setattr(legacy, "_clear_state", clear)
    monkeypatch.setattr(legacy, "_render_profile_detail", render)

    await legacy._save_edit(SimpleNamespace(callback_query=None), 1)
    assert events[:2] == ["save", "activate"]

    events.clear()

    async def failed_save(*_args, **_kwargs):
        events.append("save_failed")
        raise RuntimeError("synthetic write failure")

    monkeypatch.setattr(legacy, "save_profile", failed_save)
    with pytest.raises(RuntimeError):
        await legacy._save_edit(SimpleNamespace(callback_query=None), 1)
    assert events == ["save_failed"]


@pytest.mark.asyncio
async def test_pa_a02_active_profile_is_not_committed_when_materialization_fails(monkeypatch):
    import app.alert_profiles as profiles

    old = {
        "user_id": 7,
        "profile_id": "abc123def0",
        "name": "Default",
        "config": _valid_profile_config(),
    }
    new_config = _valid_profile_config()
    new_config["location"] = dict(new_config["location"], radius_km=25.0)
    profile_updates: list[dict] = []

    async def get_profile(_uid, _pid):
        return old

    class Users:
        async def find_one(self, *_args, **_kwargs):
            return {"active_profile_id": old["profile_id"]}

    class Profiles:
        async def update_one(self, _query, update):
            profile_updates.append(update)

    async def fail_materialization(candidate):
        assert candidate["config"]["location"]["radius_km"] == 25.0
        raise RuntimeError("second materialization write failed")

    monkeypatch.setattr(profiles, "get_profile", get_profile)
    monkeypatch.setattr(profiles, "users_col", lambda: Users())
    monkeypatch.setattr(profiles, "profiles_col", lambda: Profiles())
    monkeypatch.setattr(profiles, "materialize_profile", fail_materialization)

    with pytest.raises(RuntimeError):
        await profiles.save_profile(7, old["profile_id"], config=new_config)
    assert profile_updates == []


class _AsyncCursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def __aiter__(self):
        self._index = 0
        return self

    async def __anext__(self):
        if self._index >= len(self.rows):
            raise StopAsyncIteration
        row = self.rows[self._index]
        self._index += 1
        return row


class _ReadCollection:
    def __init__(self, rows):
        self.rows = list(rows)

    def find(self, _query, _projection=None):
        return _AsyncCursor(self.rows)

    async def find_one(self, query, _projection=None):
        for row in self.rows:
            if all(row.get(key) == value for key, value in query.items()):
                return row
        return None


@pytest.mark.asyncio
async def test_pa_a02_torn_refresh_preserves_per_user_last_known_good(monkeypatch):
    from app.worker import storage_guard_v48 as guard

    runtime = guard.storage_runtime
    old_users = runtime._users
    try:
        runtime._users = {
            7: {
                "user_id": 7,
                "location": {"user_id": 7, "latitude": 1.0, "longitude": 2.0, "config_revision": "old"},
                "preferences": {"user_id": 7, "config_revision": "old", "aircraft_filter": {"mode": "all"}},
                "admin_control": {},
            }
        }
        db = {
            "users": _ReadCollection([{"user_id": 7, "setup_complete": True, "active_profile_id": "abc123def0"}]),
            "locations": _ReadCollection([{"user_id": 7, "latitude": 1.0, "longitude": 2.0, "config_revision": "old"}]),
            "preferences": _ReadCollection([{"user_id": 7, "config_revision": "new"}]),
            "profiles": _ReadCollection([]),
        }
        loaded = await guard._load_active_users_coherent(db)
        assert loaded[7]["location"]["config_revision"] == "old"
        assert loaded[7]["preferences"]["config_revision"] == "old"
    finally:
        runtime._users = old_users


def test_pa_a09_fresh_main_import_installs_interaction_layer():
    code = (
        "import app.main; "
        "from app.bot import interaction_v46 as i, next60; "
        "assert i._installed is True; "
        "assert next60.cb_next60_more is i.next60_more_v46"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path.cwd(),
        env={**os.environ, "TELEGRAM_BOT_TOKEN": ""},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.asyncio
async def test_pa_a10_cached_worker_path_queues_elevation_without_blocking(monkeypatch):
    from app.worker import storage_guard_v48 as guard

    runtime = guard.storage_runtime
    old_loaded, old_users = runtime._config_loaded, runtime._users
    calls = []
    try:
        runtime._config_loaded = True
        runtime._users = {
            1: {
                "user_id": 1,
                "location": {"latitude": 41.0, "longitude": 29.0, "config_revision": "r"},
                "preferences": {"config_revision": "r"},
                "admin_control": {},
            }
        }
        monkeypatch.setattr(runtime, "start", lambda: None)
        monkeypatch.setattr(guard.monitor, "_queue_observer_elevation", lambda uid, loc: calls.append((uid, dict(loc))))
        users = await guard._get_active_users_cached()
        assert len(users) == 1
        assert calls == [(1, {"latitude": 41.0, "longitude": 29.0, "config_revision": "r"})]
    finally:
        runtime._config_loaded = old_loaded
        runtime._users = old_users


@pytest.mark.asyncio
async def test_pa_a11_stale_preset_save_cannot_create_or_activate_profile(monkeypatch):
    import app.bot.profile_legacy
    from app.bot import profile_experience_v54 as v54

    created = []
    shown = []

    async def raw_state(_uid):
        return "profile:aircraft", {
            "profile_flow": "edit",
            "profile_draft": _valid_profile_config(),
            "editing_profile_id": "abc123def0",
        }

    async def create(*_args, **_kwargs):
        created.append(True)
        raise AssertionError("stale save must not create a profile")

    async def show(_update, text, *_args, **_kwargs):
        shown.append(text)

    class Query:
        data = "ux54:save:stale-session"
        message = SimpleNamespace()

        async def answer(self, *_args, **_kwargs):
            return True

    update = SimpleNamespace(callback_query=Query(), effective_user=SimpleNamespace(id=1))
    monkeypatch.setattr(v54.legacy, "_raw_state", raw_state)
    monkeypatch.setattr(v54.legacy, "_show", show)
    monkeypatch.setattr(v54, "create_profile", create)

    with pytest.raises(ApplicationHandlerStop):
        await v54.callback_v54(update, None)
    assert created == []
    assert shown and "expired" in shown[0].lower()


def test_pa_a12_old_snapshot_accumulates_elapsed_position_age():
    from app.photography_safety_v542 import _snapshot_observation_age

    now = 2_000_000_000.0
    doc = {"captured_at": now - 3600.0, "position_age_s": 2.0}
    assert _snapshot_observation_age(doc, now=now) == pytest.approx(3602.0)


@pytest.mark.asyncio
async def test_pa_a13_untargeted_photo_uses_canonical_filter(monkeypatch):
    from app.photography_safety_v542 import _find_live_aircraft_v542
    from app.worker import monitor

    a320 = SimpleNamespace(
        has_position=True, latitude=41.01, longitude=29.0, aircraft_type="A320",
        icao24="a32001", display_type="A320", callsign="THY1", altitude=3000.0,
        velocity=200.0, heading=90.0, vertical_rate_mps=0.0, position_age_s=1.0,
        operator_icao="THY", operator="",
    )
    c17 = SimpleNamespace(
        has_position=True, latitude=41.03, longitude=29.0, aircraft_type="C17",
        icao24="c17001", display_type="C17", callsign="RCH1", altitude=3000.0,
        velocity=200.0, heading=90.0, vertical_rate_mps=0.0, position_age_s=1.0,
        operator_icao="", operator="",
    )

    class Provider:
        async def query_providers(self, **_kwargs):
            return [a320, c17], "fake"

    monkeypatch.setattr(monitor, "get_provider_manager", lambda: Provider())
    prefs = {
        "aircraft_filter": {
            "mode": "selected",
            "selected_categories": ["military"],
            "selected_types": [],
            "excluded_types": [],
        },
        "filter_rules": {"profile": {}, "categories": {}, "aircraft": {}},
    }
    selected = await _find_live_aircraft_v542(41.0, 29.0, 15.0, prefs=prefs)
    assert selected is not None
    assert selected.aircraft_type == "C17"


def test_pa_a16_inherited_altitude_contradiction_is_rejected():
    import app.bot.profile_legacy
    from app.bot import profile_handlers as legacy

    config = _valid_profile_config()
    config["preferences"]["filter_rules"] = {
        "profile": {"max_altitude_ft": 10000},
        "categories": {"widebody": {"min_altitude_ft": 20000}},
        "aircraft": {},
    }
    error = legacy._validate_draft(config)
    assert error is not None
    assert "minimum 20000" in error
    assert "maximum 10000" in error


def test_pa_a17_product_version_strings_are_canonical():
    from app.version import VERSION
    from app.bot import profile_legacy
    from app.admin import v36_routes

    assert VERSION == "5.6.11"
    assert "v4.3" not in profile_legacy.cmd_help_profiled.__doc__ if profile_legacy.cmd_help_profiled.__doc__ else True
    assert v36_routes.VERSION == VERSION
