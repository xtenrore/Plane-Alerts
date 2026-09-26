from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.intelligence.route_history import RouteGateResult
from app.intelligence.trajectory import HistorySample
from app.worker import monitor


class _ApproachStates:
    def __init__(self, document):
        self.document = dict(document)
        self.updates = []

    async def find_one(self, query):
        return dict(self.document)

    async def update_one(self, query, update, upsert=False):
        self.updates.append((query, update, upsert))


class _Db:
    def __init__(self, states):
        self.states = states

    def __getitem__(self, name):
        assert name == "approach_states"
        return self.states


@pytest.mark.asyncio
async def test_failed_cancellation_delivery_retries_without_closing_alert(monkeypatch):
    old = {
        "_id": "state-1",
        "user_id": 1,
        "aircraft_icao24": "4baa10",
        "notification_id": "alert-1",
        "message_id": 777,
        "active": True,
        "stage": "prepare",
        "projected_closest_km": 4.0,
        "observed_closest_km": 12.0,
        "cancel_confirmation_count": 2,
        "last_observation_at": 90.0,
        "updated_at": datetime.now(timezone.utc),
        "route_callsign": "THY1017",
    }
    states = _ApproachStates(old)
    monkeypatch.setattr(monitor, "get_db", lambda: _Db(states))
    monkeypatch.setattr(
        monitor,
        "_history",
        SimpleNamespace(
            get=lambda _icao: [
                HistorySample(
                    timestamp=100.0,
                    latitude=41.0,
                    longitude=29.05,
                    altitude_m=3000.0,
                    speed_kts=250.0,
                    heading_deg=270.0,
                    vertical_rate_mps=-3.0,
                    position_age_s=0.0,
                )
            ]
        ),
    )
    prediction = SimpleNamespace(
        stale=False,
        already_passed=False,
        state="Approaching",
        current_distance_km=12.0,
        projected_closest_km=4.0,
        time_to_cpa_s=120.0,
        confidence="High",
        confidence_score=0.90,
        enters_alert_radius=True,
        turn_rate_deg_s=0.0,
    )
    monkeypatch.setattr(monitor, "predict_trajectory", lambda *_args, **_kwargs: prediction)

    async def route_veto(*_args, **_kwargs):
        return RouteGateResult(
            True,
            "THY1017",
            "known destination IST is reached before projected observer CPA",
            destination_code="IST",
            route_plausible=True,
        )

    monkeypatch.setattr(monitor.route_history_service, "evaluate", route_veto)
    monkeypatch.setattr(monitor, "enqueue_snapshot", lambda **_kwargs: None)
    monkeypatch.setattr(monitor, "enqueue_outcome", lambda **_kwargs: None)

    deliveries = iter([None, 777])
    send_calls = []

    async def fake_send(*args, **kwargs):
        send_calls.append((args, kwargs))
        return next(deliveries)

    monkeypatch.setattr(monitor, "send_or_update_approach", fake_send)

    user = {
        "user_id": 1,
        "location": {"latitude": 41.0, "longitude": 28.90, "radius_km": 8.0},
        "preferences": {
            "custom_aircraft": ["A320"],
            "selected_categories": [],
            "disabled_types": [],
            "spotting": {"approach_alerts": True, "min_confidence": "Low"},
        },
    }
    aircraft = SimpleNamespace(
        has_position=True,
        icao24="4baa10",
        callsign="THY1017",
        aircraft_type="A320",
        latitude=41.0,
        longitude=29.05,
        heading=270.0,
        velocity=130.0,
    )

    await monitor._match_user_aircraft(user, [aircraft], {})
    assert len(send_calls) == 1
    assert states.updates == []

    await monitor._match_user_aircraft(user, [aircraft], {})
    assert len(send_calls) == 2
    assert len(states.updates) == 1
    _query, update, _upsert = states.updates[0]
    assert update["$set"]["active"] is False
    assert update["$set"]["stage"] == "cancelled"
    assert update["$set"]["message_id"] == 777
