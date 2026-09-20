"""Bounded post-delivery notification telemetry for Plane Alerts.

Telegram delivery is the foreground operation. Metrics, photo snapshots and
other audit writes are explicitly secondary and run through one bounded FIFO
worker so ordinary message edits cannot delay or inflate live alerts.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.database import get_db

logger = logging.getLogger(__name__)

_QUEUE_LIMIT = 256
_WORK_TIMEOUT_S = 4.0
_queue: asyncio.Queue | None = None
_worker: asyncio.Task | None = None
_dropped = 0
_failures = 0


def _logical_event_type(stage: str, input_message_id: int | None) -> str:
    if input_message_id is None:
        return "first_notification"
    if stage == "cancelled":
        return "cancellation_update"
    if stage == "passed":
        return "passed_update"
    return "message_update"


async def _persist_event(payload: dict[str, Any]) -> None:
    collection = get_db()["notification_history"]
    notification_id = str(payload["notification_id"])
    occurred_at = payload.get("occurred_at") or datetime.now(timezone.utc)
    delivered = bool(payload.get("delivered"))
    input_message_id = payload.get("input_message_id")
    output_message_id = payload.get("output_message_id")
    stage = str(payload.get("stage") or "")
    logical_type = _logical_event_type(stage, input_message_id)

    existing = await collection.find_one(
        {"_id": notification_id},
        {"first_notified_at": 1, "delivery_attempts": 1},
    ) or {}
    prior_attempts = int(existing.get("delivery_attempts", 0) or 0)
    has_first_delivery = existing.get("first_notified_at") is not None
    retrying_first_delivery = input_message_id is None and prior_attempts > 0 and not has_first_delivery

    if not delivered:
        event_type = "failed_delivery"
    elif retrying_first_delivery:
        event_type = "retry"
    else:
        event_type = logical_type

    attempt_type = (
        "retry"
        if retrying_first_delivery
        else ("initial" if input_message_id is None else "update")
    )
    event = {
        "event_type": event_type,
        "logical_event_type": logical_type,
        "attempt_type": attempt_type,
        "stage": stage,
        "delivered": delivered,
        "occurred_at": occurred_at,
        "input_message_id": input_message_id,
        "output_message_id": output_message_id,
        "delivery_detail": payload.get("delivery_detail") or "",
    }

    increments: dict[str, int] = {
        "delivery_attempts": 1,
        f"event_counts.{event_type}": 1,
    }
    if not delivered:
        increments["failed_delivery_count"] = 1

    await collection.update_one(
        {"_id": notification_id},
        {
            "$set": {
                "user_id": payload.get("user_id"),
                "aircraft_icao24": payload.get("aircraft_icao24") or "",
                "aircraft_type": payload.get("aircraft_type") or "",
                "distance_km": payload.get("distance_km"),
                "projected_closest_km": payload.get("projected_closest_km"),
                "observed_closest_km": payload.get("observed_closest_km"),
                "trajectory_state": payload.get("trajectory_state") or "",
                "prediction_confidence": payload.get("prediction_confidence") or "",
                "last_event_type": event_type,
                "last_logical_event_type": logical_type,
                "last_event_at": occurred_at,
                "last_delivery_success": delivered,
                "last_message_id": output_message_id,
            },
            "$inc": increments,
            "$push": {"events": {"$each": [event], "$slice": -64}},
        },
        upsert=True,
    )

    # ``notified_at`` is a legacy analysis field. From v4.4 onward it means the
    # first successful logical alert only and is never refreshed by edits.
    if delivered and logical_type == "first_notification" and not has_first_delivery:
        await collection.update_one(
            {"_id": notification_id, "first_notified_at": {"$exists": False}},
            {
                "$set": {
                    "first_notified_at": occurred_at,
                    "notified_at": occurred_at,
                    "cooldown_until": occurred_at + timedelta(minutes=settings.cooldown_minutes),
                    "first_message_id": output_message_id,
                },
                "$inc": {"alert_count": 1},
            },
        )


async def _run_item(
    payload: dict[str, Any] | None,
    auxiliary: Callable[[], Awaitable[None]] | None,
) -> None:
    if payload is not None:
        await _persist_event(payload)
    if auxiliary is not None:
        await auxiliary()


async def _worker_loop(queue: asyncio.Queue) -> None:
    global _failures
    while True:
        payload, auxiliary, label = await queue.get()
        try:
            await asyncio.wait_for(_run_item(payload, auxiliary), timeout=_WORK_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception:
            _failures += 1
            logger.exception("notification_background_work_failed kind=%s", label)
        finally:
            queue.task_done()


def _ensure_worker() -> asyncio.Queue:
    global _queue, _worker
    if _queue is None:
        _queue = asyncio.Queue(maxsize=_QUEUE_LIMIT)
    if _worker is None or _worker.done():
        _worker = asyncio.create_task(_worker_loop(_queue), name="notification-telemetry")
    return _queue


def _enqueue(
    payload: dict[str, Any] | None,
    auxiliary: Callable[[], Awaitable[None]] | None,
    *,
    label: str,
) -> bool:
    global _dropped
    queue = _ensure_worker()
    try:
        queue.put_nowait((payload, auxiliary, label))
        return True
    except asyncio.QueueFull:
        _dropped += 1
        logger.warning(
            "notification_background_queue_full pending=%d limit=%d kind=%s",
            queue.qsize(),
            _QUEUE_LIMIT,
            label,
        )
        return False


def enqueue_notification_event(
    payload: dict[str, Any],
    *,
    auxiliary: Callable[[], Awaitable[None]] | None = None,
) -> bool:
    """Queue one delivery result without waiting for MongoDB/photo telemetry."""
    event_payload = dict(payload)
    event_payload.setdefault("occurred_at", datetime.now(timezone.utc))
    return _enqueue(event_payload, auxiliary, label="notification_event")


def enqueue_auxiliary(
    auxiliary: Callable[[], Awaitable[None]],
    *,
    label: str = "auxiliary",
) -> bool:
    return _enqueue(None, auxiliary, label=label)


def snapshot() -> dict[str, int]:
    return {
        "pending": _queue.qsize() if _queue is not None else 0,
        "dropped": _dropped,
        "failures": _failures,
    }


async def close() -> None:
    global _worker
    if _worker is not None and not _worker.done():
        _worker.cancel()
        await asyncio.gather(_worker, return_exceptions=True)
    _worker = None
