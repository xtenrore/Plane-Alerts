"""Compatibility routing from legacy setup commands into v4.3 profiles."""
from __future__ import annotations

from copy import deepcopy
import html
import re

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.aircraft.registry import get_aircraft_type, search_aircraft
from app.alert_profiles import (
    blank_config_from,
    ensure_default_profile,
    profile_summary,
    save_profile,
)
from app.bot.profile_handlers import (
    _button,
    _clear_state,
    _draft_selection,
    _render_aircraft_menu,
    _render_profiles,
    _render_rule,
    _set_state,
)
from app.database import users_col, user_state_col
from app.version import VERSION
from app.worker.geo import compute_geohash


def _esc(value: object) -> str:
    return html.escape(str(value or ""))


async def _state(user_id: int) -> tuple[str, dict]:
    doc = await user_state_col().find_one({"user_id": int(user_id)})
    return (str((doc or {}).get("current_state") or "idle"), dict((doc or {}).get("temp_data") or {}))


async def _begin_profile_setup(update: Update, user_id: int, *, edit_message: bool = False) -> None:
    active = await ensure_default_profile(user_id)
    temp = {
        "profile_flow": "setup",
        "editing_profile_id": active["profile_id"],
        "draft_name": active.get("name") or "Default",
        "profile_draft": blank_config_from(active.get("config") or {}),
        "profile_return": "edit",
    }
    await _set_state(user_id, "profile:location", temp)
    text = (
        "<b>Set Up Plane Alerts</b>\n\n"
        "Send the Telegram location you want this profile to monitor."
    )
    keyboard = InlineKeyboardMarkup([[_button("Cancel", "pf:home")]])
    query = update.callback_query
    if edit_message and query and query.message:
        await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    elif update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    elif query and query.message:
        await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def cmd_setup_profiled(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    user = update.effective_user
    if not user:
        return
    await _begin_profile_setup(update, user.id)
    raise ApplicationHandlerStop


async def cmd_location_profiled(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    user = update.effective_user
    if not user:
        return
    active = await ensure_default_profile(user.id)
    await _set_state(
        user.id,
        "profile:quick_location",
        {
            "editing_profile_id": active["profile_id"],
            "profile_draft": deepcopy(active.get("config") or {}),
        },
    )
    if update.message:
        await update.message.reply_text(
            "<b>Update Location</b>\n\nSend the Telegram location for the active profile.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[_button("Cancel", "pf:home")]]),
        )
    raise ApplicationHandlerStop


async def cmd_status_profiled(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the configuration that is actually authoritative for alerts."""
    del context
    user = update.effective_user
    message = update.message
    if not user or not message:
        return

    user_doc = await users_col().find_one({"user_id": user.id}, {"setup_complete": 1})
    if not user_doc or not user_doc.get("setup_complete"):
        await message.reply_text(
            "<b>Plane Alerts Configuration</b>\n\nSetup is not complete. Use /start to begin.",
            parse_mode=ParseMode.HTML,
        )
        raise ApplicationHandlerStop

    active = await ensure_default_profile(user.id)
    await message.reply_text(
        "<b>Plane Alerts Configuration</b>\n\n"
        f"Active profile: <b>{_esc(active.get('name') or 'Default')}</b>\n"
        f"{_esc(profile_summary(active))}\n\n"
        "Monitoring active.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[_button("Manage Profiles", "pf:home")]]),
    )
    raise ApplicationHandlerStop


async def cmd_help_profiled(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Keep the command reference aligned with the current Plane Alerts runtime."""
    del context
    message = update.message
    if not message:
        return
    await message.reply_text(
        f"<b>Plane Alerts v{VERSION}</b>\n\n"
        "/profiles — create, switch and manage alert profiles\n"
        "/preferences — aircraft selection and advanced filters\n"
        "/location — update the active profile location\n"
        "/status — show the active alert configuration\n"
        "/next60 — aircraft expected in the next 60 minutes\n"
        "/forecast — alias for /next60\n"
        "/camera — set camera body\n"
        "/lens — set lens\n"
        "/photo — current shooting guidance\n"
        "/conditions — weather, sun and atmospheric conditions\n"
        "/spotting — spotting tools\n"
        "/setup — reconfigure the active profile\n"
        "/cancel — cancel the current conversation flow",
        parse_mode=ParseMode.HTML,
    )
    raise ApplicationHandlerStop


async def accept_terms_profiled(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Keep the existing welcome/disclaimer but use the profiled setup after it."""
    del context
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    await query.answer()
    await users_col().update_one(
        {"user_id": user.id},
        {"$set": {"terms_accepted": True}},
        upsert=True,
    )
    await _begin_profile_setup(update, user.id, edit_message=True)
    raise ApplicationHandlerStop


async def stale_profile_callback_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Expire nested profile menus whose persisted edit draft no longer exists."""
    del context
    query = update.callback_query
    user = update.effective_user
    if not query or not query.data or not user:
        return

    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action in {"home", "new", "o", "a", "e", "r", "d", "x", "xd"}:
        return

    _current, temp = await _state(user.id)
    if isinstance(temp.get("profile_draft"), dict):
        return

    await query.answer("This profile menu has expired.", show_alert=True)
    await _clear_state(user.id)
    await _render_profiles(update, user.id)
    raise ApplicationHandlerStop


async def unknown_aircraft_search_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Let valid ICAO type codes work even before local metadata catches up."""
    del context
    user = update.effective_user
    message = update.message
    if not user or not message or message.text is None:
        return
    state, temp = await _state(user.id)
    if state != "profile:search":
        return

    raw = message.text.strip()
    if search_aircraft(raw, limit=1):
        return
    code = raw.upper()
    if get_aircraft_type(code) is not None or not re.fullmatch(r"(?=.*[A-Z])[A-Z0-9]{2,4}", code):
        return

    purpose = str(temp.get("profile_search_purpose") or "select")
    _draft, prefs, selection = _draft_selection(temp)
    selected = set(selection["selected_types"])
    excluded = set(selection["excluded_types"])
    selected.add(code)
    excluded.discard(code)
    selection["selected_types"] = sorted(selected)
    selection["excluded_types"] = sorted(excluded)
    prefs["aircraft_filter"] = selection
    await _set_state(user.id, "profile:aircraft", temp)

    if purpose == "rule":
        await _render_rule(update, user.id, "aircraft", code)
    else:
        await message.reply_text(
            f"Added <b>{_esc(code)}</b> as a custom ICAO aircraft type. "
            "It will be treated as Other until catalogue metadata identifies its category.",
            parse_mode=ParseMode.HTML,
        )
        await _render_aircraft_menu(update, user.id)
    raise ApplicationHandlerStop


async def quick_location_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    user = update.effective_user
    message = update.message
    if not user or not message or not message.location:
        return
    state, temp = await _state(user.id)
    if state != "profile:quick_location":
        return
    profile_id = str(temp.get("editing_profile_id") or "")
    config = deepcopy(temp.get("profile_draft") or {})
    loc = dict(config.get("location") or {})
    lat = float(message.location.latitude)
    lon = float(message.location.longitude)
    loc.update({"latitude": lat, "longitude": lon, "geohash": compute_geohash(lat, lon)})
    config["location"] = loc
    profile = await save_profile(user.id, profile_id, config=config)
    await _clear_state(user.id)
    await message.reply_text(
        f"Location updated for <b>{_esc(profile.get('name') or 'active profile')}</b>.",
        parse_mode=ParseMode.HTML,
    )
    raise ApplicationHandlerStop


# Explicit runtime composition: app.main imports this module only after the
# profile/Next60 modules exist. Install here so a fresh interpreter import of
# app.main cannot depend on incidental app.worker import order.
from app.profile_safety_v542 import install_profile_safety_v542
from app.bot.interaction_v46 import install_interaction_v46

install_profile_safety_v542()
install_interaction_v46()


def register_profile_legacy_handlers(app: Application) -> None:
    group = -31
    app.add_handler(CommandHandler("setup", cmd_setup_profiled), group=group)
    app.add_handler(CommandHandler("location", cmd_location_profiled), group=group)
    app.add_handler(CommandHandler("status", cmd_status_profiled), group=group)
    app.add_handler(CommandHandler("help", cmd_help_profiled), group=group)
    app.add_handler(CallbackQueryHandler(accept_terms_profiled, pattern=r"^terms:accept$"), group=group)
    app.add_handler(CallbackQueryHandler(stale_profile_callback_guard, pattern=r"^pf:"), group=group)
    app.add_handler(MessageHandler(filters.LOCATION, quick_location_message), group=group)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_aircraft_search_fallback), group=group)
