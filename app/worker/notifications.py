"""Telegram notifications for Plane Alerts spotting alerts."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from html import escape

from telegram import Bot, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError

from app.aircraft.models import NormalizedAircraft
from app.config import settings
from app.database import get_db, locations_col, users_col
from app.photography.keyboards import notification_actions_keyboard
from app.worker.notification_telemetry import enqueue_auxiliary, enqueue_notification_event

logger = logging.getLogger(__name__)
_send_semaphore = asyncio.Semaphore(20)
_MIN_SEND_INTERVAL = 0.05
_bot_instance: Bot | None = None
_LOCATION_CACHE_TTL_S = 600.0
_LOCATION_CACHE_MAX = 2048
_location_cache: OrderedDict[int, tuple[float, float, float]] = OrderedDict()


def _safe(v: str) -> str:
    return escape(v or "", quote=True)


def _safe_provider_text(value: str) -> str:
    """Backward-compatible provider escaping helper retained for callers/tests."""
    return _safe(value)


def _get_bot() -> Bot:
    global _bot_instance
    if _bot_instance is None or _bot_instance.token != settings.telegram_bot_token:
        _bot_instance = Bot(token=settings.telegram_bot_token)
    return _bot_instance


def _clock(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = max(0, int(round(seconds)))
    return f"{s // 60:02d}:{s % 60:02d}"


def _identity(ac: NormalizedAircraft) -> str:
    callsign = _safe(ac.callsign)
    aircraft_type = _safe(ac.aircraft_type or ac.display_type or "Aircraft")
    if callsign:
        return f"<b>{callsign}</b> · <code>{aircraft_type}</code>"
    return f"<b>{aircraft_type}</b>"


def _flight_status_line(ac: NormalizedAircraft, pred) -> str:
    parts = [f"<b>{escape(pred.state)}</b>", escape(pred.confidence)]
    if ac.altitude is not None:
        parts.append(f"{int(round(ac.altitude * 3.28084)):,} ft")
    if ac.ground_speed is not None:
        parts.append(f"{int(round(ac.ground_speed))} kt")
    return " · ".join(parts)


def _camera_line(camera) -> str | None:
    if not camera:
        return None
    parts = [camera.shutter_speed, camera.aperture, escape(camera.iso)]
    focal = escape(camera.focal_length) if getattr(camera, "focal_length", None) else None
    if focal:
        parts.append(focal)
    elif getattr(camera, "focal_range_mm", None):
        lo, hi = camera.focal_range_mm
        parts.append(f"{lo}–{hi} mm")
    return "📷 " + " · ".join(parts)


def _conditions_line(environment) -> str | None:
    if not environment:
        return None
    solar = environment.get("solar")
    atm = environment.get("atmosphere")
    con = environment.get("contrail")
    parts: list[str] = []
    if solar:
        parts.append(escape(solar.lighting_relationship))
    if atm:
        parts.append(f"Haze {escape(atm.heat_haze)}")
    if con:
        parts.append(f"Contrail {escape(con.formation)}")
    elif environment.get("upper_error"):
        parts.append("Contrail uncertain")
    return "☀️ " + " · ".join(parts) if parts else None


def _approach_text(
    ac,
    pred,
    stage,
    notification_id="",
    camera=None,
    environment=None,
    previous_cpa_km=None,
    observed_closest_km=None,
    prediction_changed=False,
) -> str:
    """Build a compact glanceable Telegram alert."""
    title = {
        "prepare": "📡 <b>NEXT SHOT</b>",
        "camera_ready": "📷 <b>CAMERA READY</b>",
        "photo_now": "🔥 <b>PHOTO NOW</b>",
        "passed": "✅ <b>AIRCRAFT PASSED</b>",
        "cancelled": "↪️ <b>TRAJECTORY CHANGED</b>",
    }.get(stage, "✈️ <b>SPOTTING</b>")

    lines = [title, _identity(ac)]

    if stage == "cancelled":
        cpa = f"{pred.projected_closest_km:.1f} km"
        if previous_cpa_km is not None:
            lines.append(f"Alert cancelled · CPA <b>{cpa}</b> (was {float(previous_cpa_km):.1f} km)")
        else:
            lines.append(f"Alert cancelled · CPA <b>{cpa}</b>")
        return "\n".join(lines)

    if stage == "passed":
        closest = observed_closest_km if observed_closest_km is not None else pred.projected_closest_km
        passed_parts = [f"Closest <b>{float(closest):.1f} km</b>"]
        if ac.altitude is not None:
            passed_parts.append(f"{int(round(ac.altitude * 3.28084)):,} ft")
        lines.append(" · ".join(passed_parts))
        return "\n".join(lines)

    countdown = camera.best_window_start_s if camera and camera.best_window_start_s is not None else pred.time_to_cpa_s
    lines.append(
        f"⏱ <b>{_clock(countdown)}</b> · CPA <b>{pred.projected_closest_km:.1f} km</b> · {pred.current_distance_km:.1f} km now"
    )
    lines.append(_flight_status_line(ac, pred))

    camera_text = _camera_line(camera)
    if camera_text:
        lines.append(camera_text)

    conditions_text = _conditions_line(environment)
    if conditions_text:
        lines.append(conditions_text)

    if prediction_changed:
        lines.append("🔄 Trajectory updated")

    if environment:
        solar_crossing = environment.get("sun_crossing")
        moon_crossing = environment.get("moon_crossing")
        if solar_crossing and solar_crossing.candidate:
            seconds = int(solar_crossing.time_to_min_s or 0)
            lines.append(f"☀️ Solar crossing candidate in ~{seconds}s")
            if solar_crossing.safety_warning:
                lines.append(f"⚠️ {escape(solar_crossing.safety_warning)}")
        if moon_crossing and moon_crossing.candidate:
            seconds = int(moon_crossing.time_to_min_s or 0)
            lines.append(f"🌙 Moon crossing candidate in ~{seconds}s")

    return "\n".join(lines)


def _basic_alert_text(
    aircraft: NormalizedAircraft,
    distance_km: float,
    eta_seconds: float | None,
    notification_id: str = "",
) -> str:
    title = "🚀 <b>EARLY WARNING</b>" if eta_seconds is not None and eta_seconds > 0 else "✈️ <b>AIRCRAFT ALERT</b>"
    lines = [title, _identity(aircraft)]
    details = [f"{distance_km:.1f} km away"]
    if eta_seconds is not None and eta_seconds > 0:
        details.insert(0, f"~{_clock(eta_seconds)}")
    if aircraft.altitude is not None:
        details.append(f"{int(round(aircraft.altitude * 3.28084)):,} ft")
    if aircraft.ground_speed is not None:
        details.append(f"{int(round(aircraft.ground_speed))} kt")
    lines.append(" · ".join(details))
    return "\n".join(lines)


async def _observer_coordinates(user_id: int) -> tuple[float, float] | None:
    now = time.monotonic()
    cached = _location_cache.get(int(user_id))
    if cached and cached[0] > now:
        _location_cache.move_to_end(int(user_id))
        return cached[1], cached[2]
    if cached:
        _location_cache.pop(int(user_id), None)

    doc = await locations_col().find_one({"user_id": user_id}, {"latitude": 1, "longitude": 1})
    if not doc or doc.get("latitude") is None or doc.get("longitude") is None:
        return None
    value = (now + _LOCATION_CACHE_TTL_S, float(doc["latitude"]), float(doc["longitude"]))
    _location_cache[int(user_id)] = value
    _location_cache.move_to_end(int(user_id))
    while len(_location_cache) > _LOCATION_CACHE_MAX:
        _location_cache.popitem(last=False)
    return value[1], value[2]


async def _record_photo_snapshot(
    user_id: int,
    aircraft: NormalizedAircraft,
    distance_km: float,
    notification_id: str,
    eta_seconds: float | None,
) -> None:
    if not notification_id:
        return
    now = datetime.now(timezone.utc)
    observer = await _observer_coordinates(user_id)
    values = {
        "user_id": user_id,
        "aircraft_icao24": aircraft.icao24 or "",
        "aircraft_type": aircraft.aircraft_type or aircraft.display_type or "",
        "callsign": aircraft.callsign or "",
        "distance_km": float(distance_km),
        "altitude_m": aircraft.altitude,
        "speed_ms": aircraft.velocity,
        "heading_deg": aircraft.heading,
        "vertical_rate_mps": getattr(aircraft, "vertical_rate_mps", None),
        "position_age_s": getattr(aircraft, "position_age_s", None),
        "latitude": aircraft.latitude,
        "longitude": aircraft.longitude,
        "eta_seconds": eta_seconds,
        "captured_at": now,
        "expires_at": now + timedelta(hours=6),
    }
    if observer:
        values["observer_latitude"] = observer[0]
        values["observer_longitude"] = observer[1]
    await get_db()["photo_alert_snapshots"].update_one(
        {"_id": notification_id, "user_id": user_id},
        {"$set": values},
        upsert=True,
    )


def _notification_payload(
    *,
    user_id: int,
    aircraft: NormalizedAircraft,
    prediction,
    stage: str,
    notification_id: str,
    input_message_id: int | None,
    output_message_id: int | None,
    delivered: bool,
    delivery_detail: str,
    observed_closest_km: float | None,
) -> dict:
    return {
        "notification_id": notification_id,
        "user_id": user_id,
        "aircraft_icao24": aircraft.icao24,
        "aircraft_type": aircraft.aircraft_type,
        "distance_km": prediction.current_distance_km,
        "projected_closest_km": prediction.projected_closest_km,
        "observed_closest_km": observed_closest_km,
        "trajectory_state": prediction.state,
        "prediction_confidence": prediction.confidence,
        "stage": stage,
        "input_message_id": input_message_id,
        "output_message_id": output_message_id,
        "delivered": delivered,
        "delivery_detail": delivery_detail,
    }


async def send_or_update_approach(
    user_id: int,
    aircraft,
    prediction,
    stage: str,
    notification_id: str,
    message_id: int | None = None,
    *,
    camera=None,
    environment=None,
    previous_cpa_km=None,
    observed_closest_km=None,
    prediction_changed=False,
) -> int | None:
    text = _approach_text(
        aircraft,
        prediction,
        stage,
        notification_id,
        camera,
        environment,
        previous_cpa_km,
        observed_closest_km,
        prediction_changed,
    )
    markup = (
        notification_actions_keyboard(notification_id, getattr(aircraft, "icao24", None))
        if notification_id
        else None
    )

    delivered = False
    result_message_id: int | None = None
    delivery_detail = ""

    async with _send_semaphore:
        try:
            bot = _get_bot()
            if message_id:
                try:
                    await bot.edit_message_text(
                        chat_id=user_id,
                        message_id=int(message_id),
                        text=text,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                        reply_markup=markup,
                    )
                    delivered = True
                    result_message_id = int(message_id)
                    delivery_detail = "edited"
                except BadRequest as exc:
                    if "message is not modified" in str(exc).lower():
                        delivered = True
                        result_message_id = int(message_id)
                        delivery_detail = "not_modified"
                    else:
                        logger.info(
                            "live_edit_failed user=%s message=%s error=%s",
                            user_id,
                            message_id,
                            type(exc).__name__,
                        )
                        # A message created while the old satellite-map feature was
                        # enabled is media-only and cannot be converted back to text.
                        # Replace it once, then remove the old map message. This is
                        # still an update to the same logical alert, never a new alert.
                        sent = await bot.send_message(
                            chat_id=user_id,
                            text=text,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                            reply_markup=markup,
                        )
                        try:
                            await bot.delete_message(chat_id=user_id, message_id=int(message_id))
                        except Exception:
                            pass
                        await asyncio.sleep(_MIN_SEND_INTERVAL)
                        delivered = True
                        result_message_id = int(sent.message_id)
                        delivery_detail = "replaced_after_edit_failure"
            else:
                sent = await bot.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=markup,
                )
                await asyncio.sleep(_MIN_SEND_INTERVAL)
                delivered = True
                result_message_id = int(sent.message_id)
                delivery_detail = "sent"
        except Forbidden:
            delivery_detail = "forbidden"
            await users_col().update_one({"user_id": user_id}, {"$set": {"setup_complete": False}})
        except TelegramError as exc:
            delivery_detail = f"telegram_error:{type(exc).__name__}"
            logger.error("approach_message_failed user=%s error=%s", user_id, type(exc).__name__)
        except Exception as exc:
            delivery_detail = f"unexpected:{type(exc).__name__}"
            logger.exception("approach_message_unexpected user=%s", user_id)

    if notification_id:
        async def _photo_snapshot() -> None:
            await _record_photo_snapshot(
                user_id,
                aircraft,
                prediction.current_distance_km,
                notification_id,
                prediction.time_to_cpa_s,
            )

        enqueue_notification_event(
            _notification_payload(
                user_id=user_id,
                aircraft=aircraft,
                prediction=prediction,
                stage=stage,
                notification_id=notification_id,
                input_message_id=message_id,
                output_message_id=result_message_id,
                delivered=delivered,
                delivery_detail=delivery_detail,
                observed_closest_km=observed_closest_km,
            ),
            auxiliary=_photo_snapshot if delivered else None,
        )

    return result_message_id


async def send_aircraft_notification(
    user_id: int,
    aircraft: NormalizedAircraft,
    distance_km: float,
    notification_id: str = "",
    eta_seconds: float | None = None,
) -> bool:
    msg = _basic_alert_text(aircraft, distance_km, eta_seconds, notification_id)
    sent = await _send_message(
        user_id,
        msg,
        notification_actions_keyboard(notification_id, aircraft.icao24) if notification_id else None,
    )
    if sent and notification_id:
        async def _photo_snapshot() -> None:
            await _record_photo_snapshot(user_id, aircraft, distance_km, notification_id, eta_seconds)

        enqueue_auxiliary(_photo_snapshot, label="basic_photo_snapshot")
    return sent


async def _send_message(
    user_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    async with _send_semaphore:
        try:
            await _get_bot().send_message(
                chat_id=user_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
            await asyncio.sleep(_MIN_SEND_INTERVAL)
            return True
        except Forbidden:
            await users_col().update_one({"user_id": user_id}, {"$set": {"setup_complete": False}})
            return False
        except Exception:
            logger.exception("notification_failed user=%s", user_id)
            return False


async def send_admin_alert(text: str) -> None:
    if not settings.admin_telegram_id:
        return
    try:
        await _get_bot().send_message(
            chat_id=settings.admin_telegram_id,
            text=f"🔔 <b>Admin Alert</b>\n\n{escape(text, quote=True)}",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("Failed to send admin alert")