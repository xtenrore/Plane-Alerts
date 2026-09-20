"""Plane Alerts v4.4 reliability runtime, profiles, Prediction Lab, bot, and web server."""
from __future__ import annotations

import asyncio
import logging
import hashlib
import hmac
from collections import OrderedDict
import os
import platform
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from telegram import BotCommand, Update
from telegram.ext import Application

from app.admin.auth import DelegatedAdminMiddleware
from app.admin.routes import router as admin_router
from app.admin.v36_routes import router as admin_v36_router
from app.agy_console import register_agy_console_handlers
from app.aircraft.api_keys import opensky_key_manager
from app.aircraft.providers import close_http_client
from app.bot.handlers import register_handlers
from app.bot.next60 import build_next60_docs, register_next60_handlers
from app.bot.next60_web import NEXT60_HTML, serialize_next60, validate_telegram_init_data
from app.bot.profile_handlers import register_profile_handlers
from app.bot.profile_legacy import register_profile_legacy_handlers
from app.config import settings
from app.version import VERSION, COMMIT
from app.worker import timing
from app.worker.notification_telemetry import close as close_notification_telemetry
from app.worker.notification_telemetry import snapshot as notification_telemetry_snapshot
from app.worker.optional_work import enrichment
from app.database import close_db, connect_db, get_db, system_status_col, users_col
from app.logging_security import configure_secure_logging
from app.photography.telegram import register_photography_handlers
from app.sentinel_network import EUROPE_SENTINELS, run_sentinel_network
from app.worker.monitor import get_cycle_stats, init_services
from app.worker.v36 import run_monitor_cycle_v36 as run_monitor_cycle

logger = logging.getLogger(__name__)
telegram_app: Application | None = None
_server_start_time: float = time.time()
_recent_updates: OrderedDict[int, float] = OrderedDict()


def webhook_secret() -> str:
    explicit = settings.webhook_secret.strip()
    token = settings.telegram_bot_token.strip()
    return explicit or (hmac.new(token.encode(), b"plane-alerts-telegram-webhook-v1", hashlib.sha256).hexdigest() if token else "")


async def _monitor_loop() -> None:
    """Run the ADS-B monitor in-process to fit small container memory limits."""
    logger.info(
        "Integrated ADS-B worker enabled: base interval=%ds, shared polling + Plane Alerts v%s active",
        settings.poll_interval_seconds,
        VERSION,
    )
    first_cycle_confirmed = False
    while True:
        cycle_started = timing.start_cycle()
        try:
            get_db()
            await run_monitor_cycle()
            if not first_cycle_confirmed:
                stats = get_cycle_stats()
                if int(stats.get("total_cycles", 0) or 0) > 0:
                    logger.info(
                        "Integrated ADS-B worker first cycle completed: total_cycles=%s duration_ms=%s",
                        stats.get("total_cycles"),
                        stats.get("last_cycle_duration_ms"),
                    )
                    first_cycle_confirmed = True
        except asyncio.CancelledError:
            raise
        except RuntimeError:
            logger.debug("Integrated worker waiting for MongoDB connection")
        except Exception:
            logger.exception("Integrated ADS-B monitor iteration failed")

        timing.finish_cycle(cycle_started)
        if get_cycle_stats().get("total_cycles", 0) % 12 == 0:
            logger.info("monitor_timing %s", timing.snapshot())
        elapsed = time.monotonic() - cycle_started
        delay = max(0.25, float(settings.poll_interval_seconds) - elapsed)
        await asyncio.sleep(delay)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global telegram_app

    configure_secure_logging()
    logger.info("Initializing Plane Alerts v%s reliability runtime...", VERSION)

    db_reconnect_task: asyncio.Task | None = None
    monitor_task: asyncio.Task | None = None
    sentinel_task: asyncio.Task | None = None

    async def _reconnect_db_loop() -> None:
        while True:
            try:
                await connect_db(max_retries=1, retry_delay=1.0, timeout_ms=10000)
                logger.info("MongoDB background connection established.")
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("MongoDB not ready yet (%s). Retrying in 5s...", type(exc).__name__)
                await asyncio.sleep(5)

    try:
        await connect_db(max_retries=2, retry_delay=1.0, timeout_ms=10000)
    except Exception as exc:
        logger.warning("MongoDB not reachable immediately: %s. Launching reconnect loop...", type(exc).__name__)
        db_reconnect_task = asyncio.create_task(_reconnect_db_loop(), name="mongo-reconnect")

    key_count = opensky_key_manager.load_keys()
    logger.info("OpenSky key manager: %d key(s) available.", key_count)
    await init_services()

    monitor_task = asyncio.create_task(_monitor_loop(), name="aircraft-monitor")
    sentinel_task = asyncio.create_task(run_sentinel_network(), name="prediction-lab-europe-sentinels")
    logger.info("Prediction Lab Europe sentinel network enabled: regions=%d shadow_only=true", len(EUROPE_SENTINELS))

    bot_token = settings.telegram_bot_token.strip()
    if bot_token and bot_token != "your_bot_token_from_botfather":
        try:
            telegram_app = Application.builder().token(bot_token).update_queue(asyncio.Queue(maxsize=256)).concurrent_updates(False).build()
            # Private owner-only AGY bridge runs in an earlier handler group so
            # OAuth codes and interactive AGY input are never mistaken for
            # ordinary Plane Alerts setup text.
            register_agy_console_handlers(telegram_app)
            # Legacy setup/location entry points are routed into profile-aware
            # flows before both the profile state router and old catch-alls.
            register_profile_legacy_handlers(telegram_app)
            # Profile handlers use a negative group so profile callbacks and
            # profile text/location states are handled before legacy catch-alls.
            register_profile_handlers(telegram_app)
            register_handlers(telegram_app)
            register_next60_handlers(telegram_app)
            register_photography_handlers(telegram_app)
            await telegram_app.initialize()
            await telegram_app.start()
            try:
                await telegram_app.bot.set_my_commands(
                    [
                        BotCommand("start", "Set up aircraft alerts"),
                        BotCommand("profiles", "Manage alert profiles"),
                        BotCommand("status", "Show monitoring status"),
                        BotCommand("next60", "Planes expected in the next 60 minutes"),
                        BotCommand("forecast", "Alias for the Next 60 Minutes forecast"),
                        BotCommand("location", "Set monitoring / shooting location"),
                        BotCommand("preferences", "Choose aircraft and advanced filters"),
                        BotCommand("camera", "Set your camera body"),
                        BotCommand("lens", "Set the aircraft lens"),
                        BotCommand("photo", "Get live best-shot camera settings"),
                        BotCommand("conditions", "Show weather / sun / haze conditions"),
                        BotCommand("spotting", "Open Spotting Mode"),
                        BotCommand("agy", "Open private Antigravity console"),
                        BotCommand("help", "Show all commands"),
                    ]
                )
            except Exception as exc:
                logger.warning("Could not update Telegram command menu: %s", exc)

            webhook_url = settings.webhook_url.strip()
            if webhook_url:
                full_webhook_url = f"{webhook_url.rstrip('/')}/webhook"
                logger.info("Registering Telegram webhook: %s", full_webhook_url)
                await telegram_app.bot.set_webhook(
                    url=full_webhook_url,
                    secret_token=webhook_secret(),
                    drop_pending_updates=False,
                )
            else:
                logger.info("Starting Telegram long polling.")
                await telegram_app.bot.delete_webhook(drop_pending_updates=False)
                if telegram_app.updater:
                    await telegram_app.updater.start_polling(drop_pending_updates=False)
        except Exception as exc:
            logger.exception("Failed to initialize Telegram bot: %s", exc)
    else:
        logger.warning("TELEGRAM_BOT_TOKEN is not configured; Telegram runtime disabled.")

    yield

    logger.info("Shutting down Plane Alerts v%s...", VERSION)
    if telegram_app:
        try:
            if telegram_app.updater and telegram_app.updater.running:
                await telegram_app.updater.stop()
            await telegram_app.stop()
            await telegram_app.shutdown()
        except Exception as exc:
            logger.warning("Error stopping Telegram app: %s", exc)

    for task in (monitor_task, sentinel_task, db_reconnect_task):
        if task and not task.done():
            task.cancel()
    for task in (monitor_task, sentinel_task, db_reconnect_task):
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Background task failed during shutdown")

    from app.prediction_lab_audit import audit_work
    await audit_work.close()
    await close_notification_telemetry()
    await enrichment.close()
    await close_http_client()
    await close_db()


app = FastAPI(
    title="Plane Alerts",
    description="Deterministic real-time ADS-B spotting intelligence with profiles, reliability guards and inherited aircraft filters",
    version=VERSION,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(DelegatedAdminMiddleware)
app.include_router(admin_router, prefix="/admin", tags=["admin"])
app.include_router(admin_v36_router, prefix="/admin", tags=["admin-v3.6"])


@app.get("/next60-ui", response_class=HTMLResponse)
async def next60_ui() -> HTMLResponse:
    return HTMLResponse(content=NEXT60_HTML, headers={"Cache-Control": "no-store"})


@app.post("/api/next60")
async def next60_api(request: Request) -> Response:
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            {"detail": "invalid request"},
            status_code=status.HTTP_400_BAD_REQUEST,
            headers={"Cache-Control": "no-store"},
        )

    init_data = str((payload or {}).get("init_data") or "") if isinstance(payload, dict) else ""
    user_id = validate_telegram_init_data(init_data, settings.telegram_bot_token)
    if user_id is None:
        return JSONResponse(
            {"detail": "unauthorized"},
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"Cache-Control": "no-store"},
        )

    user_doc = await users_col().find_one({"user_id": user_id}, {"setup_complete": 1})
    if not user_doc or not user_doc.get("setup_complete"):
        return JSONResponse(
            {"detail": "setup required"},
            status_code=status.HTTP_403_FORBIDDEN,
            headers={"Cache-Control": "no-store"},
        )

    now = datetime.now(timezone.utc)
    docs = await build_next60_docs(user_id, now)
    return JSONResponse(serialize_next60(now, docs), headers={"Cache-Control": "no-store"})


@app.api_route("/", methods=["GET", "HEAD"], response_class=Response)
async def root_health_check() -> Response:
    return Response(content="OK", media_type="text/plain", status_code=status.HTTP_200_OK)


@app.api_route("/health", methods=["GET", "HEAD"])
async def health_check() -> dict[str, Any]:
    db_ok = False
    try:
        db = get_db()
        await asyncio.wait_for(db.command("ping"), timeout=1.0)
        db_ok = True
    except Exception:
        pass

    bot_status = "unconfigured"
    if settings.telegram_bot_token and settings.telegram_bot_token != "your_bot_token_from_botfather":
        if telegram_app and telegram_app.running:
            bot_status = "webhook" if settings.webhook_url.strip() else "polling"
        else:
            bot_status = "stopped"

    worker_info: dict[str, Any] = {"status": "unknown", "version": VERSION, "commit": COMMIT}
    sentinel_info: dict[str, Any] = {"enabled": True, "regions": len(EUROPE_SENTINELS), "mode": "shadow-only"}
    try:
        doc = await asyncio.wait_for(system_status_col().find_one({"_id": "monitor_worker"}), timeout=1.0)
        if doc:
            last_time = doc.get("last_cycle_time", 0.0)
            is_stale = (time.time() - last_time) > (settings.poll_interval_seconds * 4)
            worker_info = {
                "status": "active" if not is_stale else "stale",
                "version": doc.get("plane_version") or VERSION,
                "commit": doc.get("plane_commit") or COMMIT,
                "total_cycles": doc.get("total_cycles", 0),
                "last_cycle_duration_ms": doc.get("last_cycle_duration_ms", 0.0),
                "seconds_since_last_cycle": round(time.time() - last_time, 1),
                "polling_mode": doc.get("polling_mode", "adaptive-shared-regions-priority"),
                "shared_regions_last_cycle": doc.get("shared_regions_last_cycle", 0),
                "provider_queries_last_cycle": doc.get("provider_queries_last_cycle", 0),
                "shared_snapshot_cache_hits_last_cycle": doc.get("shared_snapshot_cache_hits_last_cycle", 0),
                "priority_users_last_cycle": doc.get("priority_users_last_cycle", 0),
                "deferred_users_last_cycle": doc.get("deferred_users_last_cycle", 0),
                "notifications_paused_last_cycle": doc.get("notifications_paused_last_cycle", 0),
            }
        else:
            stats = get_cycle_stats()
            if stats.get("total_cycles", 0) > 0:
                worker_info = {
                    "status": "active (in-process)",
                    "version": VERSION,
                    "commit": COMMIT,
                    "total_cycles": stats.get("total_cycles", 0),
                }
        sentinel_doc = await asyncio.wait_for(system_status_col().find_one({"_id": "prediction_lab_sentinels"}), timeout=1.0)
        if sentinel_doc:
            sentinel_info.update({
                "last_region": sentinel_doc.get("last_region"),
                "last_provider": sentinel_doc.get("last_provider"),
                "last_aircraft": sentinel_doc.get("last_aircraft", 0),
                "last_points_stored": sentinel_doc.get("last_points_stored", 0),
                "poll_interval_seconds": sentinel_doc.get("poll_interval_seconds", 30),
            })
    except Exception:
        pass

    return {
        "status": "healthy" if db_ok and bot_status != "stopped" and worker_info.get("status", "").startswith("active") and get_cycle_stats().get("total_cycles", 0) > 0 else "degraded",
        "version": VERSION,
        "commit": COMMIT,
        "release_match": worker_info.get("version") == VERSION and worker_info.get("commit") == COMMIT,
        "timing": timing.snapshot(),
        "optional_work": {"pending": len(enrichment.pending), "dropped": enrichment.dropped, "failures": enrichment.failures},
        "notification_telemetry": notification_telemetry_snapshot(),
        "database_connected": db_ok,
        "bot_mode": bot_status,
        "uptime_seconds": round(time.time() - _server_start_time, 1),
        "worker": worker_info,
        "sentinel_network": sentinel_info,
        "spotting_intelligence": {
            "deterministic_core": True,
            "terminal_arrival_ensemble": True,
            "prediction_lab": True,
            "next_60_shadow": True,
            "europe_sentinel_shadow": True,
            "gemini_advisor_enabled": bool(settings.gemini_api_key.strip()),
            "weather_provider": "Open-Meteo",
            "shared_adaptive_adsb_polling": True,
            "priority_admin_controls": True,
            "monochrome_ui": True,
            "alert_profiles": True,
        },
        "python_version": platform.python_version(),
    }


@app.api_route("/stats", methods=["GET", "HEAD"])
async def stats() -> dict[str, Any]:
    active_users = 0
    total_users = 0
    worker_metrics: dict[str, Any] = {}
    try:
        active_users = await users_col().count_documents({"setup_complete": True})
        total_users = await users_col().count_documents({})
        doc = await asyncio.wait_for(system_status_col().find_one({"_id": "monitor_worker"}), timeout=1.0)
        if doc:
            worker_metrics = {
                "version": doc.get("plane_version") or VERSION,
                "commit": doc.get("plane_commit") or COMMIT,
                "shared_regions_last_cycle": doc.get("shared_regions_last_cycle", 0),
                "provider_queries_last_cycle": doc.get("provider_queries_last_cycle", 0),
                "shared_snapshot_cache_hits_last_cycle": doc.get("shared_snapshot_cache_hits_last_cycle", 0),
                "priority_users_last_cycle": doc.get("priority_users_last_cycle", 0),
                "deferred_users_last_cycle": doc.get("deferred_users_last_cycle", 0),
                "notifications_paused_last_cycle": doc.get("notifications_paused_last_cycle", 0),
                "polling_mode": doc.get("polling_mode", "adaptive-shared-regions-priority"),
            }
    except Exception:
        pass
    return {
        "version": VERSION,
        "commit": COMMIT,
        "active_users": active_users,
        "total_users": total_users,
        "poll_interval_seconds": settings.poll_interval_seconds,
        "base_monitor_interval_seconds": settings.poll_interval_seconds,
        "discovery_poll_interval_seconds": 15,
        "hot_region_poll_interval_seconds": 5,
        "priority_hot_interval_seconds": 5,
        "default_radius_km": settings.default_radius_km,
        "cooldown_minutes": settings.cooldown_minutes,
        "cycle_stats": get_cycle_stats(),
        "shared_polling": worker_metrics,
        "prediction_lab_sentinel_regions": len(EUROPE_SENTINELS),
    }


@app.post("/webhook")
async def telegram_webhook(request: Request) -> Response:
    if not telegram_app:
        return Response(content="Bot not initialized", status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    secret = webhook_secret()
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not secret or not hmac.compare_digest(supplied, secret):
        return Response(content="Unauthorized", status_code=401)
    try:
        data = await request.json()
        if not isinstance(data, dict) or type(data.get("update_id")) is not int:
            return Response(content="Invalid update", status_code=400)
        update_id = data["update_id"]
        now = time.monotonic()
        while _recent_updates and (now - next(iter(_recent_updates.values())) > 3600 or len(_recent_updates) > 4096):
            _recent_updates.popitem(last=False)
        if update_id in _recent_updates:
            return Response(status_code=200)
        update = Update.de_json(data, telegram_app.bot)
        # PTB's single consumer preserves setup-state order across webhook
        # requests; admission is bounded and Telegram retries on saturation.
        telegram_app.update_queue.put_nowait(update)
        _recent_updates[update_id] = now
        return Response(status_code=200)
    except asyncio.QueueFull:
        logger.warning("telegram_update_queue_full")
        return Response(content="Busy", status_code=503)
    except (ValueError, TypeError, KeyError):
        return Response(content="Invalid update", status_code=400)
    except Exception:
        logger.exception("telegram_webhook_failed")
        return Response(content="Internal Error", status_code=500)


@app.get("/ready")
async def readiness() -> Response:
    health = await health_check()
    return JSONResponse(health, status_code=200 if health["status"] == "healthy" else 503)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", str(settings.port)))
    host = os.getenv("HOST", settings.host)
    uvicorn.run("app.main:app", host=host, port=port, reload=False)