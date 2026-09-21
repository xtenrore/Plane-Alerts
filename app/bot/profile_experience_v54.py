"""Plane Alerts v5.4 guided profile UX and recovery layer.

The proven v4.3 profile engine remains authoritative. This module registers a
higher-priority presentation layer for profile home/detail, presets and setup
recovery, then delegates detailed aircraft/rule editing to the existing engine.
It is registered explicitly by ``app.main`` so importing bot modules cannot
change the prediction-worker composition as a side effect.
"""
from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.alert_profiles import (
    create_profile,
    ensure_default_profile,
    get_active_profile,
    get_profile,
    list_profiles,
    profile_summary,
    save_profile,
)
from app.bot import profile_handlers as legacy
from app.database import users_col
from app.profile_presets_v54 import PRESETS, apply_preset, get_preset, preset_summary
from app.version import VERSION
from app.worker.geo import compute_geohash


def _b(text: str, data: str):
    return legacy._button(text, data)


def brand_next60_html_v54(html: str) -> str:
    """Return the existing Next 60 Mini App with current product terminology."""
    return (
        str(html)
        .replace("<title>Plane? · Next 60</title>", "<title>Plane Alerts · Next 60</title>")
        .replace("PLANE? · FORECAST", "PLANE ALERTS · FORECAST")
        .replace("Plane? Telegram bot", "Plane Alerts Telegram bot")
    )


async def _home_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    active = await get_active_profile(user_id)
    profiles = await list_profiles(user_id)
    lines = ["<b>Alert Profiles</b>", "", "Switch setups without rebuilding your alerts.", ""]
    rows = []
    for profile in profiles:
        is_active = bool(active and profile.get("profile_id") == active.get("profile_id"))
        marker = "✓ " if is_active else ""
        lines.append(f"<b>{marker}{legacy._esc(profile.get('name'))}</b>")
        lines.append(legacy._esc(profile_summary(profile)))
        lines.append("")
        rows.append([_b(f"{marker}{profile.get('name')}", f"ux54:o:{profile['profile_id']}")])
    rows.extend([
        [_b("New Profile", "ux54:new"), _b("Status", "ux54:status")],
        [_b("Close", "ux54:close")],
    ])
    return "\n".join(lines).rstrip(), InlineKeyboardMarkup(rows)


async def _render_home(update: Update, user_id: int) -> None:
    text, keyboard = await _home_view(user_id)
    await legacy._show(update, text, keyboard)


async def _render_detail(update: Update, user_id: int, profile_id: str, note: str = "") -> None:
    profile = await get_profile(user_id, profile_id)
    if not profile:
        await _recover(update, user_id, "That profile no longer exists.")
        return
    active = await get_active_profile(user_id)
    is_active = bool(active and active.get("profile_id") == profile_id)
    text = (
        (f"{legacy._esc(note)}\n\n" if note else "")
        + f"<b>{legacy._esc(profile.get('name'))}</b>\n"
        + f"{legacy._esc(profile_summary(profile))}\n\n"
        + f"Status: {'Active' if is_active else 'Inactive'}"
    )
    rows = []
    if not is_active:
        rows.append([_b("Activate", f"pf:a:{profile_id}")])
    rows.extend([
        [_b("Edit", f"pf:e:{profile_id}"), _b("Apply Preset", f"ux54:apply:{profile_id}")],
        [_b("Rename", f"pf:r:{profile_id}"), _b("Duplicate", f"pf:d:{profile_id}")],
        [_b("Delete", f"pf:x:{profile_id}")],
        [_b("Back", "ux54:home")],
    ])
    await legacy._show(update, text, InlineKeyboardMarkup(rows))


def _preset_rows(prefix: str) -> list[list[Any]]:
    return [[_b(preset.name, f"{prefix}:{preset.preset_id}")] for preset in PRESETS]


async def _render_new_method(update: Update) -> None:
    await legacy._show(
        update,
        "<b>Create Profile</b>\n\nChoose a quick starting point or build every setting yourself.",
        InlineKeyboardMarkup([
            [_b("Quick Preset", "ux54:presets")],
            [_b("Custom Setup", "ux54:custom")],
            [_b("Back", "ux54:home")],
        ]),
    )


async def _render_presets(update: Update, *, existing_profile_id: str = "") -> None:
    prefix = f"ux54:pick:{existing_profile_id}" if existing_profile_id else "ux54:picknew"
    rows = _preset_rows(prefix)
    if existing_profile_id:
        rows.append([_b("Back", f"ux54:o:{existing_profile_id}")])
    else:
        rows.extend([[_b("Custom Setup", "ux54:custom")], [_b("Back", "ux54:new")]])
    await legacy._show(
        update,
        "<b>Choose a Preset</b>\n\nPresets only set understandable alert defaults. You can edit every setting afterward.",
        InlineKeyboardMarkup(rows),
    )


async def _render_preset_preview(update: Update, user_id: int, profile_id: str, preset_id: str) -> None:
    preset = get_preset(preset_id)
    profile = await get_profile(user_id, profile_id)
    if not preset or not profile:
        await _recover(update, user_id, "That preset or profile is no longer available.")
        return
    note = (
        "\n\nLocal SDR Mode changes alert preferences only; receiver connectivity remains a deployment setting."
        if preset_id == "local_sdr" else ""
    )
    await legacy._show(
        update,
        f"<b>{legacy._esc(preset.name)}</b>\n\n{legacy._esc(preset.description)}\n{legacy._esc(preset_summary(preset))}{note}\n\n"
        f"Apply this to <b>{legacy._esc(profile.get('name'))}</b>? Your saved location is kept. Advanced aircraft overrides are reset so the preset behaves as described.",
        InlineKeyboardMarkup([
            [_b("Apply Preset", f"ux54:confirm:{profile_id}:{preset_id}")],
            [_b("Back", f"ux54:apply:{profile_id}"), _b("Cancel", f"ux54:o:{profile_id}")],
        ]),
    )


async def _start_preset_create(update: Update, user_id: int, preset_id: str) -> None:
    preset = get_preset(preset_id)
    if not preset:
        await _recover(update, user_id, "That preset is no longer available.")
        return
    active = await ensure_default_profile(user_id)
    config = apply_preset(active.get("config") or {}, preset_id, preserve_location=True)
    temp = {
        "profile_flow": "create",
        "v54_preset_flow": True,
        "preset_id": preset_id,
        "draft_name": preset.name,
        "profile_draft": config,
    }
    await legacy._set_state(user_id, "v54:review", temp)
    await _render_review(update, user_id)


async def _render_review(update: Update, user_id: int) -> None:
    _, temp = await legacy._raw_state(user_id)
    config = temp.get("profile_draft") or {}
    preset = get_preset(str(temp.get("preset_id") or ""))
    loc = config.get("location") or {}
    prefs = config.get("preferences") or {}
    selection = legacy.normalized_filter_config(prefs)
    radius = loc.get("radius_km")
    location_text = "Saved" if loc.get("latitude") is not None and loc.get("longitude") is not None else "Needed"
    radius_text = f"{float(radius):g} km" if radius is not None else "Needed"
    aircraft_text = "All aircraft" if selection["mode"] == "all" else f"{len(selection['selected_categories'])} groups"
    await legacy._set_state(user_id, "v54:review", temp)
    text = (
        f"<b>{legacy._esc(temp.get('draft_name') or 'New Profile')}</b>\n\n"
        f"Preset: {legacy._esc(preset.name if preset else 'Custom')}\n"
        f"Location: {location_text}\n"
        f"Radius: {radius_text}\n"
        f"Aircraft: {legacy._esc(aircraft_text)}\n\n"
        "Review anything you want, then save."
    )
    await legacy._show(update, text, InlineKeyboardMarkup([
        [_b("Location", "ux54:loc"), _b("Radius", "ux54:radius")],
        [_b("Aircraft", "ux54:aircraft"), _b("Advanced", "ux54:advanced")],
        [_b("Change Preset", "ux54:presets"), _b("Save Profile", "ux54:save")],
        [_b("Cancel", "ux54:home")],
    ]))


def _validation_keyboard(error: str) -> InlineKeyboardMarkup:
    lower = error.casefold()
    if "location" in lower:
        target = _b("Set Location", "ux54:loc")
    elif "radius" in lower:
        target = _b("Set Radius", "ux54:radius")
    else:
        target = _b("Choose Aircraft", "ux54:aircraft")
    return InlineKeyboardMarkup([[target], [_b("Back", "ux54:review"), _b("Cancel", "ux54:home")]])


async def _save_preset_profile(update: Update, user_id: int) -> None:
    _, temp = await legacy._raw_state(user_id)
    config = temp.get("profile_draft") or {}
    error = legacy._validate_draft(config)
    if error:
        await legacy._show(update, f"<b>One thing needs attention</b>\n\n{legacy._esc(error)}", _validation_keyboard(error))
        return
    profile = await create_profile(user_id, temp.get("draft_name") or "Profile", config, activate=True)
    await users_col().update_one({"user_id": int(user_id)}, {"$set": {"setup_complete": True}})
    await legacy._clear_state(user_id)
    await _render_detail(update, user_id, profile["profile_id"], "Profile saved and activated.")


async def _render_status(update: Update, user_id: int) -> None:
    active = await ensure_default_profile(user_id)
    config = active.get("config") or {}
    loc = config.get("location") or {}
    await legacy._show(
        update,
        "<b>Profile Status</b>\n\n"
        f"Active: {legacy._esc(active.get('name'))}\n"
        f"Location saved: {'Yes' if loc.get('latitude') is not None and loc.get('longitude') is not None else 'No'}\n"
        f"{legacy._esc(profile_summary(active))}\n"
        f"Plane Alerts: v{VERSION}\n\n"
        "Your exact saved coordinates are intentionally not displayed here. Use /status for the full monitoring summary or /help for commands.",
        InlineKeyboardMarkup([[_b("Back", "ux54:home")]]),
    )


async def _recover(update: Update, user_id: int, reason: str = "This menu is no longer current.") -> None:
    await legacy._clear_state(user_id)
    text, keyboard = await _home_view(user_id)
    await legacy._show(update, f"{legacy._esc(reason)}\n\n{text}", keyboard)


async def cmd_profiles_v54(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    await ensure_default_profile(user.id)
    await legacy._clear_state(user.id)
    await _render_home(update, user.id)
    raise ApplicationHandlerStop


async def cmd_preferences_v54(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    active = await ensure_default_profile(user.id)
    await legacy._clear_state(user.id)
    await legacy._start_edit(update, user.id, active["profile_id"])
    raise ApplicationHandlerStop


async def callback_v54(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not query.data or not user:
        return
    data = query.data
    uid = user.id

    # Only intercept legacy Done when this is our preset review flow. Otherwise
    # return without stopping so the established v4.3 handler receives it.
    if data == "pf:adone":
        _, temp = await legacy._raw_state(uid)
        if not temp.get("v54_preset_flow"):
            return
        await query.answer()
        await _render_review(update, uid)
        raise ApplicationHandlerStop

    await query.answer()
    try:
        parts = data.split(":")
        if data in {"ux54:home", "pf:home"}:
            await legacy._clear_state(uid)
            await _render_home(update, uid)
        elif data in {"ux54:close", "pf:close"}:
            await legacy._clear_state(uid)
            await legacy._show(update, "Profiles closed. Use /profiles whenever you want to return.")
        elif data == "ux54:new":
            await _render_new_method(update)
        elif data == "ux54:presets":
            await _render_presets(update)
        elif data == "ux54:custom":
            await legacy._start_create(update, uid)
        elif data == "ux54:status":
            await _render_status(update, uid)
        elif data.startswith("ux54:o:") or data.startswith("pf:o:"):
            await legacy._clear_state(uid)
            await _render_detail(update, uid, parts[2])
        elif data.startswith("ux54:apply:") and len(parts) == 3:
            await _render_presets(update, existing_profile_id=parts[2])
        elif data.startswith("ux54:picknew:") and len(parts) == 3:
            await _start_preset_create(update, uid, parts[2])
        elif data.startswith("ux54:pick:") and len(parts) == 4:
            await _render_preset_preview(update, uid, parts[2], parts[3])
        elif data.startswith("ux54:confirm:") and len(parts) == 4:
            profile = await get_profile(uid, parts[2])
            preset = get_preset(parts[3])
            if not profile or not preset:
                await _recover(update, uid)
            else:
                config = apply_preset(profile.get("config") or {}, parts[3], preserve_location=True)
                await save_profile(uid, parts[2], config=config)
                await legacy._clear_state(uid)
                await _render_detail(update, uid, parts[2], f"{preset.name} applied. You can edit any setting.")
        elif data == "ux54:review":
            await _render_review(update, uid)
        elif data == "ux54:loc":
            _, temp = await legacy._raw_state(uid)
            if not temp.get("v54_preset_flow"):
                await _recover(update, uid)
            else:
                await legacy._set_state(uid, "v54:location", temp)
                await legacy._show(update, "<b>Profile Location</b>\n\nSend a Telegram location.", InlineKeyboardMarkup([[_b("Back", "ux54:review"), _b("Cancel", "ux54:home")]]))
        elif data == "ux54:radius":
            _, temp = await legacy._raw_state(uid)
            if not temp.get("v54_preset_flow"):
                await _recover(update, uid)
            else:
                await legacy._set_state(uid, "v54:radius", temp)
                await legacy._show(update, "<b>Alert Radius</b>\n\nSend a number from 1 to 150 km.", InlineKeyboardMarkup([[_b("Back", "ux54:review"), _b("Cancel", "ux54:home")]]))
        elif data == "ux54:aircraft":
            _, temp = await legacy._raw_state(uid)
            if not temp.get("v54_preset_flow"):
                await _recover(update, uid)
            else:
                await legacy._set_state(uid, "profile:aircraft", temp)
                await legacy._render_aircraft_menu(update, uid)
        elif data == "ux54:advanced":
            _, temp = await legacy._raw_state(uid)
            if not temp.get("v54_preset_flow"):
                await _recover(update, uid)
            else:
                await legacy._set_state(uid, "profile:aircraft", temp)
                await legacy._render_advanced(update, uid)
        elif data == "ux54:save":
            await _save_preset_profile(update, uid)
        else:
            await _recover(update, uid)
    except (KeyError, ValueError, TypeError, IndexError):
        await _recover(update, uid)
    raise ApplicationHandlerStop


async def location_v54(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.message
    if not user or not message or not message.location:
        return
    state, temp = await legacy._raw_state(user.id)
    if state != "v54:location" or not temp.get("v54_preset_flow"):
        return
    lat = float(message.location.latitude)
    lon = float(message.location.longitude)
    draft = temp.setdefault("profile_draft", {})
    old = dict(draft.get("location") or {})
    draft["location"] = {**old, "latitude": lat, "longitude": lon, "geohash": compute_geohash(lat, lon)}
    await legacy._set_state(user.id, "v54:review", temp)
    await _render_review(update, user.id)
    raise ApplicationHandlerStop


async def text_v54(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.message
    if not user or not message or message.text is None:
        return
    state, temp = await legacy._raw_state(user.id)
    if state != "v54:radius" or not temp.get("v54_preset_flow"):
        return
    try:
        value = float(message.text.strip())
        if not 1 <= value <= 150:
            raise ValueError
    except ValueError:
        await message.reply_text("Send a radius from 1 to 150 km, or use the Back button.")
        raise ApplicationHandlerStop
    draft = temp.setdefault("profile_draft", {})
    draft.setdefault("location", {})["radius_km"] = value
    await legacy._set_state(user.id, "v54:review", temp)
    await _render_review(update, user.id)
    raise ApplicationHandlerStop


def register_v54_handlers(app: Application) -> None:
    """Register UX handlers ahead of the established profile engine."""
    group = -40
    app.add_handler(CommandHandler("profiles", cmd_profiles_v54), group=group)
    app.add_handler(CommandHandler("preferences", cmd_preferences_v54), group=group)
    app.add_handler(
        CallbackQueryHandler(callback_v54, pattern=r"^(?:ux54:|pf:home$|pf:close$|pf:o:|pf:adone$)"),
        group=group,
    )
    app.add_handler(MessageHandler(filters.LOCATION, location_v54), group=group)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_v54), group=group)
