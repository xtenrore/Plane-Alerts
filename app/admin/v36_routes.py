"""Plane Alerts administration APIs.

Adds server-side paginated user search, per-user scheduling/priority controls,
delegated admin access, and an audit trail without changing the legacy admin
monitoring endpoints.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.admin.auth import get_admin_actor, make_delegated_admin_token, require_root
from app.config import settings
from app.database import get_db, locations_col, preferences_col, users_col
from app.version import VERSION

router = APIRouter(tags=["admin-v3.6"])
STATIC_DIR = Path(__file__).parent / "static"


class UserControlsPatch(BaseModel):
    priority_enabled: bool | None = None
    delay_seconds: float | None = Field(default=None, ge=5.0, le=120.0)
    notifications_enabled: bool | None = None
    monitoring_enabled: bool | None = None
    radius_km: float | None = Field(default=None, ge=1.0, le=250.0)


class AdminAccessPatch(BaseModel):
    enabled: bool


def _controls(doc: dict[str, Any]) -> dict[str, Any]:
    raw = doc.get("admin_controls") or {}
    return {
        "priority_enabled": bool(raw.get("priority_enabled", False)),
        "delay_seconds": float(raw.get("delay_seconds", settings.poll_interval_seconds)),
        "notifications_enabled": bool(raw.get("notifications_enabled", True)),
    }


async def _audit(actor: dict[str, Any], action: str, target_user_id: int | None, details: dict[str, Any] | None = None) -> None:
    try:
        await get_db()["admin_audit"].insert_one({
            "actor_kind": actor.get("kind", "unknown"),
            "actor_user_id": actor.get("user_id"),
            "action": action,
            "target_user_id": target_user_id,
            "details": details or {},
            "created_at": datetime.now(timezone.utc),
        })
    except Exception:
        pass


@router.get("/v36.css", include_in_schema=False)
async def v36_css() -> FileResponse:
    return FileResponse(STATIC_DIR / "v36.css", media_type="text/css")


@router.get("/v36.js", include_in_schema=False)
async def v36_js() -> FileResponse:
    return FileResponse(STATIC_DIR / "v36.js", media_type="application/javascript")


@router.get("/api/v36/session")
async def admin_session(request: Request) -> dict[str, Any]:
    actor = await get_admin_actor(request)
    return {
        "root": bool(actor.get("root")),
        "kind": actor.get("kind"),
        "user_id": actor.get("user_id"),
        "can_manage_admins": bool(actor.get("root")),
        "version": VERSION,
        "api_schema_version": "3.6",
    }


@router.get("/api/v36/users")
async def admin_users_v36(
    request: Request,
    search: str = Query(default="", max_length=80),
    limit: int = Query(default=25, ge=5, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    await get_admin_actor(request)
    needle = search.strip()
    if needle.startswith("@"):
        needle = needle[1:]

    query: dict[str, Any] = {}
    if needle:
        escaped = re.escape(needle)
        clauses: list[dict[str, Any]] = [
            {"username": {"$regex": f"^{escaped}", "$options": "i"}},
            {"first_name": {"$regex": escaped, "$options": "i"}},
        ]
        if needle.isdigit():
            clauses.append({"user_id": int(needle)})
        query = {"$or": clauses}

    total = await users_col().count_documents(query)
    cursor = (
        users_col()
        .find(query)
        .sort([("admin_controls.priority_enabled", -1), ("last_active", -1), ("created_at", -1)])
        .skip(offset)
        .limit(limit)
    )
    docs = [doc async for doc in cursor]
    ids = [int(doc["user_id"]) for doc in docs]

    locations: dict[int, dict[str, Any]] = {}
    preferences: dict[int, dict[str, Any]] = {}
    if ids:
        async for loc in locations_col().find({"user_id": {"$in": ids}}):
            locations[int(loc["user_id"])] = loc
        async for pref in preferences_col().find({"user_id": {"$in": ids}}):
            preferences[int(pref["user_id"])] = pref

    items: list[dict[str, Any]] = []
    for doc in docs:
        uid = int(doc["user_id"])
        loc = locations.get(uid) or {}
        pref = preferences.get(uid) or {}
        items.append({
            "user_id": uid,
            "username": doc.get("username", ""),
            "first_name": doc.get("first_name", ""),
            "setup_complete": bool(doc.get("setup_complete", False)),
            "is_admin": bool(doc.get("is_admin", False)),
            "created_at": str(doc.get("created_at", "")),
            "last_active": str(doc.get("last_active", "")),
            "location": {
                "latitude": loc.get("latitude"),
                "longitude": loc.get("longitude"),
                "radius_km": float(loc.get("radius_km", settings.default_radius_km)),
            },
            "preferences": {
                "selected_categories": pref.get("selected_categories", []),
                "custom_aircraft": pref.get("custom_aircraft", []),
            },
            "controls": _controls(doc),
        })

    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(items) < total,
        "search": search,
    }


@router.patch("/api/v36/user/{user_id}/controls")
async def update_user_controls(
    user_id: int,
    patch: UserControlsPatch,
    request: Request,
) -> dict[str, Any]:
    actor = await get_admin_actor(request)
    user = await users_col().find_one({"user_id": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    values = patch.model_dump(exclude_none=True)
    user_set: dict[str, Any] = {}
    if "priority_enabled" in values:
        user_set["admin_controls.priority_enabled"] = bool(values["priority_enabled"])
    if "delay_seconds" in values:
        user_set["admin_controls.delay_seconds"] = float(values["delay_seconds"])
    if "notifications_enabled" in values:
        user_set["admin_controls.notifications_enabled"] = bool(values["notifications_enabled"])
    if "monitoring_enabled" in values:
        user_set["setup_complete"] = bool(values["monitoring_enabled"])

    if user_set:
        await users_col().update_one({"user_id": user_id}, {"$set": user_set})

    if "radius_km" in values:
        result = await locations_col().update_one(
            {"user_id": user_id},
            {"$set": {"radius_km": float(values["radius_km"])}}
        )
        if result.matched_count == 0:
            raise HTTPException(status_code=409, detail="User has no saved location yet")

    await _audit(actor, "update_user_controls", user_id, values)
    updated = await users_col().find_one({"user_id": user_id}) or user
    loc = await locations_col().find_one({"user_id": user_id}) or {}
    return {
        "user_id": user_id,
        "setup_complete": bool(updated.get("setup_complete", False)),
        "controls": _controls(updated),
        "radius_km": float(loc.get("radius_km", settings.default_radius_km)),
    }


@router.post("/api/v36/user/{user_id}/reset-controls")
async def reset_user_controls(user_id: int, request: Request) -> dict[str, Any]:
    actor = await get_admin_actor(request)
    user = await users_col().find_one({"user_id": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await users_col().update_one({"user_id": user_id}, {"$unset": {"admin_controls": ""}})
    await _audit(actor, "reset_user_controls", user_id)
    return {
        "user_id": user_id,
        "controls": {
            "priority_enabled": False,
            "delay_seconds": float(settings.poll_interval_seconds),
            "notifications_enabled": True,
        },
    }


@router.post("/api/v36/user/{user_id}/admin-access")
async def set_admin_access(
    user_id: int,
    patch: AdminAccessPatch,
    request: Request,
) -> dict[str, Any]:
    actor = await get_admin_actor(request)
    require_root(actor)
    user = await users_col().find_one({"user_id": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    await users_col().update_one(
        {"user_id": user_id},
        {"$set": {"is_admin": bool(patch.enabled), "admin_access_updated_at": datetime.now(timezone.utc)}},
    )
    await _audit(actor, "grant_admin" if patch.enabled else "revoke_admin", user_id)

    token = make_delegated_admin_token(user_id) if patch.enabled else None
    return {
        "user_id": user_id,
        "username": user.get("username", ""),
        "enabled": bool(patch.enabled),
        "token": token,
    }


@router.get("/api/v36/admin-stats")
async def admin_stats(request: Request) -> dict[str, Any]:
    await get_admin_actor(request)
    total = await users_col().count_documents({})
    priority = await users_col().count_documents({"admin_controls.priority_enabled": True})
    admins = await users_col().count_documents({"is_admin": True})
    paused = await users_col().count_documents({"admin_controls.notifications_enabled": False})
    custom_delay = await users_col().count_documents({
        "admin_controls.delay_seconds": {"$exists": True, "$ne": float(settings.poll_interval_seconds)}
    })
    return {
        "users": total,
        "priority_users": priority,
        "delegated_admins": admins,
        "notifications_paused": paused,
        "custom_delay_users": custom_delay,
        "default_delay_seconds": float(settings.poll_interval_seconds),
    }


@router.get("/api/v36/audit")
async def admin_audit(
    request: Request,
    limit: int = Query(default=25, ge=1, le=100),
) -> list[dict[str, Any]]:
    await get_admin_actor(request)
    results: list[dict[str, Any]] = []
    cursor = get_db()["admin_audit"].find({}).sort("created_at", -1).limit(limit)
    async for doc in cursor:
        results.append({
            "actor_kind": doc.get("actor_kind"),
            "actor_user_id": doc.get("actor_user_id"),
            "action": doc.get("action"),
            "target_user_id": doc.get("target_user_id"),
            "details": doc.get("details", {}),
            "created_at": str(doc.get("created_at", "")),
        })
    return results
