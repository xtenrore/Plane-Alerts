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


def test_v562_installer_starts_route_retirement_and_disables_legacy_migrator(monkeypatch):
    from app import legacy_mongo_retirement_v562 as retirement
    from app import prediction_lab_files_v55, sentinel_network

    scheduled: list[bool] = []
    monkeypatch.setattr(retirement, "_schedule_route_migration", lambda: scheduled.append(True))
    monkeypatch.setattr(retirement, "_installed", False)
    monkeypatch.setattr(prediction_lab_files_v55, "migrate_prediction_lab_mongo", prediction_lab_files_v55.migrate_prediction_lab_mongo)
    monkeypatch.setattr(sentinel_network, "migrate_prediction_lab_mongo", sentinel_network.migrate_prediction_lab_mongo)

    retirement.install_legacy_mongo_retirement_v562()

    assert scheduled == [True]
    assert prediction_lab_files_v55.migrate_prediction_lab_mongo is retirement._retired_prediction_lab_migration
    assert sentinel_network.migrate_prediction_lab_mongo is retirement._retired_prediction_lab_migration


@pytest.mark.asyncio
async def test_retired_prediction_migrator_never_touches_mongo():
    from app import legacy_mongo_retirement_v562 as retirement

    class ExplodingDb:
        def __getitem__(self, name):
            raise AssertionError(f"retired operational migrator touched Mongo collection {name}")

    state = await retirement._retired_prediction_lab_migration(ExplodingDb())
    assert state["verified"] is True
    assert state["runtime_writes_disabled"] is True
    assert state["storage"] == "persistent-volume"
