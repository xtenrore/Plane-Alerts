"""Session binding for the v5.4 preset-create flow.

Old Telegram messages may remain tappable after the user starts a different
profile operation. Bind Save Profile to one persisted draft session so stale or
duplicate callbacks cannot create/activate an unintended profile.
"""
from __future__ import annotations

import secrets
from typing import Any

from telegram import InlineKeyboardMarkup
from telegram.ext import ApplicationHandlerStop

_INSTALLED = False


def install_preset_session_v542() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from app.bot import profile_experience_v54 as v54

    original_render_review = v54._render_review
    original_callback = v54.callback_v54

    async def render_review_bound(update: Any, user_id: int) -> None:
        state, temp = await v54.legacy._raw_state(user_id)
        if temp.get("v54_preset_flow") and temp.get("profile_flow") == "create":
            session_id = str(temp.get("v54_session_id") or "")
            if not session_id:
                session_id = secrets.token_hex(5)
                temp["v54_session_id"] = session_id
                await v54.legacy._set_state(user_id, state or "v54:review", temp)

            config = temp.get("profile_draft") or {}
            preset = v54.get_preset(str(temp.get("preset_id") or ""))
            loc = config.get("location") or {}
            prefs = config.get("preferences") or {}
            selection = v54.legacy.normalized_filter_config(prefs)
            radius = loc.get("radius_km")
            location_text = "Saved" if loc.get("latitude") is not None and loc.get("longitude") is not None else "Needed"
            radius_text = f"{float(radius):g} km" if radius is not None else "Needed"
            aircraft_text = "All aircraft" if selection["mode"] == "all" else f"{len(selection['selected_categories'])} groups"
            await v54.legacy._set_state(user_id, "v54:review", temp)
            text = (
                f"<b>{v54.legacy._esc(temp.get('draft_name') or 'New Profile')}</b>\n\n"
                f"Preset: {v54.legacy._esc(preset.name if preset else 'Custom')}\n"
                f"Location: {location_text}\n"
                f"Radius: {radius_text}\n"
                f"Aircraft: {v54.legacy._esc(aircraft_text)}\n\n"
                "Review anything you want, then save."
            )
            await v54.legacy._show(update, text, InlineKeyboardMarkup([
                [v54._b("Location", "ux54:loc"), v54._b("Radius", "ux54:radius")],
                [v54._b("Aircraft", "ux54:aircraft"), v54._b("Advanced", "ux54:advanced")],
                [v54._b("Change Preset", "ux54:presets"), v54._b("Save Profile", f"ux54:save:{session_id}")],
                [v54._b("Cancel", "ux54:home")],
            ]))
            return
        await original_render_review(update, user_id)

    async def callback_bound(update: Any, context: Any) -> None:
        query = getattr(update, "callback_query", None)
        user = getattr(update, "effective_user", None)
        data = str(getattr(query, "data", "") or "")
        if query is None or user is None or not data.startswith("ux54:save"):
            await original_callback(update, context)
            return

        await query.answer()
        state, temp = await v54.legacy._raw_state(user.id)
        parts = data.split(":")
        supplied = parts[2] if len(parts) == 3 else ""
        expected = str(temp.get("v54_session_id") or "")
        valid = bool(
            state == "v54:review"
            and temp.get("v54_preset_flow") is True
            and temp.get("profile_flow") == "create"
            and supplied
            and supplied == expected
        )
        if not valid:
            await v54.legacy._show(
                update,
                "This Save Profile button has expired. Your current profile draft was not changed.",
            )
            raise ApplicationHandlerStop

        config = temp.get("profile_draft") or {}
        error = v54.legacy._validate_draft(config)
        if error:
            await v54.legacy._show(
                update,
                f"<b>One thing needs attention</b>\n\n{v54.legacy._esc(error)}",
                v54._validation_keyboard(error),
            )
            raise ApplicationHandlerStop

        profile = await v54.create_profile(
            user.id,
            temp.get("draft_name") or "Profile",
            config,
            activate=True,
        )
        await v54.users_col().update_one(
            {"user_id": int(user.id)},
            {"$set": {"setup_complete": True}},
        )
        await v54.legacy._clear_state(user.id)
        await v54._render_detail(
            update,
            user.id,
            profile["profile_id"],
            "Profile saved and activated.",
        )
        raise ApplicationHandlerStop

    v54._render_review = render_review_bound
    v54.callback_v54 = callback_bound
    _INSTALLED = True
