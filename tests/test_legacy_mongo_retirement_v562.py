from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_sentinel_never_runs_legacy_mongo_export_when_file_backed_is_authoritative(monkeypatch):
    from app import sentinel_network

    async def forbidden_migration(*args, **kwargs):
        raise AssertionError("legacy Prediction Lab Mongo exporter must not run")

    monkeypatch.setattr(sentinel_network, "migration_verified", lambda: True)
    monkeypatch.setattr(sentinel_network, "migrate_prediction_lab_mongo", forbidden_migration)

    assert await sentinel_network._ensure_migration() is True


def test_v562_installer_schedules_narrow_retirement_and_disables_legacy_migrator(monkeypatch):
    from app import legacy_mongo_retirement_v562 as retirement
    from app import prediction_lab_files_v55, sentinel_network

    route_scheduled: list[bool] = []
    prediction_scheduled: list[bool] = []
    monkeypatch.setattr(retirement, "_schedule_route_cleanup_once", lambda: route_scheduled.append(True))
    monkeypatch.setattr(retirement, "_schedule_prediction_cleanup", lambda: prediction_scheduled.append(True))
    monkeypatch.setattr(retirement, "_installed", False)
    monkeypatch.setattr(prediction_lab_files_v55, "migrate_prediction_lab_mongo", prediction_lab_files_v55.migrate_prediction_lab_mongo)
    monkeypatch.setattr(sentinel_network, "migrate_prediction_lab_mongo", sentinel_network.migrate_prediction_lab_mongo)

    retirement.install_legacy_mongo_retirement_v562()

    assert route_scheduled == [True]
    assert prediction_scheduled == [True]
    assert prediction_lab_files_v55.migration_verified is retirement._volume_authoritative_and_schedule_retirement
    assert sentinel_network.migration_verified is retirement._volume_authoritative_and_schedule_retirement
    assert prediction_lab_files_v55.migrate_prediction_lab_mongo is retirement._retired_prediction_lab_migration
    assert sentinel_network.migrate_prediction_lab_mongo is retirement._retired_prediction_lab_migration


def test_authoritative_check_schedules_both_operational_retirements(monkeypatch):
    from app import legacy_mongo_retirement_v562 as retirement

    route_scheduled: list[bool] = []
    prediction_scheduled: list[bool] = []
    monkeypatch.setattr(retirement, "_schedule_route_cleanup_once", lambda: route_scheduled.append(True))
    monkeypatch.setattr(retirement, "_schedule_prediction_cleanup", lambda: prediction_scheduled.append(True))

    assert retirement._volume_authoritative_and_schedule_retirement() is True
    assert route_scheduled == [True]
    assert prediction_scheduled == [True]


def test_route_cleanup_scheduler_starts_underlying_runner_only_once(monkeypatch):
    from app import legacy_mongo_retirement_v562 as retirement

    class FakeTask:
        pass

    calls: list[bool] = []
    monkeypatch.setattr(retirement, "_route_schedule_started", False)
    monkeypatch.setattr(retirement.operational_volume, "_migration_task", None)

    def schedule():
        calls.append(True)
        retirement.operational_volume._migration_task = FakeTask()

    monkeypatch.setattr(retirement.operational_volume, "_schedule_route_migration", schedule)
    retirement._schedule_route_cleanup_once()
    retirement._schedule_route_cleanup_once()

    assert calls == [True]
    assert retirement._route_schedule_started is True


@pytest.mark.asyncio
async def test_prediction_cleanup_drops_only_verified_non_user_legacy_collections(tmp_path, monkeypatch):
    from app import legacy_mongo_retirement_v562 as retirement
    from app import prediction_lab_files_v55

    monkeypatch.setenv("PREDICTION_LAB_ROOT", str(tmp_path))
    monkeypatch.setattr(retirement, "_cleanup_complete", False)
    touched: list[str] = []

    class Collection:
        def __init__(self, name: str):
            self.name = name

        async def drop(self):
            touched.append(self.name)

    class Db:
        def __getitem__(self, name: str):
            if name in {"users", "locations", "profiles", "preferences", "settings"}:
                raise AssertionError(f"user/config collection must never be touched: {name}")
            if name not in prediction_lab_files_v55.LAB_COLLECTIONS:
                raise AssertionError(f"unexpected collection cleanup: {name}")
            return Collection(name)

    monkeypatch.setattr(retirement, "get_db", lambda: Db())
    state = await retirement._drop_legacy_prediction_collections()

    assert set(touched) == set(prediction_lab_files_v55.LAB_COLLECTIONS)
    assert state["legacy_collections_dropped"] is True
    assert state["normal_application_data_untouched"] is True
    assert state["runtime_writes_disabled"] is True
    assert (tmp_path / "state" / "prediction_lab_legacy_retirement_v562.json").exists()


@pytest.mark.asyncio
async def test_retired_prediction_migrator_never_reopens_mongo_export(monkeypatch):
    from app import legacy_mongo_retirement_v562 as retirement

    scheduled: list[bool] = []
    monkeypatch.setattr(retirement, "_schedule_prediction_cleanup", lambda: scheduled.append(True))
    state = await retirement._retired_prediction_lab_migration(object())
    assert scheduled == [True]
    assert state["verified"] is True
    assert state["runtime_writes_disabled"] is True
    assert state["storage"] == "persistent-volume"
