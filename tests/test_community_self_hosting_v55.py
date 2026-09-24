from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.doctor_v55 import static_checks
from app.version import VERSION

ROOT = Path(__file__).resolve().parents[1]


def _config(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "telegram_bot_token": "123456:test-token",
        "mongo_uri": "mongodb://localhost:27017",
        "default_radius_km": 15.0,
        "poll_interval_seconds": 5,
        "local_adsb_url": "",
        "local_adsb_auth_header": "",
        "opensky_1": "",
        "opensky_2": "",
        "opensky_3": "",
        "opensky_4": "",
        "opensky_5": "",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_v55_is_sequential_community_release() -> None:
    assert VERSION == "5.5.1"


def test_community_runtime_stubs_private_agy_before_loading_main() -> None:
    text = (ROOT / "app" / "community_main_v55.py").read_text(encoding="utf-8")
    stub_pos = text.index('sys.modules["app.agy_console"] = _stub')
    main_pos = text.index("from app.main import app")
    assert stub_pos < main_pos
    assert '!= "agy"' in text
    assert "agy_worker_url" not in text.lower()
    assert "agy_worker_token" not in text.lower()


def test_community_doctor_has_explicit_agy_exclusion_and_no_private_config_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    checks = static_checks(_config())
    exclusion = next(check for check in checks if check.name == "agy-exclusion")
    assert exclusion.status == "ok"
    rendered = "\n".join(check.detail for check in checks).lower()
    assert "worker token" not in rendered
    assert "worker url" not in rendered


def test_doctor_accepts_current_opensky_slot_formats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    compact = static_checks(_config(opensky_1="client-id:client-secret"))
    assert next(check for check in compact if check.name == "opensky-config").status == "ok"

    json_slot = static_checks(_config(opensky_1='{"clientId":"abc","clientSecret":"def"}'))
    assert next(check for check in json_slot if check.name == "opensky-config").status == "ok"

    malformed = static_checks(_config(opensky_1="not-a-credential"))
    assert next(check for check in malformed if check.name == "opensky-config").status == "fail"


@pytest.mark.asyncio
async def test_database_module_selects_sqlite_and_runs_real_indexes_and_migrations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import database

    await database.close_db()
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "planealerts.db"))
    db = await database.connect_db(max_retries=1)
    try:
        assert database.database_backend_name() == "sqlite"
        assert bool(getattr(db, "is_plane_alerts_sqlite", False)) is True
        assert await database.ping_db(timeout_s=0.5) is True
        migration = await db["schema_migrations"].find_one({"version": 1})
        assert migration is not None
        await db["users"].insert_one({"user_id": 1, "setup_complete": True})
        with pytest.raises(ValueError):
            await db["users"].insert_one({"user_id": 1, "setup_complete": True})
    finally:
        await database.close_db()


def test_community_runtime_is_not_used_by_railway_workflow() -> None:
    workflow = (ROOT / ".github" / "workflows" / "deploy-railway.yml").read_text(encoding="utf-8")
    assert "head_branch == 'main'" in workflow
    assert "community_main_v55" not in workflow
