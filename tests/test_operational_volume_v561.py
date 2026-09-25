from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import operational_volume_v561 as volume


@pytest.mark.asyncio
async def test_route_history_round_trip_is_volume_backed(tmp_path, monkeypatch):
    monkeypatch.setenv("PREDICTION_LAB_ROOT", str(tmp_path))
    volume._last_prune_mono = 0.0

    service = SimpleNamespace(_last_sample={})
    ac = SimpleNamespace(
        callsign="THY123",
        latitude=41.0,
        longitude=28.0,
        altitude=3500.0,
        heading=90.0,
        aircraft_type="A321",
    )
    base = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc).timestamp()
    for offset in (0.0, 30.0, 60.0):
        ac.longitude += 0.02
        await volume.observe_route_volume(service, ac, now=base + offset)

    day = datetime.fromtimestamp(base, timezone.utc).date().isoformat()
    doc = await volume.load_route_doc("THY123", day)
    assert doc is not None
    assert doc["aircraft_type"] == "A321"
    assert len(doc["points"]) == 3
    assert volume.route_db_path().exists()


@pytest.mark.asyncio
async def test_next60_route_lookup_uses_volume_not_mongo(tmp_path, monkeypatch):
    monkeypatch.setenv("PREDICTION_LAB_ROOT", str(tmp_path))
    day = "2026-09-24"
    await volume.append_route_sample(
        "THY999",
        day,
        {"t": 1.0, "lat": 41.0, "lon": 28.0},
        captured_at=datetime(2026, 9, 24, tzinfo=timezone.utc).timestamp(),
        aircraft_type="A359",
    )

    class ExplodingDb:
        def __getitem__(self, name):
            raise AssertionError(f"Mongo must not be used for route history: {name}")

    doc = await volume._next60_route_from_volume(ExplodingDb(), "THY999", day)
    assert doc is not None
    assert doc["aircraft_type"] == "A359"


@pytest.mark.asyncio
async def test_migration_imports_recent_route_rows_then_drops_only_route_collection(tmp_path, monkeypatch):
    monkeypatch.setenv("PREDICTION_LAB_ROOT", str(tmp_path))
    recent_day = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()

    class Cursor:
        def __aiter__(self):
            async def rows():
                yield {
                    "callsign": "THY321",
                    "utc_date": recent_day,
                    "points": [
                        {"t": 1.0, "lat": 41.0, "lon": 28.0},
                        {"t": 2.0, "lat": 41.1, "lon": 28.1},
                        {"t": 3.0, "lat": 41.2, "lon": 28.2},
                    ],
                    "aircraft_type": "B738",
                }
            return rows()

    class Collection:
        def __init__(self):
            self.dropped = False

        def find(self, query, projection):
            assert query["utc_date"]["$gte"]
            assert projection["points"] == 1
            return Cursor()

        async def drop(self):
            self.dropped = True

    class Db:
        def __init__(self):
            self.route = Collection()

        def __getitem__(self, name):
            if name != "flight_route_samples":
                raise AssertionError(f"unexpected Mongo collection touched: {name}")
            return self.route

    db = Db()
    state = await volume.migrate_route_history_mongo(db)
    assert state["legacy_collection_dropped"] is True
    assert state["normal_application_data_untouched"] is True
    assert db.route.dropped is True
    migrated = await volume.load_route_doc("THY321", recent_day)
    assert migrated is not None
    assert len(migrated["points"]) == 3


@pytest.mark.asyncio
async def test_legacy_prediction_writers_are_permanent_noops():
    assert await volume._no_legacy_prediction_mongo_write("prediction_lab_audit", {"x": 1}) is None
    assert await volume._no_legacy_sentinel_mongo_write(None, [("THY1", {})], "provider", 1.0) == 0


def test_install_rebinds_all_route_consumers_to_volume():
    from app import next60_outcomes_v55, prediction_lab_audit, sentinel_network
    from app.intelligence import route_guard_v2, route_history, route_intelligence_v46, route_observe_guard_v44

    volume._installed = False
    volume.install_operational_volume_v561()

    assert route_guard_v2._ORIGINAL_OBSERVE is volume.observe_route_volume
    assert route_observe_guard_v44._BASE_OBSERVE is volume.observe_route_volume
    assert route_history.RouteHistoryService._historical_paths is volume.historical_paths_volume
    assert route_intelligence_v46.observe_v46 is volume.observe_route_volume
    assert next60_outcomes_v55._route_for is volume._next60_route_from_volume
    assert prediction_lab_audit._legacy_insert is volume._no_legacy_prediction_mongo_write
    assert sentinel_network._legacy_record_region is volume._no_legacy_sentinel_mongo_write
    assert sentinel_network._ensure_migration is volume._ensure_volume_migrations
