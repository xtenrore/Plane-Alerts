from types import SimpleNamespace

import pytest

from app.worker import monitor


@pytest.mark.asyncio
async def test_requalifying_trajectory_clears_preserved_cancellation_evidence(monkeypatch):
    """Neutral-cycle evidence must not survive a genuine qualifying recovery."""
    prediction = SimpleNamespace(
        stale=False,
        already_passed=False,
        state="Approaching",
        enters_alert_radius=True,
        current_distance_km=6.0,
        projected_closest_km=4.0,
        time_to_cpa_s=120.0,
        confidence="High",
        confidence_score=0.90,
    )
    aircraft = SimpleNamespace(
        has_position=True,
        aircraft_type="A321",
        latitude=41.05,
        longitude=29.0,
        icao24="abc123",
        callsign="TK1",
    )
    old_state = {
        "_id": "state-1",
        "message_id": 42,
        "notification_id": "notif-1",
        "active": True,
        "projected_closest_km": 5.0,
        "observed_closest_km": 6.5,
        "cancel_confirmation_count": 2,
    }

    class FakeStates:
        def __init__(self):
            self.updates = []

        async def find_one(self, query):
            return old_state

        async def update_one(self, query, update, upsert=False):
            self.updates.append(update)

    states = FakeStates()
    monkeypatch.setattr(monitor, "get_db", lambda: {"approach_states": states})
    monkeypatch.setattr(monitor, "_watched", lambda prefs: {"A321"})
    monkeypatch.setattr(monitor, "_history", SimpleNamespace(get=lambda icao24: []))
    monkeypatch.setattr(monitor, "predict_trajectory", lambda *args, **kwargs: prediction)
    monkeypatch.setattr(
        monitor,
        "decide_lifecycle",
        lambda *args, **kwargs: SimpleNamespace(stage="candidate"),
    )

    async def evaluate_route(*args, **kwargs):
        return SimpleNamespace(
            suppress_alert=False,
            callsign="TK1",
            destination_code=None,
            history_days=0,
            similar_days=0,
            similarity_km=None,
            reason="",
            expected_turn_pending=False,
        )

    async def no_camera(*args, **kwargs):
        return None

    async def keep_message(*args, **kwargs):
        return 42

    monkeypatch.setattr(monitor.route_history_service, "evaluate", evaluate_route)
    monkeypatch.setattr(monitor, "_camera", no_camera)
    monkeypatch.setattr(monitor, "send_or_update_approach", keep_message)

    user = {
        "user_id": 1,
        "preferences": {},
        "location": {"latitude": 41.0, "longitude": 29.0, "radius_km": 10.0},
    }

    await monitor._match_user_aircraft(user, [aircraft], {})

    assert states.updates
    final_update = states.updates[-1]
    assert final_update["$set"]["active"] is True
    assert final_update["$set"]["cancel_confirmation_count"] == 0
    assert final_update["$unset"] == {
        "candidate_projected_closest_km": "",
        "candidate_state": "",
        "candidate_confidence": "",
        "candidate_time_to_cpa_s": "",
    }
