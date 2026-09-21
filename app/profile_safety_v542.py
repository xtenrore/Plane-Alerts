"""Focused v5.4.2 profile-safety fixes from the general reliability audit.

This layer is installed explicitly by the Telegram profile registration path.
It does not alter prediction, CPA, ETA, qualification, cancellation, or alert
timing. It only tightens profile commit/validation behavior and installs the
related preset/photography correctness guards.
"""
from __future__ import annotations

from typing import Any

from app.aircraft.registry import CATEGORY_LABELS, primary_category

_INSTALLED = False
_ORIGINAL_VALIDATE = None


def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _merge_altitude(parent: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(parent)
    for key in ("min_altitude_ft", "max_altitude_ft"):
        if key in override:
            merged[key] = override[key]
    return merged


def _altitude_conflict(label: str, rule: dict[str, Any]) -> str | None:
    low = _number_or_none(rule.get("min_altitude_ft"))
    high = _number_or_none(rule.get("max_altitude_ft"))
    if low is None or high is None or low <= high:
        return None
    return (
        f"Altitude settings conflict for {label}: minimum {low:g} ft exceeds "
        f"effective maximum {high:g} ft. Adjust the Advanced altitude limits "
        "or use Parent for the override."
    )


def install_profile_safety_v542() -> None:
    """Install setup-commit and inherited-altitude validation fixes once."""
    global _INSTALLED, _ORIGINAL_VALIDATE
    if _INSTALLED:
        return

    from app.bot import profile_handlers as legacy

    _ORIGINAL_VALIDATE = legacy._validate_draft

    def validate_draft_v542(config: dict[str, Any]) -> str | None:
        assert _ORIGINAL_VALIDATE is not None
        error = _ORIGINAL_VALIDATE(config)
        if error:
            return error

        prefs = config.get("preferences") or {}
        rules = legacy.normalized_rule_config(prefs)
        profile_rule = dict(rules.get("profile") or {})

        error = _altitude_conflict("the profile", profile_rule)
        if error:
            return error

        category_rules = dict(rules.get("categories") or {})
        for category, rule in category_rules.items():
            effective = _merge_altitude(profile_rule, dict(rule or {}))
            label = CATEGORY_LABELS.get(str(category), str(category).replace("_", " ").title())
            error = _altitude_conflict(label, effective)
            if error:
                return error

        for code, rule in dict(rules.get("aircraft") or {}).items():
            aircraft_code = str(code).upper()
            category = primary_category(aircraft_code, None)
            effective = _merge_altitude(profile_rule, dict(category_rules.get(category) or {}))
            effective = _merge_altitude(effective, dict(rule or {}))
            error = _altitude_conflict(aircraft_code, effective)
            if error:
                return error
        return None

    async def save_edit_v542(update: Any, user_id: int) -> None:
        _, temp = await legacy._raw_state(user_id)
        profile_id = str(temp.get("editing_profile_id") or "")
        config = temp.get("profile_draft") or {}
        error = validate_draft_v542(config)
        if error:
            query = update.callback_query
            if query:
                await query.answer(error, show_alert=True)
            return

        await legacy.save_profile(user_id, profile_id, config=config)
        if temp.get("profile_flow") == "setup":
            await legacy.users_col().update_one(
                {"user_id": int(user_id)},
                {"$set": {"setup_complete": True}},
            )
            # A materialization refresh can race the setup-complete write. Mark
            # config dirty again after activation so the live worker promptly
            # refreshes the now-monitorable user rather than waiting for cadence.
            try:
                from app.storage_runtime_v48 import storage_runtime

                storage_runtime.invalidate_user_config(int(user_id))
            except Exception:
                pass
        await legacy._clear_state(user_id)
        await legacy._render_profile_detail(update, user_id, profile_id)

    legacy._validate_draft = validate_draft_v542
    legacy._save_edit = save_edit_v542

    from app.preset_session_v542 import install_preset_session_v542
    from app.photography_safety_v542 import install_photography_safety_v542

    install_preset_session_v542()
    install_photography_safety_v542()
    _INSTALLED = True
