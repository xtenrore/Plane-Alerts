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


def test_runtime_uses_current_minor_release_version() -> None:
    assert VERSION == "6.0.0"


def test_community_runtime_installs_receiver_guard_before_loading_main() -> None:
    text = (ROOT / "app" / "community_main_v55.py").read_text(encoding="utf-8")
    guard_pos = text.index("install_local_receiver_coverage_guard()")
    main_pos = text.index("from app.main import app")
    assert guard_pos < main_pos


def test_community_doctor_has_safe_local_storage_and_five_second_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    checks = static_checks(_config())
    assert next(check for check in checks if check.name == "database-config").status == "ok"
    cadence = next(check for check in checks if check.name == "monitor-cadence")
    assert cadence.status == "ok"
    assert "Five-second" in cadence.detail
    contradictions = next(check for check in checks if check.name == "configuration-contradictions")
    assert contradictions.status == "ok"


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


def test_v5610_prediction_sync_uses_existing_admin_http_bridge_not_sftp() -> None:
    deploy = (ROOT / ".github" / "workflows" / "deploy-railway.yml").read_text(encoding="utf-8")
    sync = (ROOT / ".github" / "workflows" / "prediction-lab-sync.yml").read_text(encoding="utf-8")
    assert 'railway volume --project "$PROJECT_ID" --service "$MAIN_SERVICE_ID" --environment "$ENVIRONMENT_ID" list --json' in deploy
    assert 'RAILWAY_TOKEN: ${{ secrets.RAILWAY_API_TOKEN }}' in sync
    assert "railway variable list" in sync
    assert "/admin/api/prediction-lab/sync-batch" in sync
    assert "/admin/api/prediction-lab/sync-ack" in sync
    assert "railway service files" not in sync
    assert "ssh-keygen" not in sync

def test_blank_env_example_values_fall_back_to_validated_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # The checked-in template deliberately leaves optional values blank. Pydantic
    # must treat those as unset rather than feeding empty strings into typed fields.
    for name in (
        "MONGO_URI",
        "DATABASE_NAME",
        "API_KEYS_DIR",
        "LOCAL_ADSB_RECEIVER_TYPE",
        "LOCAL_ADSB_TIMEOUT_SECONDS",
        "LOCAL_ADSB_MAX_POSITION_AGE_SECONDS",
        "GEMINI_PHOTO_TIMEOUT_SECONDS",
        "PHOTOGRAPHY_HTTP_TIMEOUT_SECONDS",
        "PHOTOGRAPHY_CONDITIONS_CACHE_SECONDS",
        "HOST",
        "LOG_LEVEL",
        "POLL_INTERVAL_SECONDS",
        "DEFAULT_RADIUS_KM",
        "COOLDOWN_MINUTES",
        "LEARNING_PLANE_THRESHOLD",
        "RELEARN_PLANE_COUNT",
        "ADMIN_TELEGRAM_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    loaded = Settings(_env_file=ROOT / ".env.example")
    assert loaded.mongo_uri == "mongodb://localhost:27017"
    assert loaded.database_name == "aircraft_bot"
    assert loaded.api_keys_dir == "api"
    assert loaded.local_adsb_receiver_type == "auto"
    assert loaded.local_adsb_timeout_seconds == 0.8
    assert loaded.local_adsb_max_position_age_seconds == 12.0
    assert loaded.gemini_photo_timeout_seconds == 30.0
    assert loaded.photography_http_timeout_seconds == 12.0
    assert loaded.photography_conditions_cache_seconds == 120
    assert loaded.host == "0.0.0.0"
    assert loaded.log_level == "INFO"
    assert loaded.poll_interval_seconds == 5
    assert loaded.default_radius_km == 15.0
    assert loaded.cooldown_minutes == 30
    assert loaded.learning_plane_threshold == 100
    assert loaded.relearn_plane_count == 25
    assert loaded.admin_telegram_id is None
