"""Admin dashboard API routes and static asset serving.

All API endpoints are available at both ``/admin/api/*`` and ``/admin/*``.
The admin HTML dashboard is served directly at ``/admin``.
"""

from __future__ import annotations

import logging
from pathlib import Path
import platform
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.aircraft.ai_judge import ai_judge
from app.aircraft.api_keys import opensky_key_manager
from app.config import settings
from app.database import (
    ai_usage_col,
    feedback_col,
    locations_col,
    notification_history_col,
    preferences_col,
    provider_learning_col,
    system_status_col,
    users_col,
)
from app.worker.monitor import get_cycle_stats, get_provider_manager

logger = logging.getLogger(__name__)

admin_router = APIRouter(tags=["admin"])
router = admin_router

STATIC_DIR = Path(__file__).parent / "static"

_security = HTTPBasic(auto_error=False)
_start_time = time.time()


async def _check_auth(
    credentials: HTTPBasicCredentials | None = Depends(_security),
) -> None:
    """Require configured HTTP Basic authentication for legacy admin routes."""
    configured = settings.admin_password.strip()
    if not configured or credentials is None or credentials.password != configured:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )


async def _get_effective_cycle_stats() -> dict[str, Any]:
    """Return cycle stats from memory or database (for cross-process monitoring)."""
    stats = get_cycle_stats()
    if stats.get("total_cycles", 0) == 0:
        try:
            doc = await system_status_col().find_one({"_id": "monitor_worker"})
            if doc:
                last_time = doc.get("last_cycle_time", 0.0)
                duration_ms = doc.get("last_cycle_duration_ms", 0.0)
                total = doc.get("total_cycles", 0)
                is_stale = (time.time() - last_time) > (settings.poll_interval_seconds * 4)
                return {
                    "last_cycle_time": last_time,
                    "last_cycle_duration_ms": duration_ms,
                    "total_cycles": total,
                    "worker_status": "running" if not is_stale else "stale",
                }
        except Exception:
            pass
    return stats


@admin_router.api_route("", methods=["GET", "HEAD"], response_class=FileResponse, include_in_schema=False)
@admin_router.api_route("/", methods=["GET", "HEAD"], response_class=FileResponse, include_in_schema=False)
async def serve_admin_index() -> FileResponse:
    html_file = STATIC_DIR / "admin.html"
    if not html_file.exists():
        raise HTTPException(status_code=404, detail="Admin dashboard HTML not found")
    return FileResponse(html_file, media_type="text/html")


@admin_router.api_route("/admin.css", methods=["GET", "HEAD"], response_class=FileResponse, include_in_schema=False)
async def serve_admin_css() -> FileResponse:
    css_file = STATIC_DIR / "admin.css"
    if not css_file.exists():
        raise HTTPException(status_code=404, detail="admin.css not found")
    return FileResponse(css_file, media_type="text/css")


@admin_router.api_route("/admin.js", methods=["GET", "HEAD"], response_class=FileResponse, include_in_schema=False)
async def serve_admin_js() -> FileResponse:
    js_file = STATIC_DIR / "admin.js"
    if not js_file.exists():
        raise HTTPException(status_code=404, detail="admin.js not found")
    return FileResponse(js_file, media_type="application/javascript")


@admin_router.get("/overview")
@admin_router.get("/api/overview")
async def admin_overview(_: None = Depends(_check_auth)) -> dict[str, Any]:
    total_users = 0
    active_users = 0
    total_notifications = 0

    try:
        total_users = await users_col().count_documents({})
        active_users = await users_col().count_documents({"setup_complete": True})
        total_notifications = await notification_history_col().count_documents({})
    except Exception as exc:
        logger.debug("Could not fetch overview counts from DB (DB may be offline): %s", exc)

    cycle_stats = await _get_effective_cycle_stats()

    try:
        import psutil  # type: ignore[import-untyped]
        process = psutil.Process()
        mem_mb = process.memory_info().rss / (1024 * 1024)
    except Exception:
        mem_mb = -1

    return {
        "total_users": total_users,
        "active_users": active_users,
        "total_notifications": total_notifications,
        "uptime_seconds": round(time.time() - _start_time, 1),
        "memory_mb": round(mem_mb, 1),
        "python_version": platform.python_version(),
        "cycle_stats": cycle_stats,
        "ai_status": ai_judge.get_usage_report(),
    }


@admin_router.get("/users")
@admin_router.get("/api/users")
async def admin_users(_: None = Depends(_check_auth)) -> list[dict[str, Any]]:
    users = []
    try:
        cursor = users_col().find({}).sort("created_at", -1)
        async for doc in cursor:
            user_id = doc["user_id"]
            loc = await locations_col().find_one({"user_id": user_id})
            prefs = await preferences_col().find_one({"user_id": user_id})
            users.append({
                "user_id": user_id,
                "username": doc.get("username", ""),
                "first_name": doc.get("first_name", ""),
                "setup_complete": doc.get("setup_complete", False),
                "terms_accepted": doc.get("terms_accepted", False),
                "created_at": str(doc.get("created_at", "")),
                "last_active": str(doc.get("last_active", "")),
                "location": {
                    "latitude": loc.get("latitude") if loc else None,
                    "longitude": loc.get("longitude") if loc else None,
                    "geohash": loc.get("geohash", "") if loc else "",
                    "radius_km": loc.get("radius_km", settings.default_radius_km) if loc else settings.default_radius_km,
                },
                "preferences": {
                    "selected_categories": prefs.get("selected_categories", []) if prefs else [],
                    "custom_aircraft": prefs.get("custom_aircraft", []) if prefs else [],
                },
            })
    except Exception as exc:
        logger.debug("Could not fetch users list from DB: %s", exc)
    return users


@admin_router.post("/user/{user_id}/toggle")
@admin_router.post("/api/user/{user_id}/toggle")
async def admin_toggle_user(user_id: int, _: None = Depends(_check_auth)) -> dict[str, Any]:
    doc = await users_col().find_one({"user_id": user_id})
    if not doc:
        raise HTTPException(status_code=404, detail="User not found")
    new_status = not doc.get("setup_complete", False)
    await users_col().update_one({"user_id": user_id}, {"$set": {"setup_complete": new_status}})
    return {"user_id": user_id, "setup_complete": new_status}


@admin_router.get("/providers")
@admin_router.get("/api/providers")
async def admin_providers(_: None = Depends(_check_auth)) -> dict[str, Any]:
    pm = get_provider_manager()
    return {"providers": pm.get_all_provider_status(), "ai_usage": ai_judge.get_usage_report()}


@admin_router.get("/keys")
@admin_router.get("/api/keys")
async def admin_keys(_: None = Depends(_check_auth)) -> dict[str, Any]:
    status = opensky_key_manager.get_status()
    return {
        "total_keys": status.total_keys,
        "active_key_index": status.active_key_index,
        "all_exhausted": status.all_exhausted,
        "keys": status.keys,
    }


@admin_router.get("/notifications")
@admin_router.get("/api/notifications")
async def admin_notifications(
    limit: int = Query(default=50, le=200),
    _: None = Depends(_check_auth),
) -> list[dict[str, Any]]:
    results = []
    try:
        cursor = notification_history_col().find({}).sort("notified_at", -1).limit(limit)
        async for doc in cursor:
            results.append({
                "user_id": doc.get("user_id"),
                "aircraft_icao24": doc.get("aircraft_icao24", ""),
                "aircraft_type": doc.get("aircraft_type", ""),
                "distance_km": doc.get("distance_km"),
                "notified_at": str(doc.get("notified_at", "")),
                "cooldown_until": str(doc.get("cooldown_until", "")),
            })
    except Exception as exc:
        logger.debug("Could not fetch notifications from DB: %s", exc)
    return results


@admin_router.get("/system")
@admin_router.get("/api/system")
async def admin_system(_: None = Depends(_check_auth)) -> dict[str, Any]:
    cycle_stats = await _get_effective_cycle_stats()
    try:
        db_stats = {
            "users": await users_col().count_documents({}),
            "locations": await locations_col().count_documents({}),
            "preferences": await preferences_col().count_documents({}),
            "notifications": await notification_history_col().count_documents({}),
            "learning_records": await provider_learning_col().count_documents({}),
            "feedback_records": await feedback_col().count_documents({}),
        }
    except Exception:
        db_stats = {"error": "Could not fetch DB stats"}

    return {
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "uptime_seconds": round(time.time() - _start_time, 1),
        "worker": cycle_stats,
        "database": db_stats,
        "ai": ai_judge.get_usage_report(),
        "config": {
            "poll_interval_seconds": settings.poll_interval_seconds,
            "default_radius_km": settings.default_radius_km,
            "cooldown_minutes": settings.cooldown_minutes,
        },
    }
