"""Native Telegram Next 60 forecast for Plane Alerts v5.5.

Live entries come from the deterministic production approach state. Longer-range
history entries are derived on demand from bounded normal route history and are
shadow-only. No Prediction Lab Mongo collection is required.
"""
from __future__ import annotations

import html
import math
import re
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes

from app.database import get_db, users_col
from app.prediction_lab_files_v55 import append_evidence
from app.worker.optional_work import OptionalCache

MAX_ROWS = 30
MAX_HISTORY_ROUTES = 1500
HISTORY_DAYS = 3
MORE_PREFIX = "n60_more:"
_next60_evidence = OptionalCache(max_entries=2048, max_pending=32, concurrency=1, timeout=3.0)


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime): return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _minutes_from(now: datetime, value: datetime | None) -> int | None:
    if value is None: return None
    return max(0, int(round((value - now).total_seconds() / 60.0)))


def _bucket(horizon_minutes: float) -> str:
    if horizon_minutes <= 15: return "0–15 min"
    if horizon_minutes <= 30: return "15–30 min"
    return "30–60 min"


def _identity(doc: dict[str, Any]) -> str:
    return str(doc.get("callsign") or doc.get("aircraft_icao24") or "").strip().upper()


def _callback_token(doc: dict[str, Any]) -> str:
    return re.sub(r"[^A-Z0-9]", "", _identity(doc))[:24]


def _icao24(doc: dict[str, Any]) -> str:
    raw = str(doc.get("aircraft_icao24") or "").strip().lower()
    return raw if re.fullmatch(r"[0-9a-f]{6}", raw) else ""


def _adsb_url(doc: dict[str, Any]) -> str | None:
    icao = _icao24(doc)
    if icao: return f"https://adsb.lol/?icao={icao}"
    callsign = re.sub(r"[^A-Z0-9]", "", _identity(doc))[:24]
    return f"https://adsb.lol/?filterCallSign=%5E{callsign}%24" if callsign else None


def _aircraft_label(doc: dict[str, Any]) -> str:
    return str(doc.get("aircraft_type") or "").strip().upper()


def _distance_text(doc: dict[str, Any]) -> str:
    try: return f"{float(doc.get('predicted_closest_km')):.1f} km"
    except (TypeError, ValueError): return "? km"


def _eta_text(now: datetime, doc: dict[str, Any]) -> str:
    cpa = _minutes_from(now, _aware(doc.get("predicted_cpa_at")))
    if cpa is not None: return f"{cpa}m"
    start = _minutes_from(now, _aware(doc.get("window_start"))); end = _minutes_from(now, _aware(doc.get("window_end")))
    if start is not None and end is not None: return f"{start}–{end}m"
    return "?m"


def _compact_row(now: datetime, doc: dict[str, Any]) -> str:
    type_label = html.escape(_aircraft_label(doc)); callsign = html.escape(_identity(doc) or "Unknown")
    if type_label: return f"✈️ <b>{type_label}</b> · {callsign} · {_eta_text(now, doc)} · {_distance_text(doc)}"
    return f"✈️ <b>{callsign}</b> · {_eta_text(now, doc)} · {_distance_text(doc)}"


def _detail_text(now: datetime, doc: dict[str, Any]) -> str:
    callsign = html.escape(_identity(doc) or "Unknown"); aircraft_type = html.escape(str(doc.get("aircraft_type") or "Unknown").upper())
    confidence = html.escape(str(doc.get("confidence") or "Low")); icao = html.escape(_icao24(doc).upper() or "Not available")
    source = str(doc.get("source") or "history")
    if source == "live":
        source_text = "Live deterministic trajectory"; stage_text = html.escape(str(doc.get("stage") or "live").replace("_", " "))
    else:
        history_days = int(doc.get("historical_days") or 0); source_text = f"Prediction Lab history shadow · {history_days} day{'s' if history_days != 1 else ''}"; stage_text = "forecast only"
    return (f"✈️ <b>{callsign}</b>\nAircraft: <b>{aircraft_type}</b>\nETA to closest approach: <b>{_eta_text(now, doc)}</b>\nProjected closest: <b>{_distance_text(doc)}</b>\nConfidence: <b>{confidence}</b>\nState: {stage_text}\nSource: {source_text}\nICAO24: <code>{icao}</code>")


def render_next60_native(now: datetime, docs: list[dict[str, Any]]) -> tuple[str, InlineKeyboardMarkup | None]:
    buckets: dict[str, list[dict[str, Any]]] = {"0–15 min": [], "15–30 min": [], "30–60 min": []}; visible: list[dict[str, Any]] = []
    for doc in docs:
        cpa = _aware(doc.get("predicted_cpa_at"))
        if cpa is None: continue
        horizon = (cpa - now).total_seconds() / 60.0
        if horizon < 0 or horizon > 60: continue
        buckets[_bucket(horizon)].append(doc); visible.append(doc)
    lines = ["✈️ <b>Next 60 Minutes</b>", "<i>Type · flight · ETA · closest pass</i>"]
    if not visible:
        lines.append("\nNo next-hour candidates right now. Longer-range entries appear only when Plane Alerts has enough route history.")
        return "\n".join(lines), None
    for label in ("0–15 min", "15–30 min", "30–60 min"):
        rows = buckets[label]
        if not rows: continue
        rows.sort(key=lambda d: _aware(d.get("predicted_cpa_at")) or now); lines.append(f"\n<b>{label}</b>"); lines.extend(_compact_row(now, row) for row in rows)
    keyboard_rows: list[list[InlineKeyboardButton]] = []
    for doc in visible:
        token = _callback_token(doc)
        if not token: continue
        callsign = (_identity(doc) or "Plane")[:12]; row: list[InlineKeyboardButton] = []; adsb_url = _adsb_url(doc)
        if adsb_url: row.append(InlineKeyboardButton(f"ADSB · {callsign}", url=adsb_url))
        row.append(InlineKeyboardButton(f"More Info · {callsign}", callback_data=f"{MORE_PREFIX}{token}")); keyboard_rows.append(row)
    lines.append("\n<i>Live CPA is authoritative. 30–60 min history entries remain shadow forecasts.</i>")
    return "\n".join(lines), InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None


def render_next60(now: datetime, docs: list[dict[str, Any]]) -> str:
    buckets: dict[str, list[dict[str, Any]]] = {"0–15 min": [], "15–30 min": [], "30–60 min": []}
    for doc in docs:
        cpa = _aware(doc.get("predicted_cpa_at"))
        if cpa is None: continue
        horizon = (cpa - now).total_seconds() / 60.0
        if 0 <= horizon <= 60: buckets[_bucket(horizon)].append(doc)
    lines = ["✈️ <b>Plane Alerts · Next 60 Minutes</b>"]
    for label in ("0–15 min", "15–30 min", "30–60 min"):
        lines.append(f"\n<b>{label}</b>"); rows = sorted(buckets[label], key=lambda d: _aware(d.get("predicted_cpa_at")) or now)
        if not rows: lines.append("No current candidates."); continue
        for doc in rows:
            callsign = html.escape(_identity(doc) or "Unknown"); confidence = html.escape(str(doc.get("confidence") or "Low"))
            if doc.get("source") == "live": evidence = f"live trajectory · {html.escape(str(doc.get('stage') or 'live'))}"
            else:
                days = int(doc.get("historical_days") or 0); evidence = f"history shadow · {days} day{'s' if days != 1 else ''}"
            lines.append(f"• <b>{callsign}</b> · {_eta_text(now, doc)} · {_distance_text(doc)}\n  confidence {confidence} · {evidence}")
    lines.append("\n<i>Live trajectory/CPA is preferred when available. 30–60 min history entries are shadow estimates, not guaranteed alerts.</i>")
    return "\n".join(lines)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088; p1, p2 = math.radians(lat1), math.radians(lat2); dp = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * r * math.asin(min(1.0, math.sqrt(a)))


def _closest_point(points: list[dict[str, Any]], lat: float, lon: float) -> tuple[float, float] | None:
    best: tuple[float, float] | None = None
    for point in points:
        try: plat = float(point.get("lat", point.get("latitude"))); plon = float(point.get("lon", point.get("longitude"))); ts = float(point.get("t", point.get("timestamp", 0.0)) or 0.0)
        except (TypeError, ValueError): continue
        if ts <= 0: continue
        distance = _haversine_km(lat, lon, plat, plon)
        if best is None or distance < best[0]: best = (distance, ts)
    return best


def _median_time_of_day(values: list[float]) -> tuple[float, float]:
    normalized = list(values)
    if max(normalized) - min(normalized) > 12 * 3600: normalized = [value + 86400.0 if value < 12 * 3600 else value for value in normalized]
    median = float(statistics.median(normalized)); spread = max(abs(value - median) for value in normalized)
    return median % 86400.0, float(spread)


def _history_confidence(days: int, spread_s: float, horizon_s: float) -> str:
    if horizon_s > 1800: return "Low"
    if days >= 3 and spread_s <= 600: return "High"
    if days >= 2 and spread_s <= 1200: return "Medium"
    return "Low"


def _record_next60_evidence(user_id: int, doc: dict[str, Any], now: datetime) -> None:
    callsign = _identity(doc)
    if not callsign: return
    key = ("next60", int(user_id), callsign, now.date().isoformat())
    _next60_evidence.get(key, lambda: append_evidence({
        "kind": "next60_expectation", "captured_at": now, "user_id": int(user_id), "callsign": callsign,
        "aircraft_type": doc.get("aircraft_type"), "predicted_cpa_at": doc.get("predicted_cpa_at"), "window_start": doc.get("window_start"),
        "window_end": doc.get("window_end"), "prediction_horizon_s": doc.get("prediction_horizon_s"), "predicted_closest_km": doc.get("predicted_closest_km"),
        "historical_days": doc.get("historical_days"), "historical_time_spread_s": doc.get("historical_time_spread_s"), "confidence": doc.get("confidence"),
        "coverage_mode": "historical_flight_number_timing_shadow", "coverage_missing": False, "coverage_resolution": "observed_at_prediction_time",
        "outcome_resolution": "inconclusive_until_observed", "missing_coverage_policy": "inconclusive", "shadow_only": True,
        "status": "awaiting_observed_reality", "note": "30–60 minute entries are shadow-only; missing later ADS-B coverage remains inconclusive.",
    }), ttl=300)


async def _history_docs(user_id: int, now: datetime) -> list[dict[str, Any]]:
    """Derive bounded route-history shadow forecasts without Prediction Lab Mongo writes."""
    db = get_db(); loc = await db["locations"].find_one({"user_id": int(user_id)}, {"latitude": 1, "longitude": 1, "radius_km": 1, "_id": 0})
    if not loc: return []
    try: ulat = float(loc["latitude"]); ulon = float(loc["longitude"]); radius = float(loc.get("radius_km") or 15.0)
    except (KeyError, TypeError, ValueError): return []
    days = [(now.date() - timedelta(days=i)).isoformat() for i in range(1, HISTORY_DAYS + 1)]
    cursor = db["flight_route_samples"].find({"utc_date": {"$in": days}}, {"callsign": 1, "aircraft_type": 1, "utc_date": 1, "points": 1, "_id": 0}).limit(MAX_HISTORY_ROUTES)
    by_callsign: dict[str, list[dict[str, Any]]] = defaultdict(list)
    async for route in cursor:
        callsign = str(route.get("callsign") or "").strip().upper()
        if callsign and route.get("points"): by_callsign[callsign].append(route)
    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc); docs: list[dict[str, Any]] = []
    for callsign, routes in by_callsign.items():
        pass_times: list[float] = []; closest_distances: list[float] = []; used_days: set[str] = set(); aircraft_type = ""
        for route in routes:
            closest = _closest_point(list(route.get("points") or []), ulat, ulon)
            if closest is None: continue
            distance_km, ts = closest
            if distance_km > radius + max(2.0, radius * 0.15): continue
            day = str(route.get("utc_date") or "")
            if not day or day in used_days: continue
            used_days.add(day); dt = datetime.fromtimestamp(ts, timezone.utc)
            pass_times.append(dt.hour * 3600.0 + dt.minute * 60.0 + dt.second + dt.microsecond / 1_000_000.0); closest_distances.append(distance_km)
            aircraft_type = aircraft_type or str(route.get("aircraft_type") or "").strip().upper()
        if not pass_times: continue
        predicted_sod, spread_s = _median_time_of_day(pass_times); predicted_at = midnight + timedelta(seconds=predicted_sod); horizon_s = (predicted_at - now).total_seconds()
        if horizon_s < 0 or horizon_s > 3600: continue
        half_window = max(600.0, min(1800.0, spread_s + 300.0))
        doc = {"callsign": callsign, "aircraft_type": aircraft_type, "predicted_cpa_at": predicted_at, "window_start": predicted_at - timedelta(seconds=half_window),
               "window_end": predicted_at + timedelta(seconds=half_window), "prediction_horizon_s": round(horizon_s, 1), "predicted_closest_km": round(float(statistics.median(closest_distances)), 3),
               "historical_days": len(pass_times), "historical_time_spread_s": round(spread_s, 1), "confidence": _history_confidence(len(pass_times), spread_s, horizon_s), "source": "history"}
        docs.append(doc); _record_next60_evidence(user_id, doc, now)
    docs.sort(key=lambda d: _aware(d.get("predicted_cpa_at")) or now)
    return docs[:MAX_ROWS]


async def _live_docs(user_id: int, now: datetime) -> list[dict[str, Any]]:
    type_cache: dict[str, str] = {}
    try:
        from app.worker.monitor import get_provider_manager
        type_cache = dict(getattr(get_provider_manager(), "_type_cache", {}) or {})
    except Exception: type_cache = {}
    cursor = get_db()["approach_states"].find({"user_id": user_id, "active": True, "time_to_cpa_s": {"$gte": 0, "$lte": 3600}, "prediction_at": {"$gte": now - timedelta(seconds=20)}}, {"_id": 0, "aircraft_icao24": 1, "route_callsign": 1, "aircraft_type": 1, "projected_closest_km": 1, "time_to_cpa_s": 1, "prediction_at": 1, "confidence": 1, "stage": 1}).limit(MAX_ROWS)
    docs: list[dict[str, Any]] = []
    async for state in cursor:
        try: eta_s = float(state.get("time_to_cpa_s"))
        except (TypeError, ValueError): continue
        captured = _aware(state.get("prediction_at"))
        if captured is None or not 0 <= (now - captured).total_seconds() <= 20: continue
        cpa_at = captured + timedelta(seconds=eta_s); eta_s = (cpa_at - now).total_seconds()
        if eta_s < 0 or eta_s > 3600: continue
        icao = str(state.get("aircraft_icao24") or "").lower().strip(); callsign = str(state.get("route_callsign") or icao or "Unknown").upper(); aircraft_type = str(state.get("aircraft_type") or type_cache.get(icao) or "").upper().strip()
        docs.append({"callsign": callsign, "aircraft_icao24": icao, "aircraft_type": aircraft_type, "predicted_cpa_at": cpa_at, "prediction_horizon_s": eta_s, "predicted_closest_km": state.get("projected_closest_km"), "confidence": state.get("confidence") or "Low", "stage": state.get("stage") or "live", "source": "live"})
    return docs


async def build_next60_docs(user_id: int, now: datetime) -> list[dict[str, Any]]:
    history = await _history_docs(user_id, now); live = await _live_docs(user_id, now); merged: dict[str, dict[str, Any]] = {}; anonymous = 0
    for doc in history:
        key = _identity(doc)
        if not key: anonymous += 1; key = f"history-{anonymous}"
        merged[key] = doc
    for doc in live:
        key = _identity(doc)
        if not key: anonymous += 1; key = f"live-{anonymous}"
        merged[key] = doc
    docs = list(merged.values()); docs.sort(key=lambda d: _aware(d.get("predicted_cpa_at")) or now)
    return docs[:MAX_ROWS]


async def cmd_next60(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context; user = update.effective_user; message = update.message
    if user is None or message is None: return
    user_doc = await users_col().find_one({"user_id": user.id}, {"setup_complete": 1})
    if not user_doc or not user_doc.get("setup_complete"):
        await message.reply_text("Finish /start setup first so Plane Alerts knows which location to forecast for."); return
    now = datetime.now(timezone.utc); docs = await build_next60_docs(user.id, now); text, keyboard = render_next60_native(now, docs)
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard, disable_web_page_preview=True)


async def cb_next60_more(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context; query = update.callback_query; user = update.effective_user
    if query is None or user is None or not query.data: raise ApplicationHandlerStop
    token = query.data[len(MORE_PREFIX):]; now = datetime.now(timezone.utc); docs = await build_next60_docs(user.id, now); selected = next((doc for doc in docs if _callback_token(doc) == token), None)
    if selected is None:
        await query.answer("Forecast changed. Run /next60 again.", show_alert=True); raise ApplicationHandlerStop
    await query.answer()
    if query.message is not None:
        adsb_url = _adsb_url(selected); detail_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Open in ADSB", url=adsb_url)]]) if adsb_url else None
        await query.message.reply_text(_detail_text(now, selected), parse_mode=ParseMode.HTML, reply_markup=detail_keyboard, disable_web_page_preview=True)
    raise ApplicationHandlerStop


def register_next60_handlers(app: Application) -> None:
    app.add_handler(CallbackQueryHandler(cb_next60_more, pattern=rf"^{MORE_PREFIX}"), group=-1)
    app.add_handler(CommandHandler("next60", cmd_next60)); app.add_handler(CommandHandler("forecast", cmd_next60))
