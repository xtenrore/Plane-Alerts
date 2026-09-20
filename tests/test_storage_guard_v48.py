from types import SimpleNamespace

import pytest

from app.worker import storage_guard_v48 as guard


@pytest.mark.asyncio
async def test_memory_first_state_gets_stable_id_and_normalizes_without_mongo_id(monkeypatch):
    def fake_states(user_id, icao24s):
        assert user_id == 7
        assert icao24s == {"abc123"}
        return {
            "abc123": {
                "user_id": 7,
                "aircraft_icao24": "abc123",
                "active": True,
                "first_notification_sent": True,
            }
        }

    monkeypatch.setattr(guard.storage_runtime, "approach_states", fake_states)
    states = await guard._prefetch_approach_states_cached(
        7, [SimpleNamespace(icao24="abc123")]
    )
    memory_id = states["abc123"]["_id"]
    assert memory_id == "v48-memory:7:abc123"
    assert guard._normalize_state_query(7, states, {"_id": memory_id}) == {
        "user_id": 7,
        "aircraft_icao24": "abc123",
    }


def test_persisted_mongo_id_also_normalizes_to_unique_lifecycle_identity():
    states = {
        "abc123": {
            "_id": "mongo-object-id-placeholder",
            "user_id": 7,
            "aircraft_icao24": "abc123",
        }
    }
    assert guard._normalize_state_query(
        7, states, {"_id": "mongo-object-id-placeholder"}
    ) == {"user_id": 7, "aircraft_icao24": "abc123"}
