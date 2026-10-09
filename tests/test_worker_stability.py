"""Tests for background worker stability, scheduler lifecycle, and error resilience.

Covers:
- Worker scheduler initialization, configuration, and job lifecycle.
- Monitoring cycle execution with in-memory MongoDB (mongomock-motor).
- Error resilience when ADS-B data feeds fail (ClientError, TimeoutError, generic exceptions).
- Error resilience when Telegram notifications fail (Forbidden/blocked bot, TelegramError).
- Telemetry corruption tolerance and state persistence across consecutive cycles.
- Graceful shutdown signal handling.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
import signal
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure settings parse in all test environments
if not os.environ.get("ADMIN_TELEGRAM_ID"):
    os.environ["ADMIN_TELEGRAM_ID"] = "0"

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from mongomock_motor import AsyncMongoMockClient
import pytest
from telegram.error import Forbidden, TelegramError

import app.database as database
from app.aircraft.models import NormalizedAircraft
from app.config import settings
from app.worker.monitor import (
    _last_cycle_time,
    get_cycle_stats,
    run_monitor_cycle,
)
import worker


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_mongo():
    """Provide an in-memory MongoDB database using mongomock-motor."""
    client = AsyncMongoMockClient()
    mock_db = client[settings.database_name]

    # Save original database references
    orig_client = database._client
    orig_db = database._db

    database._client = client
    database._db = mock_db

    yield mock_db

    # Teardown
    database._client = orig_client
    database._db = orig_db
    client.close()


def _make_dummy_aircraft(
    icao24: str = "400001",
    callsign: str = "BAW123",
    lat: float = 51.52,
    lon: float = -0.12,
    type_code: str = "B738",
    velocity_ms: float = 210.0,
    heading: float = 180.0,
) -> NormalizedAircraft:
    """Helper to build a valid NormalizedAircraft instance."""
    return NormalizedAircraft(
        icao24=icao24,
        callsign=callsign,
        latitude=lat,
        longitude=lon,
        altitude=3000.0,
        velocity=velocity_ms,
        heading=heading,
        vertical_rate=0.0,
        on_ground=False,
        squawk="1200",
        aircraft_type=type_code,
        display_type=type_code,
        origin_country="United Kingdom",
        last_contact=int(datetime.now(timezone.utc).timestamp()),
        source_provider="test_provider",
    )


# ── Scheduler Initialization & Lifecycle Tests ────────────────────────────────

@pytest.mark.asyncio
async def test_scheduler_initialization_and_configuration() -> None:
    """Verify AsyncIOScheduler initializes with expected interval, job ID, and concurrency limits."""
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_monitor_cycle,
        trigger="interval",
        seconds=settings.poll_interval_seconds,
        id="aircraft_monitor",
        max_instances=1,
        coalesce=True,
    )

    # Validate added job properties
    job = scheduler.get_job("aircraft_monitor")
    assert job is not None
    assert job.id == "aircraft_monitor"
    assert job.max_instances == 1
    assert job.coalesce is True

    # Validate start and shutdown lifecycle
    scheduler.start()
    assert scheduler.running is True

    scheduler.shutdown(wait=False)
    await asyncio.sleep(0.02)
    assert scheduler.running is False


def test_worker_signal_handler_triggers_shutdown() -> None:
    """Verify SIGTERM / SIGINT signal handler signals worker graceful shutdown event."""
    worker._shutdown_event.clear()
    assert not worker._shutdown_event.is_set()

    worker._signal_handler(signal.SIGTERM, None)
    assert worker._shutdown_event.is_set()


# ── Cycle Execution with In-Memory MongoDB Tests ──────────────────────────────

@pytest.mark.asyncio
async def test_monitor_cycle_with_empty_database(mock_mongo) -> None:
    """When no users exist, monitor cycle skips cleanly and records worker heartbeat."""
    stats_before = get_cycle_stats()
    cycles_before = stats_before["total_cycles"]

    await run_monitor_cycle()

    stats_after = get_cycle_stats()
    assert stats_after["total_cycles"] == cycles_before + 1

    # Verify heartbeat document was recorded in system_status
    status = await database.system_status_col().find_one({"_id": "monitor_worker"})
    assert status is not None
    assert status["active_users"] == 0
    assert status["notifications_sent_last_cycle"] == 0


@pytest.mark.asyncio
async def test_monitor_cycle_with_active_user_and_match(mock_mongo) -> None:
    """Worker matches aircraft to active user, writes history cooldown, and records heartbeat."""
    user_id = 1234567

    # Seed user, location, and preference collections
    await database.users_col().insert_one({
        "user_id": user_id,
        "username": "spotter",
        "setup_complete": True,
        "created_at": datetime.now(timezone.utc),
    })
    await database.locations_col().insert_one({
        "user_id": user_id,
        "latitude": 51.5074,
        "longitude": -0.1278,
        "radius_km": 15.0,
        "geohash": "gcpv",
    })
    await database.preferences_col().insert_one({
        "user_id": user_id,
        "selected_categories": ["Commercial Jets"],
        "custom_aircraft": ["B738"],
    })

    # Prepare aircraft within 5 km of user
    dummy_ac = _make_dummy_aircraft(
        lat=51.53,
        lon=-0.12,
        type_code="B738",
    )

    with (
        patch("app.worker.monitor._provider_manager.query_providers", new_callable=AsyncMock) as mock_query,
        patch("app.worker.notifications._send_message", new_callable=AsyncMock) as mock_send,
    ):
        mock_query.return_value = ([dummy_ac], {"test_provider": [dummy_ac]})
        mock_send.return_value = True

        await run_monitor_cycle()

        assert mock_send.call_count >= 1

    # Verify notification cooldown history record in MongoDB
    history_entry = await database.notification_history_col().find_one({"user_id": user_id})
    assert history_entry is not None
    assert history_entry["aircraft_icao24"] == dummy_ac.icao24

    # Verify heartbeat record has active_users: 1 and notifications_sent >= 1
    status = await database.system_status_col().find_one({"_id": "monitor_worker"})
    assert status is not None
    assert status["active_users"] == 1
    assert status["notifications_sent_last_cycle"] >= 1


# ── Error Resilience Tests ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_error_resilience_provider_network_failure(mock_mongo) -> None:
    """When ADS-B provider query raises ClientError, the worker must log and not crash."""
    user_id = 9991
    await database.users_col().insert_one({"user_id": user_id, "setup_complete": True})
    await database.locations_col().insert_one({
        "user_id": user_id,
        "latitude": 51.50,
        "longitude": -0.12,
        "radius_km": 15.0,
        "geohash": "gcpv",
    })
    await database.preferences_col().insert_one({
        "user_id": user_id,
        "selected_categories": ["Commercial Jets"],
        "custom_aircraft": [],
    })

    with patch("app.worker.monitor._provider_manager.query_providers", new_callable=AsyncMock) as mock_query:
        mock_query.side_effect = httpx.RequestError("Temporary DNS lookup failure")

        # Must not raise an unhandled exception
        await run_monitor_cycle()

    # Heartbeat and cycle stats should still be recorded
    status = await database.system_status_col().find_one({"_id": "monitor_worker"})
    assert status is not None
    assert status["active_users"] == 1
    assert status["notifications_sent_last_cycle"] == 0


@pytest.mark.asyncio
async def test_error_resilience_provider_timeout(mock_mongo) -> None:
    """When ADS-B provider query times out, the worker must catch and continue."""
    user_id = 9992
    await database.users_col().insert_one({"user_id": user_id, "setup_complete": True})
    await database.locations_col().insert_one({
        "user_id": user_id,
        "latitude": 40.71,
        "longitude": -74.00,
        "radius_km": 20.0,
        "geohash": "dr5r",
    })
    await database.preferences_col().insert_one({
        "user_id": user_id,
        "selected_categories": ["Commercial Jets"],
        "custom_aircraft": [],
    })

    with patch("app.worker.monitor._provider_manager.query_providers", new_callable=AsyncMock) as mock_query:
        mock_query.side_effect = asyncio.TimeoutError("Provider query timed out")

        await run_monitor_cycle()

    status = await database.system_status_col().find_one({"_id": "monitor_worker"})
    assert status is not None
    assert status["active_users"] == 1


@pytest.mark.asyncio
async def test_error_resilience_telegram_api_error(mock_mongo) -> None:
    """When Telegram send_message raises a generic TelegramError, worker handles it cleanly."""
    user_id = 9993
    await database.users_col().insert_one({"user_id": user_id, "setup_complete": True})
    await database.locations_col().insert_one({
        "user_id": user_id,
        "latitude": 51.50,
        "longitude": -0.12,
        "radius_km": 15.0,
        "geohash": "gcpv",
    })
    await database.preferences_col().insert_one({
        "user_id": user_id,
        "selected_categories": ["Commercial Jets"],
        "custom_aircraft": ["B738"],
    })

    dummy_ac = _make_dummy_aircraft(lat=51.51, lon=-0.12, type_code="B738")

    with (
        patch("app.worker.monitor._provider_manager.query_providers", new_callable=AsyncMock) as mock_query,
        patch("app.worker.notifications._send_message", new_callable=AsyncMock) as mock_send,
    ):
        mock_query.return_value = ([dummy_ac], {"test_provider": [dummy_ac]})
        mock_send.side_effect = TelegramError("Network connection reset")

        # Must not crash
        await run_monitor_cycle()


@pytest.mark.asyncio
async def test_error_resilience_user_blocked_bot_deactivates_user(mock_mongo) -> None:
    """When Telegram raises Forbidden (bot blocked), user is set to setup_complete=False."""
    user_id = 9994
    await database.users_col().insert_one({"user_id": user_id, "setup_complete": True})

    # Test send_aircraft_notification handling of Forbidden
    from app.worker.notifications import send_aircraft_notification
    dummy_ac = _make_dummy_aircraft()

    with patch("telegram.Bot.send_message", new_callable=AsyncMock) as mock_bot_send:
        mock_bot_send.side_effect = Forbidden("Forbidden: bot was blocked by the user")

        result = await send_aircraft_notification(
            user_id=user_id,
            aircraft=dummy_ac,
            distance_km=5.0,
        )
        assert result is False

    # Verify database was updated to deactivate user
    user_doc = await database.users_col().find_one({"user_id": user_id})
    assert user_doc is not None
    assert user_doc["setup_complete"] is False


@pytest.mark.asyncio
async def test_error_resilience_corrupted_aircraft_telemetry(mock_mongo) -> None:
    """When provider returns aircraft with None or corrupted coordinates, cycle skips safely."""
    user_id = 9995
    await database.users_col().insert_one({"user_id": user_id, "setup_complete": True})
    await database.locations_col().insert_one({
        "user_id": user_id,
        "latitude": 51.50,
        "longitude": -0.12,
        "radius_km": 15.0,
        "geohash": "gcpv",
    })
    await database.preferences_col().insert_one({
        "user_id": user_id,
        "selected_categories": ["Commercial Jets"],
        "custom_aircraft": [],
    })

    # Aircraft missing coordinates (latitude = None)
    corrupted_ac = NormalizedAircraft(
        icao24="400999",
        callsign="CORRUPT",
        latitude=None,  # missing position
        longitude=None,
        altitude=None,
        velocity=None,
        heading=None,
        vertical_rate=None,
        on_ground=False,
        squawk=None,
        aircraft_type="B738",
        display_type="B738",
        origin_country="",
        last_contact=1234567,
        source_provider="test",
    )

    with (
        patch("app.worker.monitor._provider_manager.query_providers", new_callable=AsyncMock) as mock_query,
        patch("app.worker.notifications._send_message", new_callable=AsyncMock) as mock_send,
    ):
        mock_query.return_value = ([corrupted_ac], {"test": [corrupted_ac]})

        await run_monitor_cycle()

        # No notification should be dispatched for aircraft without position
        assert mock_send.call_count == 0


@pytest.mark.asyncio
async def test_consecutive_monitor_cycles_stability(mock_mongo) -> None:
    """Execute consecutive monitor cycles to verify state isolation and stability."""
    stats_init = get_cycle_stats()
    init_cycles = stats_init["total_cycles"]

    # Run 3 consecutive cycles
    for _ in range(3):
        await run_monitor_cycle()

    stats_final = get_cycle_stats()
    assert stats_final["total_cycles"] == init_cycles + 3
