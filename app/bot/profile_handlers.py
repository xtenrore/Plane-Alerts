"""Telegram-only profile, aircraft catalogue and advanced-filter UI for v4.3."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import html
import math
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
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

from app.aircraft.filtering import normalized_filter_config, normalized_rule_config
from app.aircraft.operators import normalize_operator_list, operator_display
from app.aircraft.registry import (
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    category_members,
    get_aircraft_type,
    primary_category,
    search_aircraft,
)
from app.alert_profiles import (
    activate_profile,
    blank_config_from,
    clean_name,
    create_profile,
    delete_profile,
    duplicate_profile,
    ensure_default_profile,
    get_active_profile,
    get_profile,
    list_profiles,
    migrate_existing_users,
    profile_summary,
    rename_profile,
    save_profile,
)
from app.database import user_state_col, users_col
from app.worker.geo import compute_geohash

PAGE_SIZE = 10
PROFILE_PREFIX = "profile:"


def _button(text: str, data: str) -> InlineKeyboardButton:
    # callback_data is intentionally compact and always stays below Telegram's
    # 64-byte limit with 10-char profile ids / 4-char ICAO codes.
    if len(data.encode("utf-8")) > 64:
        raise ValueError("callback_data exceeds Telegram 64-byte limit")
    return InlineKeyboardButton(text, callback_data=data)


async def _raw_state(user_id: int) -> tuple[str, dict[str, Any]]:
    doc = await user_state_col().find_one({"user_id": int(user_id)})
    if not doc:
        return "idle", {}
    return str(doc.get("current_state") or "idle"), dict(doc.get("temp_data") or {})


async def _set_state(user_id: int, state: str, temp: dict[str, Any] | None = None) -> None:
    await user_state_col().update_one(
        {"user_id": int(user_id)},
        {"$set": {"current_state": state, "temp_data": deepcopy(temp or {})}},
        upsert=True,
    )


async def _clear_state(user_id: int) -> None:
    await _set_state(user_id, "idle", {})


async def _show(update: Update, text: str, keyboard: InlineKeyboardMarkup | None = None) -> None:
    query = update.callback_query
    if query and query.message:
        try:
            await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            return
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            if not any(reason in str(exc).lower() for reason in ("message to edit not found", "message can't be edited", "there is no text")):
                raise
    if update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    elif query and query.message:
        await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


def _esc(value: Any) -> str:
    return html.escape(str(value or ""))


async def _profiles_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    active = await get_active_profile(user_id)
    profiles = await list_profiles(user_id)
    lines = ["<b>Profiles</b>", ""]
    rows: list[list[InlineKeyboardButton]] = []
    for profile in profiles:
        marker = "✓ " if active and profile["profile_id"] == active["profile_id"] else ""
        lines.append(f"<b>{marker}{_esc(profile.get('name'))}</b>")
        lines.append(_esc(profile_summary(profile)))
        lines.append("")
        rows.append([_button(f"{marker}{profile.get('name')}", f"pf:o:{profile['profile_id']}")])
    rows.append([_button("New Profile", "pf:new"), _button("Close", "pf:close")])
    return "\n".join(lines).rstrip(), InlineKeyboardMarkup(rows)


async def _render_profiles(update: Update, user_id: int) -> None:
    text, keyboard = await _profiles_view(user_id)
    await _show(update, text, keyboard)


async def cmd_profiles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return
    await ensure_default_profile(user.id)
    await _clear_state(user.id)
    await _render_profiles(update, user.id)
    raise ApplicationHandlerStop


async def cmd_preferences(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Replace the legacy limited selector with the active profile editor."""
    user = update.effective_user
    if user is None:
        return
    profile = await ensure_default_profile(user.id)
    await _set_state(
        user.id,
        "profile:aircraft",
        {
            "profile_flow": "edit",
            "editing_profile_id": profile["profile_id"],
            "draft_name": profile.get("name") or "Default",
            "profile_draft": deepcopy(profile.get("config") or {}),
            "profile_return": "edit",
        },
    )
    await _render_aircraft_menu(update, user.id)
    raise ApplicationHandlerStop


def _profile_manage_keyboard(profile_id: str, active: bool) -> InlineKeyboardMarkup:
    rows = []
    if not active:
        rows.append([_button("Activate", f"pf:a:{profile_id}")])
    rows.extend([
        [_button("Edit", f"pf:e:{profile_id}"), _button("Rename", f"pf:r:{profile_id}")],
        [_button("Duplicate", f"pf:d:{profile_id}"), _button("Delete", f"pf:x:{profile_id}")],
        [_button("Back", "pf:home")],
    ])
    return InlineKeyboardMarkup(rows)


async def _render_profile_detail(update: Update, user_id: int, profile_id: str) -> None:
    profile = await get_profile(user_id, profile_id)
    if not profile:
        await _stale(update, user_id)
        return
    active = await get_active_profile(user_id)
    is_active = bool(active and active["profile_id"] == profile_id)
    text = (
        f"<b>{_esc(profile.get('name'))}</b>\n"
        f"{_esc(profile_summary(profile))}\n\n"
        f"Status: {'Active' if is_active else 'Inactive'}"
    )
    await _show(update, text, _profile_manage_keyboard(profile_id, is_active))


async def _start_create(update: Update, user_id: int) -> None:
    active = await ensure_default_profile(user_id)
    draft = blank_config_from(active.get("config") or {})
    await _set_state(user_id, "profile:name", {"profile_flow": "create", "profile_draft": draft})
    await _show(
        update,
        "<b>Create New Profile</b>\n\nSend a name for this profile.",
        InlineKeyboardMarkup([[_button("Cancel", "pf:home")]]),
    )


async def _start_edit(update: Update, user_id: int, profile_id: str) -> None:
    profile = await get_profile(user_id, profile_id)
    if not profile:
        await _stale(update, user_id)
        return
    await _set_state(
        user_id,
        "profile:edit_menu",
        {
            "profile_flow": "edit",
            "editing_profile_id": profile_id,
            "draft_name": profile.get("name") or "Profile",
            "profile_draft": deepcopy(profile.get("config") or {}),
            "profile_return": "edit",
        },
    )
    await _render_edit_menu(update, user_id)


async def _render_edit_menu(update: Update, user_id: int) -> None:
    state, temp = await _raw_state(user_id)
    draft = temp.get("profile_draft") or {}
    loc = draft.get("location") or {}
    prefs = draft.get("preferences") or {}
    selection = normalized_filter_config(prefs)
    radius = loc.get("radius_km")
    location_text = "Set" if loc.get("latitude") is not None and loc.get("longitude") is not None else "Not set"
    aircraft_text = "All Aircraft" if selection["mode"] == "all" else (
        f"{len(selection['selected_categories'])} categories, {len(selection['selected_types'])} aircraft"
    )
    text = (
        f"<b>Edit {_esc(temp.get('draft_name') or 'Profile')}</b>\n\n"
        f"Location: {location_text}\n"
        f"Radius: {float(radius):g} km\n" if radius is not None else
        f"<b>Edit {_esc(temp.get('draft_name') or 'Profile')}</b>\n\nLocation: {location_text}\nRadius: Not set\n"
    )
    if radius is not None:
        text += f"Aircraft: {_esc(aircraft_text)}"
    else:
        text += f"Aircraft: {_esc(aircraft_text)}"
    keyboard = InlineKeyboardMarkup([
        [_button("Location", "pf:el"), _button("Radius", "pf:er")],
        [_button("Aircraft", "pf:ea"), _button("Advanced Settings", "pf:adv")],
        [_button("Save", "pf:se"), _button("Cancel", "pf:ec")],
    ])
    await _show(update, text, keyboard)


def _draft_selection(temp: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    draft = temp.setdefault("profile_draft", {})
    prefs = draft.setdefault("preferences", {})
    selection = normalized_filter_config(prefs)
    prefs["aircraft_filter"] = selection
    prefs.setdefault("filter_rules", {"profile": {}, "categories": {}, "aircraft": {}})
    return draft, prefs, selection


def _type_selected(selection: dict[str, Any], code: str, category: str | None = None) -> bool:
    code = code.upper()
    if selection["mode"] == "all":
        return True
    if code in selection["selected_types"]:
        return True
    if code in selection["excluded_types"]:
        return False
    if category and category in selection["selected_categories"]:
        return True
    item = get_aircraft_type(code)
    return bool(item and set(item.categories).intersection(selection["selected_categories"]))


async def _render_aircraft_menu(update: Update, user_id: int) -> None:
    _, temp = await _raw_state(user_id)
    _, _, selection = _draft_selection(temp)
    await _set_state(user_id, "profile:aircraft", temp)
    all_on = selection["mode"] == "all"
    rows: list[list[InlineKeyboardButton]] = [[_button(f"{'✓ ' if all_on else ''}All Aircraft", "pf:all")]]
    category_buttons = []
    for category in CATEGORY_ORDER:
        selected = category in selection["selected_categories"]
        category_buttons.append(_button(f"{'✓ ' if selected else ''}{CATEGORY_LABELS[category]}", f"pf:c:{category}:0"))
    for index in range(0, len(category_buttons), 2):
        rows.append(category_buttons[index:index + 2])
    rows.extend([
        [_button("Search Aircraft", "pf:search"), _button("Advanced Settings", "pf:adv")],
        [_button("Done", "pf:adone"), _button("Cancel", "pf:acancel")],
    ])
    text = (
        "<b>Select Aircraft</b>\n\n"
        "Choose categories, individual types, or logical All Aircraft mode. "
        "Trajectory, CPA and pass qualification still apply after this filter."
    )
    await _show(update, text, InlineKeyboardMarkup(rows))


async def _render_category(update: Update, user_id: int, category: str, page: int) -> None:
    if category not in CATEGORY_LABELS:
        await _stale(update, user_id)
        return
    _, temp = await _raw_state(user_id)
    _, _, selection = _draft_selection(temp)
    members = category_members(category)
    pages = max(1, math.ceil(len(members) / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    chunk = members[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    rows: list[list[InlineKeyboardButton]] = []
    buttons = [
        _button(
            f"{'✓' if _type_selected(selection, item.code, category) else '○'} {item.code}",
            f"pf:t:{category}:{page}:{item.code}",
        )
        for item in chunk
    ]
    for index in range(0, len(buttons), 2):
        rows.append(buttons[index:index + 2])
    rows.append([_button("Select All", f"pf:sa:{category}:{page}"), _button("Clear All", f"pf:ca:{category}:{page}")])
    nav = []
    if page > 0:
        nav.append(_button("Previous", f"pf:c:{category}:{page - 1}"))
    if page + 1 < pages:
        nav.append(_button("Next", f"pf:c:{category}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_button("Back", "pf:ab")])
    await _show(
        update,
        f"<b>{_esc(CATEGORY_LABELS[category])}</b>\nPage {page + 1}/{pages}\n\nTap an aircraft type to toggle it.",
        InlineKeyboardMarkup(rows),
    )


async def _toggle_type(user_id: int, code: str) -> None:
    _, temp = await _raw_state(user_id)
    _, prefs, selection = _draft_selection(temp)
    code = code.upper()
    selected_types = set(selection["selected_types"])
    excluded = set(selection["excluded_types"])
    if _type_selected(selection, code):
        if code in selected_types:
            selected_types.remove(code)
            # If a selected category would immediately re-enable it, preserve
            # the user's explicit off choice as an exclusion.
            item = get_aircraft_type(code)
            if item and set(item.categories).intersection(selection["selected_categories"]):
                excluded.add(code)
        else:
            excluded.add(code)
    else:
        selected_types.add(code)
        excluded.discard(code)
    selection["selected_types"] = sorted(selected_types)
    selection["excluded_types"] = sorted(excluded)
    prefs["aircraft_filter"] = selection
    await _set_state(user_id, "profile:aircraft", temp)


async def _category_select_all(user_id: int, category: str, enabled: bool) -> None:
    _, temp = await _raw_state(user_id)
    _, prefs, selection = _draft_selection(temp)
    categories = set(selection["selected_categories"])
    excluded = set(selection["excluded_types"])
    if enabled:
        categories.add(category)
        for item in category_members(category):
            excluded.discard(item.code)
    else:
        categories.discard(category)
    selection["selected_categories"] = sorted(categories)
    selection["excluded_types"] = sorted(excluded)
    prefs["aircraft_filter"] = selection
    await _set_state(user_id, "profile:aircraft", temp)


async def _begin_search(update: Update, user_id: int, purpose: str = "select") -> None:
    state, temp = await _raw_state(user_id)
    temp["profile_search_purpose"] = purpose
    await _set_state(user_id, "profile:search", temp)
    await _show(
        update,
        "<b>Aircraft Search</b>\n\nSend an ICAO type, aircraft name, manufacturer or alias.\nExamples: <code>AN12</code>, <code>A350-900</code>, <code>Gulfstream</code>, <code>747</code>.",
        InlineKeyboardMarkup([[_button("Back", "pf:ab")]]),
    )


async def _render_search_results(update: Update, user_id: int, query_text: str) -> None:
    _, temp = await _raw_state(user_id)
    results = search_aircraft(query_text, limit=30)
    temp["profile_search_query"] = query_text
    temp["profile_search_codes"] = [item.code for item in results]
    await _set_state(user_id, "profile:search", temp)
    purpose = temp.get("profile_search_purpose") or "select"
    rows: list[list[InlineKeyboardButton]] = []
    for item in results[:20]:
        if purpose == "rule":
            rows.append([_button(f"{item.code} — {item.model}", f"pf:ra:{item.code}")])
        else:
            selection = normalized_filter_config((temp.get("profile_draft") or {}).get("preferences") or {})
            marker = "✓" if _type_selected(selection, item.code) else "○"
            rows.append([_button(f"{marker} {item.code} — {item.model}", f"pf:sr:{item.code}")])
    rows.append([_button("Back", "pf:ab" if purpose == "select" else "pf:rl:a")])
    if not results:
        text = f"<b>No results for {_esc(query_text)}</b>\n\nTry an ICAO code, family, manufacturer or common model name."
    else:
        text = f"<b>Search: {_esc(query_text)}</b>\n{len(results)} result{'s' if len(results) != 1 else ''}"
    await _show(update, text, InlineKeyboardMarkup(rows))


async def _render_advanced(update: Update, user_id: int) -> None:
    await _show(
        update,
        "<b>Advanced Settings</b>\n\nMore specific settings override their parent.",
        InlineKeyboardMarkup([
            [_button("All Aircraft", "pf:rl:p")],
            [_button("Per Category", "pf:rl:c")],
            [_button("Per Aircraft", "pf:rl:a")],
            [_button("Back", "pf:ab")],
        ]),
    )


async def _render_rule_categories(update: Update) -> None:
    rows = [[_button(CATEGORY_LABELS[cat], f"pf:rc:{cat}")] for cat in CATEGORY_ORDER]
    rows.append([_button("Back", "pf:adv")])
    await _show(update, "<b>Per Category Settings</b>", InlineKeyboardMarkup(rows))


def _selected_rule_aircraft(temp: dict[str, Any]) -> list[str]:
    prefs = (temp.get("profile_draft") or {}).get("preferences") or {}
    selection = normalized_filter_config(prefs)
    codes = set(selection["selected_types"])
    for category in selection["selected_categories"]:
        codes.update(item.code for item in category_members(category))
    codes.difference_update(selection["excluded_types"])
    return sorted(codes)


async def _render_rule_aircraft(update: Update, user_id: int, page: int = 0) -> None:
    _, temp = await _raw_state(user_id)
    codes = _selected_rule_aircraft(temp)
    pages = max(1, math.ceil(len(codes) / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    chunk = codes[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    rows = [[_button(f"{code} — {(get_aircraft_type(code).model if get_aircraft_type(code) else 'Unknown type')}", f"pf:ra:{code}")] for code in chunk]
    nav = []
    if page > 0:
        nav.append(_button("Previous", f"pf:rap:{page - 1}"))
    if page + 1 < pages:
        nav.append(_button("Next", f"pf:rap:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_button("Search Aircraft", "pf:ras"), _button("Back", "pf:adv")])
    await _show(update, f"<b>Per Aircraft Settings</b>\nPage {page + 1}/{pages}", InlineKeyboardMarkup(rows))


def _rules(temp: dict[str, Any]) -> dict[str, Any]:
    draft = temp.setdefault("profile_draft", {})
    prefs = draft.setdefault("preferences", {})
    rules = normalized_rule_config(prefs)
    prefs["filter_rules"] = rules
    return rules


def _rule_object(temp: dict[str, Any], scope: str, target: str, *, create: bool = True) -> dict[str, Any]:
    rules = _rules(temp)
    if scope == "profile":
        return rules["profile"]
    bucket = rules["categories"] if scope == "category" else rules["aircraft"]
    if create:
        return bucket.setdefault(target, {})
    return bucket.get(target, {})


def _effective_rule_for_ui(temp: dict[str, Any], scope: str, target: str) -> dict[str, Any]:
    system = {"enabled": True, "min_altitude_ft": None, "max_altitude_ft": None, "airline_mode": "all", "airlines": [], "radius_km": None}
    rules = _rules(temp)
    system.update(rules["profile"])
    if scope == "category":
        system.update(rules["categories"].get(target, {}))
    elif scope == "aircraft":
        category = primary_category(target)
        system.update(rules["categories"].get(category, {}))
        system.update(rules["aircraft"].get(target, {}))
    return system


def _fmt_alt(value: Any, low: bool) -> str:
    if value is None:
        return "No minimum" if low else "No maximum"
    return f"{float(value):g} ft"


async def _render_rule(update: Update, user_id: int, scope: str, target: str = "") -> None:
    if scope not in {"profile", "category", "aircraft"}:
        await _stale(update, user_id)
        return
    if scope == "category" and target not in CATEGORY_LABELS:
        await _stale(update, user_id)
        return
    if scope == "aircraft" and not target:
        await _stale(update, user_id)
        return
    state, temp = await _raw_state(user_id)
    temp["rule_scope"] = scope
    temp["rule_target"] = target.upper() if scope == "aircraft" else target
    await _set_state(user_id, "profile:rule", temp)
    own = _rule_object(temp, scope, temp["rule_target"], create=False)
    effective = _effective_rule_for_ui(temp, scope, temp["rule_target"])
    title = "All Aircraft Settings" if scope == "profile" else (
        f"{CATEGORY_LABELS[target]} Settings" if scope == "category" else f"{target.upper()} Settings"
    )
    airline_mode = str(effective.get("airline_mode") or "all").title()
    airline_values = effective.get("airlines") or []
    airline_text = ", ".join(operator_display(v) for v in airline_values) if airline_values else "None"
    inherit_note = "\nUse Parent Setting is available for every override." if scope != "profile" else ""
    text = (
        f"<b>{_esc(title)}</b>\n\n"
        f"Altitude: {_fmt_alt(effective.get('min_altitude_ft'), True)} – {_fmt_alt(effective.get('max_altitude_ft'), False)}\n"
        f"Airlines: {airline_mode} ({_esc(airline_text)})\n"
        f"Enabled: {'Yes' if effective.get('enabled', True) else 'No'}\n"
        f"Radius: {float(effective['radius_km']):g} km" if effective.get("radius_km") is not None else
        f"<b>{_esc(title)}</b>\n\nAltitude: {_fmt_alt(effective.get('min_altitude_ft'), True)} – {_fmt_alt(effective.get('max_altitude_ft'), False)}\nAirlines: {airline_mode} ({_esc(airline_text)})\nEnabled: {'Yes' if effective.get('enabled', True) else 'No'}\nRadius: Profile radius"
    )
    text += inherit_note
    rows = [
        [_button("Minimum Altitude", "pf:rf:min"), _button("Maximum Altitude", "pf:rf:max")],
        [_button("Airline Mode", "pf:rf:mode"), _button("Airlines", "pf:rf:air")],
        [_button("Enable / Disable", "pf:rf:enabled"), _button("Radius", "pf:rf:radius")],
    ]
    if scope != "profile":
        rows.append([_button("Use Parent Settings", "pf:rf:reset")])
    rows.append([_button("Back", "pf:adv")])
    await _show(update, text, InlineKeyboardMarkup(rows))


async def _cycle_rule_field(user_id: int, field: str) -> None:
    _, temp = await _raw_state(user_id)
    scope = temp.get("rule_scope")
    target = temp.get("rule_target") or ""
    if scope not in {"profile", "category", "aircraft"}:
        return
    own = _rule_object(temp, scope, target)
    if field == "airline_mode":
        current = own.get(field)
        sequence = [None, "all", "whitelist", "blacklist"] if scope != "profile" else [None, "whitelist", "blacklist", "all"]
        try:
            next_value = sequence[(sequence.index(current) + 1) % len(sequence)]
        except ValueError:
            next_value = "all"
        if next_value is None:
            own.pop(field, None)
            own.pop("airlines", None)
        else:
            own[field] = next_value
            if next_value == "all":
                own["airlines"] = []
    elif field == "enabled":
        current = own.get(field, None)
        next_value = True if current is None else (False if current is True else None)
        if next_value is None:
            own.pop(field, None)
        else:
            own[field] = next_value
    await _set_state(user_id, "profile:rule", temp)


async def _begin_rule_text(update: Update, user_id: int, field: str) -> None:
    state, temp = await _raw_state(user_id)
    if not temp.get("rule_scope"):
        await _stale(update, user_id)
        return
    temp["rule_input_field"] = field
    await _set_state(user_id, "profile:rule_input", temp)
    if field in {"min_altitude_ft", "max_altitude_ft"}:
        hint = "Send an altitude in feet. Send <code>none</code> for no limit"
    elif field == "radius_km":
        hint = "Send a radius from 1 to 150 km"
    else:
        hint = "Send airline/operator names or codes separated by commas"
    if temp.get("rule_scope") != "profile":
        hint += ", or send <code>parent</code> to use the parent setting."
    else:
        hint += "."
    await _show(update, f"<b>Set {field.replace('_', ' ').title()}</b>\n\n{hint}", InlineKeyboardMarkup([[_button("Back", "pf:ruleback")]]))


def _validate_draft(config: dict[str, Any]) -> str | None:
    loc = config.get("location") or {}
    if loc.get("latitude") is None or loc.get("longitude") is None:
        return "A profile needs a location before it can be saved."
    radius = loc.get("radius_km")
    try:
        radius_value = float(radius)
    except (TypeError, ValueError):
        return "A profile needs a valid alert radius."
    if not 1 <= radius_value <= 150:
        return "Alert radius must be between 1 and 150 km."
    prefs = config.get("preferences") or {}
    selection = normalized_filter_config(prefs)
    if selection["mode"] != "all" and not selection["selected_categories"] and not selection["selected_types"]:
        return "Select at least one aircraft/category, or enable All Aircraft."
    rules = normalized_rule_config(prefs)
    for rule in [rules["profile"], *rules["categories"].values(), *rules["aircraft"].values()]:
        low = rule.get("min_altitude_ft")
        high = rule.get("max_altitude_ft")
        if low is not None and high is not None and float(low) > float(high):
            return "A minimum altitude cannot be higher than its maximum altitude."
    return None


async def _render_create_confirmation(update: Update, user_id: int) -> None:
    _, temp = await _raw_state(user_id)
    config = temp.get("profile_draft") or {}
    error = _validate_draft(config)
    if error:
        query = update.callback_query
        if query:
            await query.answer(error, show_alert=True)
        else:
            await _show(update, _esc(error))
        return
    loc = config.get("location") or {}
    selection = normalized_filter_config((config.get("preferences") or {}))
    aircraft = "All Aircraft" if selection["mode"] == "all" else f"{len(selection['selected_categories'])} categories · {len(selection['selected_types'])} individual types"
    text = (
        f"<b>Save Profile?</b>\n\n"
        f"Name: {_esc(temp.get('draft_name'))}\n"
        f"Radius: {float(loc.get('radius_km')):g} km\n"
        f"Aircraft: {_esc(aircraft)}"
    )
    await _show(update, text, InlineKeyboardMarkup([[_button("Save Profile", "pf:sn")], [_button("Back", "pf:ab"), _button("Cancel", "pf:home")]]))


async def _save_new(update: Update, user_id: int) -> None:
    _, temp = await _raw_state(user_id)
    config = temp.get("profile_draft") or {}
    error = _validate_draft(config)
    if error:
        await _show(update, _esc(error), InlineKeyboardMarkup([[_button("Back", "pf:ab")]]))
        return
    profile = await create_profile(user_id, temp.get("draft_name") or "Profile", config, activate=True)
    await users_col().update_one({"user_id": int(user_id)}, {"$set": {"setup_complete": True}})
    await _clear_state(user_id)
    await _show(update, f"Active profile changed to:\n<b>{_esc(profile.get('name'))}</b>", InlineKeyboardMarkup([[_button("Profiles", "pf:home")]]))


async def _save_edit(update: Update, user_id: int) -> None:
    _, temp = await _raw_state(user_id)
    profile_id = str(temp.get("editing_profile_id") or "")
    config = temp.get("profile_draft") or {}
    error = _validate_draft(config)
    if error:
        query = update.callback_query
        if query:
            await query.answer(error, show_alert=True)
        return
    await save_profile(user_id, profile_id, config=config)
    await _clear_state(user_id)
    await _render_profile_detail(update, user_id, profile_id)


async def _stale(update: Update, user_id: int) -> None:
    await _clear_state(user_id)
    text, keyboard = await _profiles_view(user_id)
    await _show(update, "This menu is no longer current.\n\n" + text, keyboard)


async def profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not query.data or not user:
        return
    await query.answer()
    data = query.data
    uid = user.id
    try:
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        if data == "pf:home":
            await _clear_state(uid)
            await _render_profiles(update, uid)
        elif data == "pf:close":
            await _clear_state(uid)
            await _show(update, "Profiles closed. Use /profiles to return.")
        elif data == "pf:new":
            await _start_create(update, uid)
        elif action == "o" and len(parts) == 3:
            if await get_profile(uid, parts[2]):
                await _clear_state(uid)
            await _render_profile_detail(update, uid, parts[2])
        elif action == "a" and len(parts) == 3:
            profile = await activate_profile(uid, parts[2])
            await _clear_state(uid)
            await _show(update, f"Active profile changed to:\n<b>{_esc(profile.get('name'))}</b>", InlineKeyboardMarkup([[_button("Profiles", "pf:home")]]))
        elif action == "e" and len(parts) == 3:
            await _start_edit(update, uid, parts[2])
        elif action == "r" and len(parts) == 3:
            if not await get_profile(uid, parts[2]):
                await _stale(update, uid)
            else:
                await _set_state(uid, "profile:rename", {"rename_profile_id": parts[2]})
                await _show(update, "<b>Rename Profile</b>\n\nSend the new profile name.", InlineKeyboardMarkup([[_button("Cancel", f"pf:o:{parts[2]}")]]))
        elif action == "d" and len(parts) == 3:
            duplicate = await duplicate_profile(uid, parts[2])
            await _render_profile_detail(update, uid, duplicate["profile_id"])
        elif action == "x" and len(parts) == 3:
            profile = await get_profile(uid, parts[2])
            if not profile:
                await _stale(update, uid)
            else:
                await _show(update, f"Delete <b>{_esc(profile.get('name'))}</b>?", InlineKeyboardMarkup([[_button("Delete", f"pf:xd:{parts[2]}"), _button("Cancel", f"pf:o:{parts[2]}")]]))
        elif action == "xd" and len(parts) == 3:
            await delete_profile(uid, parts[2])
            await _clear_state(uid)
            await _render_profiles(update, uid)
        elif data == "pf:use_loc":
            _, temp = await _raw_state(uid)
            active = await get_active_profile(uid)
            loc = deepcopy(((active or {}).get("config") or {}).get("location") or {})
            if loc.get("latitude") is None:
                await query.answer("The active profile has no saved location.", show_alert=True)
            else:
                draft = temp.setdefault("profile_draft", {})
                draft["location"] = loc
                await _set_state(uid, "profile:radius", temp)
                await _show(update, "<b>Alert Radius</b>\n\nSend a radius from 1 to 150 km.", InlineKeyboardMarkup([[_button("Cancel", "pf:home")]]))
        elif data == "pf:all":
            _, temp = await _raw_state(uid)
            _, prefs, selection = _draft_selection(temp)
            selection["mode"] = "selected" if selection["mode"] == "all" else "all"
            prefs["aircraft_filter"] = selection
            await _set_state(uid, "profile:aircraft", temp)
            await _render_aircraft_menu(update, uid)
        elif action == "c" and len(parts) == 4:
            await _render_category(update, uid, parts[2], int(parts[3]))
        elif action == "t" and len(parts) == 5:
            await _toggle_type(uid, parts[4])
            await _render_category(update, uid, parts[2], int(parts[3]))
        elif action == "sa" and len(parts) == 4:
            await _category_select_all(uid, parts[2], True)
            await _render_category(update, uid, parts[2], int(parts[3]))
        elif action == "ca" and len(parts) == 4:
            await _category_select_all(uid, parts[2], False)
            await _render_category(update, uid, parts[2], int(parts[3]))
        elif data == "pf:ab":
            await _render_aircraft_menu(update, uid)
        elif data == "pf:search":
            await _begin_search(update, uid, "select")
        elif action == "sr" and len(parts) == 3:
            await _toggle_type(uid, parts[2])
            _, temp = await _raw_state(uid)
            await _render_search_results(update, uid, str(temp.get("profile_search_query") or parts[2]))
        elif data == "pf:adv":
            await _render_advanced(update, uid)
        elif data == "pf:rl:p":
            await _render_rule(update, uid, "profile")
        elif data == "pf:rl:c":
            await _render_rule_categories(update)
        elif data == "pf:rl:a":
            await _render_rule_aircraft(update, uid, 0)
        elif action == "rap" and len(parts) == 3:
            await _render_rule_aircraft(update, uid, int(parts[2]))
        elif data == "pf:ras":
            await _begin_search(update, uid, "rule")
        elif action == "rc" and len(parts) == 3:
            await _render_rule(update, uid, "category", parts[2])
        elif action == "ra" and len(parts) == 3:
            await _render_rule(update, uid, "aircraft", parts[2].upper())
        elif action == "rf" and len(parts) == 3:
            field = parts[2]
            if field == "min":
                await _begin_rule_text(update, uid, "min_altitude_ft")
            elif field == "max":
                await _begin_rule_text(update, uid, "max_altitude_ft")
            elif field == "radius":
                await _begin_rule_text(update, uid, "radius_km")
            elif field == "air":
                await _begin_rule_text(update, uid, "airlines")
            elif field == "mode":
                await _cycle_rule_field(uid, "airline_mode")
                _, temp = await _raw_state(uid)
                await _render_rule(update, uid, temp.get("rule_scope"), temp.get("rule_target") or "")
            elif field == "enabled":
                await _cycle_rule_field(uid, "enabled")
                _, temp = await _raw_state(uid)
                await _render_rule(update, uid, temp.get("rule_scope"), temp.get("rule_target") or "")
            elif field == "reset":
                _, temp = await _raw_state(uid)
                scope, target = temp.get("rule_scope"), temp.get("rule_target") or ""
                rules = _rules(temp)
                if scope == "category":
                    rules["categories"].pop(target, None)
                elif scope == "aircraft":
                    rules["aircraft"].pop(target, None)
                await _set_state(uid, "profile:rule", temp)
                await _render_rule(update, uid, scope, target)
        elif data == "pf:ruleback":
            _, temp = await _raw_state(uid)
            await _render_rule(update, uid, temp.get("rule_scope"), temp.get("rule_target") or "")
        elif data == "pf:adone":
            _, temp = await _raw_state(uid)
            if temp.get("profile_flow") == "create":
                await _render_create_confirmation(update, uid)
            else:
                await _render_edit_menu(update, uid)
        elif data == "pf:sn":
            await _save_new(update, uid)
        elif data == "pf:el":
            _, temp = await _raw_state(uid)
            temp["profile_location_return"] = "edit"
            await _set_state(uid, "profile:location", temp)
            await _show(update, "<b>Profile Location</b>\n\nSend a Telegram location.", InlineKeyboardMarkup([[_button("Back", "pf:eback")]]))
        elif data == "pf:er":
            _, temp = await _raw_state(uid)
            temp["profile_radius_return"] = "edit"
            await _set_state(uid, "profile:radius", temp)
            await _show(update, "<b>Alert Radius</b>\n\nSend a radius from 1 to 150 km.", InlineKeyboardMarkup([[_button("Back", "pf:eback")]]))
        elif data == "pf:ea":
            await _render_aircraft_menu(update, uid)
        elif data == "pf:se":
            await _save_edit(update, uid)
        elif data in {"pf:ec", "pf:acancel"}:
            _, temp = await _raw_state(uid)
            profile_id = temp.get("editing_profile_id")
            await _clear_state(uid)
            if profile_id:
                await _render_profile_detail(update, uid, str(profile_id))
            else:
                await _render_profiles(update, uid)
        elif data == "pf:eback":
            await _render_edit_menu(update, uid)
        else:
            await _stale(update, uid)
    except (KeyError, ValueError, TypeError):
        await _stale(update, uid)
    raise ApplicationHandlerStop


async def profile_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.message
    if not user or not message or not message.location:
        return
    state, temp = await _raw_state(user.id)
    if state != "profile:location":
        return
    draft = temp.setdefault("profile_draft", {})
    old_loc = dict(draft.get("location") or {})
    lat = float(message.location.latitude)
    lon = float(message.location.longitude)
    draft["location"] = {
        **old_loc,
        "latitude": lat,
        "longitude": lon,
        "geohash": compute_geohash(lat, lon),
    }
    if temp.get("profile_location_return") == "edit":
        await _set_state(user.id, "profile:edit_menu", temp)
        await _render_edit_menu(update, user.id)
    else:
        await _set_state(user.id, "profile:radius", temp)
        await message.reply_text("<b>Alert Radius</b>\n\nSend a radius from 1 to 150 km.", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[_button("Cancel", "pf:home")]]))
    raise ApplicationHandlerStop


async def profile_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.message
    if not user or not message or message.text is None:
        return
    state, temp = await _raw_state(user.id)
    if not state.startswith(PROFILE_PREFIX):
        return
    text = message.text.strip()
    try:
        if state == "profile:name":
            temp["draft_name"] = clean_name(text)
            await _set_state(user.id, "profile:location", temp)
            await message.reply_text(
                "<b>Profile Location</b>\n\nSend a Telegram location, or use the current active profile location.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[_button("Use Active Location", "pf:use_loc")], [_button("Cancel", "pf:home")]]),
            )
        elif state == "profile:rename":
            profile = await rename_profile(user.id, str(temp.get("rename_profile_id") or ""), text)
            await _clear_state(user.id)
            await message.reply_text(f"Profile renamed to <b>{_esc(profile.get('name'))}</b>.", parse_mode=ParseMode.HTML)
        elif state == "profile:radius":
            radius = float(text)
            if not 1 <= radius <= 150:
                raise ValueError("radius")
            draft = temp.setdefault("profile_draft", {})
            loc = draft.setdefault("location", {})
            loc["radius_km"] = radius
            if temp.get("profile_radius_return") == "edit":
                await _set_state(user.id, "profile:edit_menu", temp)
                await _render_edit_menu(update, user.id)
            else:
                await _set_state(user.id, "profile:aircraft", temp)
                await _render_aircraft_menu(update, user.id)
        elif state == "profile:search":
            await _render_search_results(update, user.id, text)
        elif state == "profile:rule_input":
            scope = temp.get("rule_scope")
            target = temp.get("rule_target") or ""
            field = temp.get("rule_input_field")
            own = _rule_object(temp, scope, target)
            if text.casefold() == "parent" and scope != "profile":
                own.pop(field, None)
                if field == "airlines":
                    own.pop("airline_mode", None)
            elif field in {"min_altitude_ft", "max_altitude_ft"}:
                if text.casefold() == "none":
                    own[field] = None
                else:
                    value = float(text.replace(",", ""))
                    if not 0 <= value <= 100000:
                        raise ValueError("altitude")
                    own[field] = value
            elif field == "radius_km":
                value = float(text)
                if not 1 <= value <= 150:
                    raise ValueError("radius")
                own[field] = value
            elif field == "airlines":
                codes, unresolved = normalize_operator_list(text)
                if unresolved:
                    await message.reply_text(
                        "I couldn't resolve: " + ", ".join(_esc(v) for v in unresolved) + "\nUse a known airline name, IATA code, or 3-letter ICAO operator code.",
                        parse_mode=ParseMode.HTML,
                    )
                    raise ApplicationHandlerStop
                own["airlines"] = codes
                if own.get("airline_mode") in {None, "all"}:
                    own["airline_mode"] = "whitelist"
            await _set_state(user.id, "profile:rule", temp)
            await _render_rule(update, user.id, scope, target)
        else:
            await message.reply_text("Use the buttons in the current profile menu or /cancel to exit.")
    except ValueError:
        if state == "profile:name":
            await message.reply_text("Profile names must be 1–40 characters.")
        elif state == "profile:radius":
            await message.reply_text("Send a radius from 1 to 150 km.")
        elif state == "profile:rule_input":
            await message.reply_text("That value is not valid for this setting. Try again, or use the Back button.")
        else:
            await message.reply_text("That value is not valid. Try again.")
    raise ApplicationHandlerStop


def register_profile_handlers(app: Application) -> None:
    """Install profile flows before legacy catch-all bot handlers."""
    group = -30
    app.add_handler(CommandHandler("profiles", cmd_profiles), group=group)
    app.add_handler(CommandHandler("preferences", cmd_preferences), group=group)
    app.add_handler(CallbackQueryHandler(profile_callback, pattern=r"^pf:"), group=group)
    app.add_handler(MessageHandler(filters.LOCATION, profile_location), group=group)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, profile_text), group=group)

    # MongoDB is connected before Telegram handlers are registered in the main
    # lifespan.  Migration is idempotent and outside the monitoring hot path.
    try:
        asyncio.get_running_loop().create_task(migrate_existing_users(), name="v43-profile-migration")
    except RuntimeError:
        pass
