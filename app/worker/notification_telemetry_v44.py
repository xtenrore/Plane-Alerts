"""Plane Alerts v4.4 notification telemetry guard.

The lifecycle reuses one Telegram message. An edit, cancellation update or passed
update is not a new alert and must never move the first-notification timestamp.

The legacy sender wrote notification_history before Telegram delivery and reset
``notified_at`` on every edit. This guard captures that legacy write in-memory,
lets Telegram delivery happen first, then persists explicit event types through
bounded background work. Failed deliveries are logged but never create a
countable notification_history record.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from app.config import settings
from app.worker import notifications

logger = logging.getLogger(__name__)

_QUEUE_LIMIT = 64
_WORKERS = 2
_WRITE_TIMEOUT_S = 2.0
_MAX_QUEUE_AGE_S = 20.0
_EVENT_HISTORY_LIMIT = 32
_INSTALLED = False

_ORIGINAL_GET_DB = notifications.get_db
_ORIGINAL_SEND = notifications.send_or_update_approach
_CAPTURE: contextvars.ContextVar[list[tuple[dict[str, Any], dict[str, Any], bool]] | None] = (
    contextvars.ContextVar("plane_alerts_v44_notification_capture", default=None)
)
_queue: asyncio.Queue | None = None
_workers: list[asyncio.Task] = []


class _NotificationHistoryProxy:
    def __init__(self, collection: Any) -> None:
        self._collection = collection

    async def update_one(self, query: dict[str, Any], update: dict[str, Any], *, upsert: bool = False, **kwargs: Any) -> Any:
        capture = _CAPTURE.get()
        if capture is None:
            return await self._collection.update_one(query, update, upsert=upsert, **kwargs)
        capture.append((dict(query), dict(update), bool(upsert)))
        return SimpleNamespace(acknowledged=True, matched_count=0, modified_count=0, upserted_id=None)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._collection, name)


class _DbProxy:
    def __init__(self, db: Any) -> None:
        self._db = db

    def __getitem__(self, name: str) -> Any:
        collection = self._db[name]
        if name == "notification_history":
            return _NotificationHistoryProxy(collection)
        return collection

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


def _capturing_get_db() -> Any:
    return _DbProxy(_ORIGINAL_GET_DB())


def classify_event(stage: str, previous_message_id: int | None, delivered_message_id: int | None) -> str:
    if delivered_message_id is None:
        return "failed_delivery"
    if previous_message_id is None:
        return "first_notification"
    if int(delivered_message_id) != int(previous_message_id):
        return "retry"
    if stage == "cancelled":
        return "cancellation_update"
    if stage == "passed":
        return "passed_update"
    return "message_update"


def _captured_fields(capture: list[tuple[dict[str, Any], dict[str, Any], bool]]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for _query, update, _upsert in capture:
        values = update.get("$set") if isinstance(update, dict) else None
        if isinstance(values, dict):
            fields.update(values)
    fields.pop("notified_at", None)
    fields.pop("cooldown_until", None)
    return fields


def build_history_update(
    *,
    event_type: str,
    stage: str,
    event_at: datetime,
    message_id: int | None,
    base_fields: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Build a backward-compatible history update without moving first alert time."""
    values = dict(base_fields)
    values.update(
        {
            "last_event_type": event_type,
            "last_event_at": event_at,
            "last_stage": stage,
            "last_message_id": message_id,
            "delivery_status": "success",
        }
    )
    event = {
        "type": event_type,
        "stage": stage,
        "at": event_at,
        "message_id": message_id,
    }
    update: dict[str, Any] = {
        "$set": values,
        "$push": {
            "events": {
                "$each": [event],
                "$slice": -_EVENT_HISTORY_LIMIT,
            }
        },
    }
    first = event_type == "first_notification"
    if first:
        update["$setOnInsert"] = {
            "notified_at": event_at,
            "first_notified_at": event_at,
            "cooldown_until": event_at + timedelta(minutes=settings.cooldown_minutes),
        }
    return update, first


async def _persist(record: dict[str, Any]) -> None:
    event_type = str(record["event_type"])
    if event_type == "failed_delivery":
        logger.warning(
            "notification_event type=failed_delivery user=%s icao=%s stage=%s notification_id=%s",
            record["user_id"],
            record["aircraft_icao24"],
            record["stage"],
            record["notification_id"],
        )
        return

    event_at = record["event_at"]
    update, upsert = build_history_update(
        event_type=event_type,
        stage=str(record["stage"]),
        event_at=event_at,
        message_id=record.get("delivered_message_id"),
        base_fields=record["base_fields"],
    )
    try:
        collection = _ORIGINAL_GET_DB()["notification_history"]
        await asyncio.wait_for(
            collection.update_one(
                {"_id": record["notification_id"]},
                update,
                upsert=upsert,
            ),
            timeout=_WRITE_TIMEOUT_S,
        )
        logger.info(
            "notification_event type=%s user=%s icao=%s stage=%s notification_id=%s message_id=%s",
            event_type,
            record["user_id"],
            record["aircraft_icao24"],
            record["stage"],
            record["notification_id"],
            record.get("delivered_message_id"),
        )
    except asyncio.TimeoutError:
        logger.warning(
            "notification_telemetry_timeout type=%s notification_id=%s timeout_s=%.1f",
            event_type,
            record["notification_id"],
            _WRITE_TIMEOUT_S,
        )
    except Exception:
        logger.exception(
            "notification_telemetry_failed type=%s notification_id=%s",
            event_type,
            record["notification_id"],
        )


async def _worker(queue: asyncio.Queue) -> None:
    while True:
        record, queued_mono = await queue.get()
        try:
            if time.monotonic() - queued_mono <= _MAX_QUEUE_AGE_S:
                await _persist(record)
            else:
                logger.info(
                    "notification_telemetry_dropped type=%s notification_id=%s reason=stale_queue",
                    record["event_type"],
                    record["notification_id"],
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("notification_telemetry_worker_failed")
        finally:
            queue.task_done()


def _ensure_workers() -> asyncio.Queue:
    global _queue, _workers
    if _queue is None:
        _queue = asyncio.Queue(maxsize=_QUEUE_LIMIT)
    if not _workers or any(worker.done() for worker in _workers):
        for worker in _workers:
            if not worker.done():
                worker.cancel()
        _workers = [
            asyncio.create_task(_worker(_queue), name=f"notification-telemetry-v44:{index}")
            for index in range(_WORKERS)
        ]
    return _queue


def _enqueue(record: dict[str, Any]) -> None:
    queue = _ensure_workers()
    try:
        queue.put_nowait((record, time.monotonic()))
    except asyncio.QueueFull:
        logger.warning(
            "notification_telemetry_queue_full pending=%d limit=%d type=%s notification_id=%s",
            queue.qsize(),
            _QUEUE_LIMIT,
            record["event_type"],
            record["notification_id"],
        )


async def send_or_update_approach_v44(
    user_id: int,
    aircraft: Any,
    prediction: Any,
    stage: str,
    notification_id: str,
    message_id: int | None = None,
    **kwargs: Any,
) -> int | None:
    capture: list[tuple[dict[str, Any], dict[str, Any], bool]] = []
    token = _CAPTURE.set(capture)
    try:
        delivered_message_id = await _ORIGINAL_SEND(
            user_id,
            aircraft,
            prediction,
            stage,
            notification_id,
            message_id,
            **kwargs,
        )
    finally:
        _CAPTURE.reset(token)

    if notification_id:
        event_type = classify_event(stage, message_id, delivered_message_id)
        base_fields = _captured_fields(capture)
        base_fields.setdefault("user_id", user_id)
        base_fields.setdefault("aircraft_icao24", getattr(aircraft, "icao24", ""))
        base_fields.setdefault("aircraft_type", getattr(aircraft, "aircraft_type", ""))
        base_fields.setdefault("distance_km", getattr(prediction, "current_distance_km", None))
        base_fields.setdefault("projected_closest_km", getattr(prediction, "projected_closest_km", None))
        base_fields.setdefault("trajectory_state", getattr(prediction, "state", ""))
        base_fields.setdefault("prediction_confidence", getattr(prediction, "confidence", ""))
        _enqueue(
            {
                "notification_id": notification_id,
                "user_id": user_id,
                "aircraft_icao24": getattr(aircraft, "icao24", ""),
                "stage": stage,
                "event_type": event_type,
                "event_at": datetime.now(timezone.utc),
                "previous_message_id": message_id,
                "delivered_message_id": delivered_message_id,
                "base_fields": base_fields,
            }
        )
    return delivered_message_id


def install_notification_telemetry_v44() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    # Capture the legacy pre-delivery history write instead of allowing it to
    # block Telegram or overwrite first-notification timing.
    notifications.get_db = _capturing_get_db
    notifications.send_or_update_approach = send_or_update_approach_v44

    # monitor.py imports the sender by name, so update that bound symbol too.
    from app.worker import monitor

    monitor.send_or_update_approach = send_or_update_approach_v44
    _INSTALLED = True
    logger.info(
        "Notification telemetry v4.4 enabled: first alerts immutable, lifecycle edits typed, queue=%d workers=%d",
        _QUEUE_LIMIT,
        _WORKERS,
    )
