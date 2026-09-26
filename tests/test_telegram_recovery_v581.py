"""Exercise the installed Telegram callback, including success and retry paths."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pymongo.errors import AutoReconnect
from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.ext import Application, ApplicationHandlerStop, ExtBot

from app.bot import interaction_v46 as interaction
from app.bot import next60
from app.bot.flight_links import FLIGHTRADAR24_DATA_URL
from app.bot import storage_failure_v48 as recovery


@pytest.fixture(autouse=True)
def reset_callback_state():
    interaction.clear_latency_state_for_tests()
    yield
    interaction.clear_latency_state_for_tests()


def registered_more_callback():
    # Use the same installation/registration path as app.main, not the original
    # function that production replaces during bootstrap.
    import app.bot.profile_legacy  # noqa: F401

    bot = ExtBot("123456:test-only-token", request=AsyncMock(), get_updates_request=AsyncMock())
    application = Application.builder().bot(bot).build()
    next60.register_next60_handlers(application)
    return application.handlers[-1][0].callback


def make_update(doc, query_id="detail-query"):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        callback_query=SimpleNamespace(
            id=query_id,
            data=next60.MORE_PREFIX + next60._callback_token(doc),
            answer=AsyncMock(return_value=True),
            message=SimpleNamespace(reply_text=AsyncMock()),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity, expected_url",
    [
        ({"callsign": "THY123", "aircraft_icao24": "4bb0e6"}, "https://www.flightradar24.com/THY123"),
        ({"callsign": "THY123", "source": "history"}, "https://www.flightradar24.com/THY123"),
        ({"callsign": "4BB0E6", "aircraft_icao24": "4bb0e6"}, FLIGHTRADAR24_DATA_URL),
        ({"aircraft_icao24": "invalid"}, None),
    ],
)
async def test_registered_more_info_sends_detail_and_correct_tracker(monkeypatch, identity, expected_url):
    callback = registered_more_callback()
    doc = {
        "aircraft_type": "A359", "predicted_closest_km": 4.2,
        "predicted_cpa_at": datetime.now(timezone.utc) + timedelta(minutes=8),
        "source": "live", **identity,
    }
    update = make_update(doc)

    async def build_docs(user_id, now):
        update.callback_query.answer.assert_awaited_once()
        assert user_id == 42
        return [doc]

    monkeypatch.setattr(next60, "build_next60_docs", build_docs)
    with pytest.raises(ApplicationHandlerStop):
        await callback(update, None)
    reply = update.callback_query.message.reply_text
    reply.assert_awaited_once()
    assert "A359" in reply.call_args.args[0]
    assert "4.2 km" in reply.call_args.args[0]
    keyboard = reply.call_args.kwargs["reply_markup"]
    if expected_url is None:
        assert keyboard is None
    else:
        button = keyboard.inline_keyboard[0][0]
        assert button.text == "Open in Flightradar24"
        assert button.url == expected_url


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["ack", "database", "reply"])
async def test_failed_more_info_can_retry_same_update_without_duplicate_delivery(monkeypatch, failure_stage):
    callback = registered_more_callback()
    doc = {"callsign": "THY123", "aircraft_type": "A359", "source": "live"}
    update = make_update(doc)
    build = AsyncMock(return_value=[doc])
    monkeypatch.setattr(next60, "build_next60_docs", build)
    reply = update.callback_query.message.reply_text
    failing = {"ack": update.callback_query.answer, "database": build, "reply": reply}[failure_stage]
    failing.side_effect = RuntimeError("synthetic temporary failure")

    with pytest.raises(RuntimeError):
        await callback(update, None)
    failing.side_effect = None
    reply.reset_mock()
    with pytest.raises(ApplicationHandlerStop):
        await callback(update, None)
    reply.assert_awaited_once()

    # Once delivered, a duplicate update still cannot repeat the detail.
    with pytest.raises(ApplicationHandlerStop):
        await callback(update, None)
    reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_uninstrumented_answers_do_not_accumulate_ids(monkeypatch):
    registered_more_callback()
    monkeypatch.setattr(interaction, "_original_answer", AsyncMock(return_value=True))
    for index in range(2 * interaction._RECEIVED_MAX):
        await interaction._answer_with_timing(SimpleNamespace(id=f"untracked-{index}"))
    assert not interaction._acked
    assert not interaction._received


@pytest.mark.asyncio
async def test_ack_metrics_count_only_success_and_expire(monkeypatch):
    registered_more_callback()
    clock = [100.0]
    monkeypatch.setattr(interaction.time, "monotonic", lambda: clock[0])
    answer = AsyncMock(side_effect=RuntimeError("temporary answer failure"))
    monkeypatch.setattr(interaction, "_original_answer", answer)
    query = SimpleNamespace(id="tracked")
    interaction.mark_callback_received(query, "next60_more")
    with pytest.raises(RuntimeError):
        await interaction._answer_with_timing(query)
    assert query.id not in interaction._acked
    assert not interaction.latency_snapshot()

    answer.side_effect = None
    clock[0] += 0.1
    await interaction._answer_with_timing(query)
    await interaction._answer_with_timing(query)
    assert interaction.latency_snapshot("next60_more")["next60_more"]["ack_ms"]["count"] == 1
    clock[0] += interaction._RECEIVED_TTL_S + 1
    interaction._prune(clock[0])
    assert not interaction._acked
    assert not interaction._received


def telegram_update(callback=False):
    user = User(id=42, first_name="Test", is_bot=False)
    message = Message(
        message_id=1, date=datetime.now(timezone.utc),
        chat=Chat(id=42, type="private"), from_user=user, text="/next60",
    )
    if callback:
        return Update(update_id=1, callback_query=CallbackQuery(
            id="failed-action", from_user=user, chat_instance="test", message=message, data="n60_more:THY123",
        ))
    return Update(update_id=1, message=message)


@pytest.mark.asyncio
@pytest.mark.parametrize("callback", [False, True])
async def test_unexpected_failure_reports_safe_location_and_user_recovery(monkeypatch, caplog, callback):
    reply = AsyncMock()
    answer = AsyncMock(side_effect=RuntimeError("callback already expired"))
    monkeypatch.setattr(Message, "reply_text", reply)
    monkeypatch.setattr(CallbackQuery, "answer", answer)
    secret = "token-that-must-never-be-logged"
    location = "41.123456,29.123456"

    # Raise in a real application function. The error text and arguments may
    # contain private input; only module/function/line breadcrumbs are safe.
    try:
        next60._distance_text(None)
    except AttributeError as exc:
        exc.args = (f"{secret} {location}",)
        failure = exc

    with caplog.at_level(logging.ERROR):
        await recovery._storage_error_handler(telegram_update(callback), SimpleNamespace(error=failure))
    reply.assert_awaited_once()
    assert "/help" in reply.call_args.args[0]
    assert "telegram_handler_failed error=AttributeError" in caplog.text
    assert "app.bot.next60:_distance_text:" in caplog.text
    assert secret not in caplog.text
    assert location not in caplog.text
    assert secret not in reply.call_args.args[0]
    assert location not in reply.call_args.args[0]


@pytest.mark.asyncio
async def test_error_notice_failure_is_contained_but_cancellation_propagates(monkeypatch):
    monkeypatch.setattr(Message, "reply_text", AsyncMock(side_effect=RuntimeError("offline")))
    await recovery._storage_error_handler(telegram_update(), SimpleNamespace(error=ValueError("failure")))
    with pytest.raises(asyncio.CancelledError):
        await recovery._storage_error_handler(telegram_update(), SimpleNamespace(error=asyncio.CancelledError()))

    monkeypatch.setattr(Message, "reply_text", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await recovery._storage_error_handler(telegram_update(), SimpleNamespace(error=ValueError("failure")))


@pytest.mark.asyncio
async def test_storage_outage_keeps_existing_recovery_message(monkeypatch, caplog):
    reply = AsyncMock()
    monkeypatch.setattr(Message, "reply_text", reply)
    await recovery._storage_error_handler(telegram_update(), SimpleNamespace(error=AutoReconnect("private database details")))
    reply.assert_awaited_once_with(recovery._STORAGE_MESSAGE)
    assert "private database details" not in caplog.text


@pytest.mark.asyncio
async def test_cancelled_detail_can_be_retried(monkeypatch):
    callback = registered_more_callback()
    update = make_update({"callsign": "THY123"})
    build = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(next60, "build_next60_docs", build)
    with pytest.raises(asyncio.CancelledError):
        await callback(update, None)
    build.side_effect = None
    build.return_value = []
    with pytest.raises(ApplicationHandlerStop):
        await callback(update, None)
    update.callback_query.message.reply_text.assert_awaited_once_with("Forecast changed. Run /next60 again.")


@pytest.mark.asyncio
async def test_callback_first_receipt_expiry_survives_repeated_delivery(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(interaction.time, "monotonic", lambda: clock[0])
    first, second = SimpleNamespace(id="first"), SimpleNamespace(id="second")
    interaction.mark_callback_received(first, "next60_more")
    clock[0] += 20
    interaction.mark_callback_received(second, "next60_more")
    interaction.mark_callback_received(first, "next60_more")
    clock[0] = 100 + interaction._RECEIVED_TTL_S + 1
    interaction._prune(clock[0])
    assert "first" not in interaction._received
    assert "second" in interaction._received
