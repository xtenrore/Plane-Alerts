"""Persistent multi-profile alert configuration for Plane Alerts v4.3.

The active profile is materialized into the existing ``locations`` and
``preferences`` collections. This preserves backward compatibility and keeps
profile collection reads out of the five-second monitoring hot path.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import logging
import re
import uuid
from typing import Any

from app.aircraft.filtering import clear_filter_cache, normalized_filter_config, normalized_rule_config
from app.config import settings
from app.database import locations_col, preferences_col, profiles_col, users_col

logger = logging.getLogger(__name__)
_PROFILE_ID_RE = re.compile(r"^[0-9a-f]{10}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_profile_id() -> str:
    return uuid.uuid4().hex[:10]


def valid_profile_id(value: str | None) -> bool:
    return bool(_PROFILE_ID_RE.fullmatch(str(value or "")))


def clean_name(value: str) -> str:
    name = " ".join(str(value or "").split()).strip()
    if not 1 <= len(name) <= 40:
        raise ValueError("Profile names must be 1–40 characters.")
    return name


def _strip_mongo(document: dict[str, Any] | None) -> dict[str, Any]:
    if not document:
        return {}
    result = deepcopy(document)
    result.pop("_id", None)
    result.pop("user_id", None)
    result.pop("updated_at", None)
    return result


def config_from_legacy(preferences: dict[str, Any] | None, location: dict[str, Any] | None) -> dict[str, Any]:
    prefs = _strip_mongo(preferences)
    loc = _strip_mongo(location)
    prefs["aircraft_filter"] = normalized_filter_config(prefs)
    prefs["filter_rules"] = normalized_rule_config(prefs)
    if loc and "radius_km" not in loc:
        loc["radius_km"] = settings.default_radius_km
    return {"location": loc, "preferences": prefs}


def blank_config_from(base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create an independent profile draft while preserving unrelated settings."""
    config = deepcopy(base or {})
    prefs = dict(config.get("preferences") or {})
    prefs["aircraft_filter"] = {
        "mode": "selected",
        "selected_categories": [],
        "selected_types": [],
        "excluded_types": [],
    }
    prefs["filter_rules"] = {
        "profile": {},
        "categories": {},
        "aircraft": {},
    }
    config["preferences"] = prefs
    config["location"] = {}
    return config


async def list_profiles(user_id: int) -> list[dict[str, Any]]:
    cursor = profiles_col().find({"user_id": int(user_id)}).sort("created_at", 1)
    return [doc async for doc in cursor]


async def get_profile(user_id: int, profile_id: str) -> dict[str, Any] | None:
    if not valid_profile_id(profile_id):
        return None
    return await profiles_col().find_one({"user_id": int(user_id), "profile_id": profile_id})


async def get_active_profile(user_id: int, *, ensure: bool = True) -> dict[str, Any] | None:
    user = await users_col().find_one({"user_id": int(user_id)}, {"active_profile_id": 1})
    profile_id = str((user or {}).get("active_profile_id") or "")
    profile = await get_profile(user_id, profile_id) if profile_id else None
    if profile or not ensure:
        return profile
    return await ensure_default_profile(user_id)


async def ensure_default_profile(user_id: int) -> dict[str, Any]:
    existing = await list_profiles(user_id)
    if existing:
        user = await users_col().find_one({"user_id": int(user_id)}, {"active_profile_id": 1})
        active_id = str((user or {}).get("active_profile_id") or "")
        active = next((p for p in existing if p.get("profile_id") == active_id), None)
        if active:
            return active
        fallback = existing[0]
        await activate_profile(user_id, fallback["profile_id"])
        return fallback

    prefs = await preferences_col().find_one({"user_id": int(user_id)})
    loc = await locations_col().find_one({"user_id": int(user_id)})
    profile_id = new_profile_id()
    now = _now()
    profile = {
        "user_id": int(user_id),
        "profile_id": profile_id,
        "name": "Default",
        "config": config_from_legacy(prefs, loc),
        "created_at": now,
        "updated_at": now,
        "schema_version": 1,
    }
    await profiles_col().insert_one(profile)
    await users_col().update_one(
        {"user_id": int(user_id)},
        {"$set": {"active_profile_id": profile_id}},
        upsert=True,
    )
    return profile


async def migrate_existing_users() -> int:
    """Idempotently give every configured legacy user an initial profile."""
    migrated = 0
    cursor = users_col().find({"setup_complete": True}, {"user_id": 1, "active_profile_id": 1})
    async for user in cursor:
        uid = int(user["user_id"])
        before = await profiles_col().count_documents({"user_id": uid}, limit=1)
        await ensure_default_profile(uid)
        if before == 0:
            migrated += 1
    return migrated


async def create_profile(user_id: int, name: str, config: dict[str, Any], *, activate: bool = False) -> dict[str, Any]:
    name = clean_name(name)
    profile_id = new_profile_id()
    now = _now()
    profile = {
        "user_id": int(user_id),
        "profile_id": profile_id,
        "name": name,
        "config": deepcopy(config),
        "created_at": now,
        "updated_at": now,
        "schema_version": 1,
    }
    await profiles_col().insert_one(profile)
    if activate:
        await activate_profile(user_id, profile_id)
    return profile


async def materialize_profile(profile: dict[str, Any]) -> None:
    """Write one coherent active-config generation into the legacy collections."""
    uid = int(profile["user_id"])
    config = deepcopy(profile.get("config") or {})
    prefs = dict(config.get("preferences") or {})
    loc = dict(config.get("location") or {})
    revision = str(uuid.uuid4())
    prefs["config_revision"] = revision
    loc["config_revision"] = revision
    prefs["user_id"] = uid
    prefs["active_profile_id"] = profile["profile_id"]
    prefs["updated_at"] = _now()
    await preferences_col().replace_one({"user_id": uid}, prefs, upsert=True)

    if loc.get("latitude") is None or loc.get("longitude") is None:
        await locations_col().delete_one({"user_id": uid})
    else:
        loc["user_id"] = uid
        loc.setdefault("radius_km", settings.default_radius_km)
        loc["updated_at"] = _now()
        await locations_col().replace_one({"user_id": uid}, loc, upsert=True)
    clear_filter_cache()


async def _restore_materialized_profile(profile: dict[str, Any] | None) -> None:
    if not profile:
        return
    try:
        await materialize_profile(profile)
    except Exception:
        logger.exception(
            "profile_materialization_rollback_failed user=%s profile=%s",
            profile.get("user_id"),
            profile.get("profile_id"),
        )


async def save_profile(user_id: int, profile_id: str, *, name: str | None = None, config: dict[str, Any] | None = None) -> dict[str, Any]:
    profile = await get_profile(user_id, profile_id)
    if not profile:
        raise KeyError("profile not found")

    update: dict[str, Any] = {"updated_at": _now()}
    if name is not None:
        update["name"] = clean_name(name)
    if config is not None:
        update["config"] = deepcopy(config)

    candidate = deepcopy(profile)
    candidate.update(deepcopy(update))
    user = await users_col().find_one({"user_id": int(user_id)}, {"active_profile_id": 1})
    is_active = str((user or {}).get("active_profile_id") or "") == profile_id

    # Publish the candidate legacy generation before committing the authoritative
    # active profile. If either legacy write tears, immediately attempt to
    # republish the previous committed profile; the live worker independently
    # retains its coherent per-user LKG until a verified refresh succeeds.
    if is_active:
        try:
            await materialize_profile(candidate)
        except Exception:
            await _restore_materialized_profile(profile)
            raise

    try:
        await profiles_col().update_one(
            {"user_id": int(user_id), "profile_id": profile_id},
            {"$set": update},
        )
    except Exception:
        if is_active:
            await _restore_materialized_profile(profile)
        raise

    refreshed = await get_profile(user_id, profile_id)
    if refreshed is None:
        if is_active:
            await _restore_materialized_profile(profile)
        raise RuntimeError("profile save could not be verified")
    return refreshed


async def activate_profile(user_id: int, profile_id: str) -> dict[str, Any]:
    profile = await get_profile(user_id, profile_id)
    if not profile:
        raise KeyError("profile not found")

    user = await users_col().find_one({"user_id": int(user_id)}, {"active_profile_id": 1})
    previous_id = str((user or {}).get("active_profile_id") or "")
    previous = await get_profile(user_id, previous_id) if previous_id and previous_id != profile_id else None

    try:
        await materialize_profile(profile)
    except Exception:
        await _restore_materialized_profile(previous)
        raise
    try:
        await users_col().update_one(
            {"user_id": int(user_id)},
            {"$set": {"active_profile_id": profile_id, "last_active": _now()}},
            upsert=True,
        )
    except Exception:
        await _restore_materialized_profile(previous)
        raise
    return profile


async def rename_profile(user_id: int, profile_id: str, name: str) -> dict[str, Any]:
    return await save_profile(user_id, profile_id, name=name)


async def duplicate_profile(user_id: int, profile_id: str) -> dict[str, Any]:
    source = await get_profile(user_id, profile_id)
    if not source:
        raise KeyError("profile not found")
    existing_names = {str(p.get("name") or "") for p in await list_profiles(user_id)}
    base = f"{source.get('name') or 'Profile'} Copy"
    name = base
    suffix = 2
    while name in existing_names:
        name = f"{base} {suffix}"
        suffix += 1
    return await create_profile(user_id, name[:40], deepcopy(source.get("config") or {}), activate=False)


async def delete_profile(user_id: int, profile_id: str) -> dict[str, Any]:
    profiles = await list_profiles(user_id)
    target = next((p for p in profiles if p.get("profile_id") == profile_id), None)
    if not target:
        raise KeyError("profile not found")
    if len(profiles) <= 1:
        raise ValueError("At least one profile must remain.")

    user = await users_col().find_one({"user_id": int(user_id)}, {"active_profile_id": 1})
    active_id = str((user or {}).get("active_profile_id") or "")
    if active_id == profile_id:
        fallback = next(p for p in profiles if p.get("profile_id") != profile_id)
        await activate_profile(user_id, fallback["profile_id"])
        await profiles_col().delete_one({"user_id": int(user_id), "profile_id": profile_id})
        return fallback

    await profiles_col().delete_one({"user_id": int(user_id), "profile_id": profile_id})
    active = await get_active_profile(user_id)
    assert active is not None
    return active


async def sync_active_profile_from_legacy(user_id: int) -> dict[str, Any]:
    """Keep legacy commands/setup changes attached to the current profile."""
    active = await ensure_default_profile(user_id)
    prefs = await preferences_col().find_one({"user_id": int(user_id)})
    loc = await locations_col().find_one({"user_id": int(user_id)})
    config = config_from_legacy(prefs, loc)
    return await save_profile(user_id, active["profile_id"], config=config)


def profile_summary(profile: dict[str, Any]) -> str:
    config = profile.get("config") or {}
    loc = config.get("location") or {}
    prefs = config.get("preferences") or {}
    selection = normalized_filter_config(prefs)
    rules = normalized_rule_config(prefs)
    radius = loc.get("radius_km")
    if selection["mode"] == "all":
        aircraft = "All Aircraft"
    else:
        categories = selection["selected_categories"]
        types = selection["selected_types"]
        if categories:
            aircraft = ", ".join(categories[:2])
            if len(categories) > 2:
                aircraft += f" +{len(categories) - 2}"
        elif types:
            aircraft = " / ".join(types[:3])
            if len(types) > 3:
                aircraft += f" +{len(types) - 3}"
        else:
            aircraft = "No aircraft selected"
    overrides = len(rules["aircraft"])
    parts = []
    if radius is not None:
        parts.append(f"{float(radius):g} km")
    parts.append(aircraft)
    if overrides:
        parts.append(f"{overrides} aircraft override{'s' if overrides != 1 else ''}")
    return " · ".join(parts)
