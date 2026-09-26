"""Plane Alerts v4.6 Telegram interaction latency guards and telemetry."""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from collections import OrderedDict, defaultdict, deque
from copy import deepcopy
from statistics import median
from typing import Any, Awaitable, Callable

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ApplicationHandlerStop

logger = logging.getLogger(__name__)

_METRIC_MAX = 2048
_RECEIVED_TTL_S = 90.0
_RECEIVED_MAX = 4096
_DEDUPE_TTL_S = 120.0
_DEDUPE_MAX = 4096
_STATE_TTL_S = 300.0
_STATE_MAX = 1024

_received: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
_acked: set[str] = set()
_seen: "OrderedDict[str, float]" = OrderedDict()
_metrics: dict[str, dict[str, deque[float]]] = defaultdict(
    lambda: {
        "ack_ms": deque(maxlen=_METRIC_MAX),
        "handler_ms": deque(maxlen=_METRIC_MAX),
        "message_ms": deque(maxlen=_METRIC_MAX),
    }
)
_state_cache: "OrderedDict[int, tuple[float, str, dict[str, Any]]]" = OrderedDict()
_installed = False
_original_answer = CallbackQuery.answer


def _prune(now: float) -> None:
    cutoff = now - _RECEIVED_TTL_S
    for key, (seen, _) in list(_received.items()):
        if seen >= cutoff:
            break
        _received.pop(key, None)
        _acked.discard(key)
    while len(_received) > _RECEIVED_MAX:
        key, _ = _received.popitem(last=False)
        _acked.discard(key)

    dedupe_cutoff = now - _DEDUPE_TTL_S
    for key, seen in list(_seen.items()):
        if seen >= dedupe_cutoff:
            break
        _seen.pop(key, None)
    while len(_seen) > _DEDUPE_MAX:
        _seen.popitem(last=False)

    state_cutoff = now - _STATE_TTL_S
    for key, (seen, _, _) in list(_state_cache.items()):
        if seen >= state_cutoff:
            break
        _state_cache.pop(key, None)
    while len(_state_cache) > _STATE_MAX:
        _state_cache.popitem(last=False)


def mark_callback_received(query: Any, handler: str) -> float:
    now = time.monotonic()
    key = str(getattr(query, "id", "") or "")
    if not key:
        return now
    existing = _received.get(key)
    if existing is None:
        _received[key] = (now, handler)
    else:
        now = existing[0]
    # Preserve insertion order: timestamps are first receipt times, including
    # duplicate delivery. Moving an old entry would break TTL pruning order.
    _prune(time.monotonic())
    return now


async def _answer_with_timing(self: CallbackQuery, *args: Any, **kwargs: Any) -> Any:
    key = str(getattr(self, "id", "") or "")
    result = await _original_answer(self, *args, **kwargs)
    entry = _received.get(key)
    # Only tracked successful answers belong here. Uninstrumented callbacks
    # have no bounded/expiring _received entry to retire their acknowledgement.
    if entry is not None and key not in _acked:
        started, handler = entry
        _metrics[handler]["ack_ms"].append(max(0.0, (time.monotonic() - started) * 1000.0))
        _acked.add(key)
    return result


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def latency_snapshot(handler: str | None = None) -> dict[str, Any]:
    names = [handler] if handler else sorted(_metrics)
    result: dict[str, Any] = {}
    for name in names:
        if name not in _metrics:
            continue
        result[name] = {}
        for metric, series in _metrics[name].items():
            values = list(series)
            result[name][metric] = {
                "count": len(values),
                "p50": _percentile(values, 0.50),
                "p95": _percentile(values, 0.95),
                "p99": _percentile(values, 0.99),
                "worst": max(values) if values else None,
            }
    return result


def _record_handler(handler: str, started: float, *, message_complete: bool = False) -> None:
    elapsed = max(0.0, (time.monotonic() - started) * 1000.0)
    _metrics[handler]["handler_ms"].append(elapsed)
    if message_complete:
        _metrics[handler]["message_ms"].append(elapsed)
    count = len(_metrics[handler]["handler_ms"])
    if count and count % 50 == 0:
        logger.info(
            "telegram_callback_latency %s",
            json.dumps({"handler": handler, **latency_snapshot(handler).get(handler, {})}, sort_keys=True, separators=(",", ":")),
        )


def _timed_callback(handler: str, fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(fn)
    async def wrapped(update: Any, context: Any) -> Any:
        query = getattr(update, "callback_query", None)
        started = mark_callback_received(query, handler) if query is not None else time.monotonic()
        try:
            return await fn(update, context)
        finally:
            _record_handler(handler, started, message_complete=True)
    return wrapped


def _is_duplicate(query: Any) -> bool:
    key = str(getattr(query, "id", "") or "")
    if not key:
        return False
    now = time.monotonic()
    _prune(now)
    if key in _seen:
        return True
    _seen[key] = now
    _seen.move_to_end(key)
    return False


async def next60_more_v46(update: Any, context: Any) -> None:
    """ACK first, then do forecast DB work; duplicate updates are idempotent."""
    del context
    from app.bot import next60

    query = update.callback_query
    user = update.effective_user
    if query is None or user is None or not query.data:
        raise ApplicationHandlerStop

    started = mark_callback_received(query, "next60_more")
    if _is_duplicate(query):
        await query.answer("Already handled.")
        _record_handler("next60_more", started)
        raise ApplicationHandlerStop

    try:
        # This must remain the first awaited external operation.
        await query.answer()

        token = query.data[len(next60.MORE_PREFIX):]
        now_dt = next60.datetime.now(next60.timezone.utc)
        docs = await next60.build_next60_docs(user.id, now_dt)
        selected = next((doc for doc in docs if next60._callback_token(doc) == token), None)

        if query.message is not None:
            if selected is None:
                await query.message.reply_text("Forecast changed. Run /next60 again.")
            else:
                tracker_url = next60._tracker_url(selected)
                detail_keyboard = (
                    InlineKeyboardMarkup([[InlineKeyboardButton("Open in Flightradar24", url=tracker_url)]])
                    if tracker_url
                    else None
                )
                await query.message.reply_text(
                    next60._detail_text(now_dt, selected),
                    parse_mode=ParseMode.HTML,
                    reply_markup=detail_keyboard,
                    disable_web_page_preview=True,
                )
    except (Exception, asyncio.CancelledError):
        # A failed/cancelled attempt is not a completed action. Allow Telegram
        # redelivery to retry, while successful deliveries stay deduplicated.
        _seen.pop(str(getattr(query, "id", "") or ""), None)
        raise
    _record_handler("next60_more", started, message_complete=True)
    raise ApplicationHandlerStop


async def _cached_state_from(original: Callable[[int], Awaitable[tuple[str, dict]]], user_id: int) -> tuple[str, dict]:
    now = time.monotonic()
    _prune(now)
    cached = _state_cache.get(int(user_id))
    if cached and now - cached[0] <= _STATE_TTL_S:
        _state_cache.move_to_end(int(user_id))
        return cached[1], deepcopy(cached[2])
    state, temp = await original(int(user_id))
    _state_cache[int(user_id)] = (time.monotonic(), str(state), deepcopy(temp))
    _state_cache.move_to_end(int(user_id))
    return str(state), deepcopy(temp)


def _cache_state(user_id: int, state: str, temp: dict[str, Any]) -> None:
    _state_cache[int(user_id)] = (time.monotonic(), str(state), deepcopy(temp))
    _state_cache.move_to_end(int(user_id))
    _prune(time.monotonic())


def clear_latency_state_for_tests() -> None:
    _received.clear()
    _acked.clear()
    _seen.clear()
    _metrics.clear()
    _state_cache.clear()


def install_interaction_v46() -> None:
    global _installed, _original_answer
    if _installed:
        return

    from app.bot import handlers, next60, profile_handlers, profile_legacy
    from app.photography import telegram as photo_telegram

    _original_answer = CallbackQuery.answer
    CallbackQuery.answer = _answer_with_timing  # type: ignore[method-assign]

    # /next60 had a verified pre-ACK DB rebuild. Replace only that callback.
    next60.cb_next60_more = next60_more_v46

    # Cache persisted profile session state. Writes still go to Mongo before the
    # cache changes, so a restart or failed write cannot invent newer state.
    original_raw_state = profile_handlers._raw_state
    original_set_state = profile_handlers._set_state
    original_clear_state = profile_handlers._clear_state
    original_legacy_state = profile_legacy._state

    async def raw_state_cached(user_id: int) -> tuple[str, dict]:
        return await _cached_state_from(original_raw_state, user_id)

    async def legacy_state_cached(user_id: int) -> tuple[str, dict]:
        return await _cached_state_from(original_legacy_state, user_id)

    async def set_state_cached(user_id: int, state: str, temp: dict | None = None, **kwargs: Any) -> None:
        payload = dict(temp if temp is not None else (kwargs.get("temp_data") or {}))
        await original_set_state(user_id, state, payload)
        _cache_state(user_id, state, payload)

    async def clear_state_cached(user_id: int) -> None:
        await original_clear_state(user_id)
        _cache_state(user_id, "idle", {})

    profile_handlers._raw_state = raw_state_cached
    profile_handlers._set_state = set_state_cached
    profile_handlers._clear_state = clear_state_cached
    profile_legacy._state = legacy_state_cached
    profile_legacy._set_state = set_state_cached
    profile_legacy._clear_state = clear_state_cached

    # Exact ACK timing is captured by CallbackQuery.answer above. These wrappers
    # add end-to-end handler/message-completion timing without changing ordering.
    profile_handlers.profile_callback = _timed_callback("profiles", profile_handlers.profile_callback)
    profile_legacy.stale_profile_callback_guard = _timed_callback(
        "profiles_guard", profile_legacy.stale_profile_callback_guard
    )
    handlers.cb_handler = _timed_callback("settings_general", handlers.cb_handler)
    photo_telegram.handle_photo_callback = _timed_callback(
        "photography", photo_telegram.handle_photo_callback
    )

    _installed = True
