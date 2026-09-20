from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.worker import notification_telemetry as telemetry


class FakeCollection:
    def __init__(self, existing=None):
        self.existing = dict(existing or {})
        self.calls = []

    async def find_one(self, query, projection=None):
        return dict(self.existing)

    async def update_one(self, query, update, upsert=False):
        self.calls.append((query, update, upsert))


class FakeDb:
    def __init__(self, collection):
        self.collection = collection

    def __getitem__(self, name):
        assert name == "notification_history"
        return self.collection


def _payload(**overrides):
    values = {
        "notification_id": "alert-1",
        "user_id": 1,
        "aircraft_icao24": "4baa10",
        "aircraft_type": "A321",
        "distance_km": 12.0,
        "projected_closest_km": 4.0,
        "observed_closest_km": 6.0,
        "trajectory_state": "Approaching",
        "prediction_confidence": "High",
        "stage": "prepare",
        "input_message_id": None,
        "output_message_id": 100,
        "delivered": True,
        "delivery_detail": "sent",
        "occurred_at": datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_first_notification_timestamp_is_written_once_after_delivery(monkeypatch):
    collection = FakeCollection()
    monkeypatch.setattr(telemetry, "get_db", lambda: FakeDb(collection))

    await telemetry._persist_event(_payload())

    assert len(collection.calls) == 2
    _query, event_update, upsert = collection.calls[0]
    assert upsert is True
    assert event_update["$inc"]["delivery_attempts"] == 1
    assert event_update["$inc"]["event_counts.first_notification"] == 1
    assert "notified_at" not in event_update["$set"]
    assert event_update["$push"]["events"]["$slice"] == -64

    first_query, first_update, first_upsert = collection.calls[1]
    assert first_upsert is False
    assert first_query["first_notified_at"] == {"$exists": False}
    assert first_update["$set"]["notified_at"] == _payload()["occurred_at"]
    assert first_update["$set"]["first_notified_at"] == _payload()["occurred_at"]
    assert first_update["$inc"]["alert_count"] == 1


@pytest.mark.asyncio
async def test_message_edit_never_overwrites_first_notification_time(monkeypatch):
    first_at = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
    collection = FakeCollection({"first_notified_at": first_at, "delivery_attempts": 1})
    monkeypatch.setattr(telemetry, "get_db", lambda: FakeDb(collection))

    await telemetry._persist_event(
        _payload(
            stage="camera_ready",
            input_message_id=100,
            output_message_id=100,
            occurred_at=datetime(2026, 9, 20, 8, 5, tzinfo=timezone.utc),
            delivery_detail="edited",
        )
    )

    assert len(collection.calls) == 1
    _query, update, upsert = collection.calls[0]
    assert upsert is True
    assert update["$set"]["last_event_type"] == "message_update"
    assert update["$inc"]["event_counts.message_update"] == 1
    assert "notified_at" not in update["$set"]
    assert "first_notified_at" not in update["$set"]
    assert "cooldown_until" not in update["$set"]
    assert "alert_count" not in update["$inc"]


@pytest.mark.asyncio
async def test_failed_first_delivery_then_success_is_typed_as_retry_but_counts_one_alert(monkeypatch):
    failed = FakeCollection({"delivery_attempts": 1})
    monkeypatch.setattr(telemetry, "get_db", lambda: FakeDb(failed))

    await telemetry._persist_event(_payload())

    assert len(failed.calls) == 2
    _query, event_update, _upsert = failed.calls[0]
    assert event_update["$set"]["last_event_type"] == "retry"
    assert event_update["$inc"]["event_counts.retry"] == 1
    _query, first_update, _upsert = failed.calls[1]
    assert first_update["$inc"]["alert_count"] == 1


@pytest.mark.asyncio
async def test_failed_delivery_is_recorded_without_creating_first_notification_time(monkeypatch):
    collection = FakeCollection()
    monkeypatch.setattr(telemetry, "get_db", lambda: FakeDb(collection))

    await telemetry._persist_event(
        _payload(
            delivered=False,
            output_message_id=None,
            delivery_detail="telegram_error:TimedOut",
        )
    )

    assert len(collection.calls) == 1
    _query, update, _upsert = collection.calls[0]
    assert update["$set"]["last_event_type"] == "failed_delivery"
    assert update["$set"]["last_logical_event_type"] == "first_notification"
    assert update["$inc"]["failed_delivery_count"] == 1
    assert update["$inc"]["event_counts.failed_delivery"] == 1
    assert "notified_at" not in update["$set"]
    assert "alert_count" not in update["$inc"]


def test_lifecycle_event_classification_is_explicit():
    assert telemetry._logical_event_type("prepare", None) == "first_notification"
    assert telemetry._logical_event_type("camera_ready", 100) == "message_update"
    assert telemetry._logical_event_type("cancelled", 100) == "cancellation_update"
    assert telemetry._logical_event_type("passed", 100) == "passed_update"


def test_runtime_does_not_install_duplicate_route_or_notification_wrappers():
    worker_init = Path("app/worker/__init__.py").read_text()
    assert "install_route_history_read_guard_v44" not in worker_init
    assert "install_notification_telemetry_v44" not in worker_init
    assert "install_route_guard_v2()" in worker_init
    assert "install_route_observe_guard_v44()" in worker_init
