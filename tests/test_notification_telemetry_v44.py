from datetime import datetime, timezone

from app.worker import notification_telemetry_v44 as telemetry


def test_first_notification_timestamp_is_insert_only():
    now = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
    update, upsert = telemetry.build_history_update(
        event_type="first_notification",
        stage="prepare",
        event_at=now,
        message_id=100,
        base_fields={"user_id": 1, "distance_km": 12.0},
    )

    assert upsert is True
    assert update["$setOnInsert"]["notified_at"] == now
    assert update["$setOnInsert"]["first_notified_at"] == now
    assert update["$set"]["last_event_type"] == "first_notification"
    assert update["$push"]["events"]["$each"][0]["type"] == "first_notification"


def test_message_edit_never_overwrites_first_notification_time():
    now = datetime(2026, 9, 20, 8, 5, tzinfo=timezone.utc)
    update, upsert = telemetry.build_history_update(
        event_type="message_update",
        stage="camera_ready",
        event_at=now,
        message_id=100,
        base_fields={"user_id": 1, "notified_at": "legacy-value", "cooldown_until": "legacy-value"},
    )

    assert upsert is False
    assert "$setOnInsert" not in update
    assert "notified_at" not in update["$set"]
    assert "cooldown_until" not in update["$set"]
    assert update["$set"]["last_event_type"] == "message_update"


def test_lifecycle_event_classification_is_explicit():
    assert telemetry.classify_event("prepare", None, 100) == "first_notification"
    assert telemetry.classify_event("camera_ready", 100, 100) == "message_update"
    assert telemetry.classify_event("cancelled", 100, 100) == "cancellation_update"
    assert telemetry.classify_event("passed", 100, 100) == "passed_update"
    assert telemetry.classify_event("prepare", 100, 101) == "retry"
    assert telemetry.classify_event("prepare", None, None) == "failed_delivery"


def test_event_history_is_bounded():
    now = datetime(2026, 9, 20, 8, 5, tzinfo=timezone.utc)
    update, _ = telemetry.build_history_update(
        event_type="message_update",
        stage="photo_now",
        event_at=now,
        message_id=100,
        base_fields={},
    )
    assert update["$push"]["events"]["$slice"] == -telemetry._EVENT_HISTORY_LIMIT
    assert telemetry._EVENT_HISTORY_LIMIT == 32
    assert telemetry._QUEUE_LIMIT == 64
    assert telemetry._WORKERS == 2
