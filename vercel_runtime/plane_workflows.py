"""Plane? v3.3 durable workflows with fast Telegram callback acknowledgement.

v3.3 intentionally uses a new Vercel Workflow namespace so no v3.2 workflow can be
replayed against the v3.3 step graph. Telegram callback queries are acknowledged by
the webhook response before MongoDB, Gemini, or application handlers finish.
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from vercel.workflow import Workflows, sleep, start

from plane_runtime_support import (
    TelegramUpdate,
    _apply_runtime_config,
    _download_source,
    _ensure_telegram_runtime,
    _runtime_is_current,
    _should_detach_telegram_update,
    logger,
)

# Never reuse planev32 here. Durable workflow histories must remain compatible with
# the code that created them; v3.3 has a different Telegram step graph.
wf = Workflows(namespace="planev33")

MONITOR_INTERVAL = "3m"
MONITOR_CYCLES_PER_RUN = 480
_callback_answer_patched = False


def webhook_path_secret(telegram_token: str, generation: str) -> str:
    material = f"plane-v3.3|{generation}|{telegram_token}".encode()
    return hashlib.sha256(material).hexdigest()[:48]


def telegram_secret_header(path_secret: str) -> str:
    return hashlib.sha256(f"telegram-header|{path_secret}".encode()).hexdigest()[:48]


def _install_preacked_callback_answer() -> None:
    """Skip only the duplicate silent ACK already completed by webhook ingress."""
    global _callback_answer_patched
    if _callback_answer_patched:
        return

    from telegram import CallbackQuery

    original_answer = CallbackQuery.answer

    async def _answer(self: Any, *args: Any, **kwargs: Any) -> Any:
        text = kwargs.get("text")
        show_alert = bool(kwargs.get("show_alert", False))
        url = kwargs.get("url")
        if not args and not text and not show_alert and not url:
            return True
        return await original_answer(self, *args, **kwargs)

    CallbackQuery.answer = _answer
    _callback_answer_patched = True


@wf.step
async def configure_telegram_v33(
    config: dict[str, Any],
    generation: str,
    base_url: str,
) -> dict[str, Any]:
    """Install the v3.3 webhook/commands and publish Telegram runtime readiness."""
    token = str(config["telegram_bot_token"])
    path_secret = webhook_path_secret(token, generation)
    secret_header = telegram_secret_header(path_secret)
    webhook_url = f"{base_url.rstrip('/')}/telegram/{path_secret}"
    api = f"https://api.telegram.org/bot{token}"

    commands = [
        {"command": "start", "description": "Set up aircraft alerts"},
        {"command": "status", "description": "Show monitoring configuration"},
        {"command": "location", "description": "Update monitoring / shooting location"},
        {"command": "preferences", "description": "Choose aircraft categories and types"},
        {"command": "camera", "description": "Tell Gemini your camera body"},
        {"command": "lens", "description": "Tell Gemini your aircraft lens"},
        {"command": "conditions", "description": "Weather, atmosphere and sun geometry"},
        {"command": "photo", "description": "Live Gemini best-shot settings"},
        {"command": "help", "description": "Show commands"},
    ]

    async with httpx.AsyncClient(timeout=20.0) as client:
        me = await client.get(f"{api}/getMe")
        me.raise_for_status()
        identity = me.json().get("result") or {}
        webhook = await client.post(
            f"{api}/setWebhook",
            json={
                "url": webhook_url,
                "secret_token": secret_header,
                "drop_pending_updates": False,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        webhook.raise_for_status()
        if not webhook.json().get("ok"):
            raise RuntimeError("Telegram rejected webhook configuration")
        menu = await client.post(f"{api}/setMyCommands", json={"commands": commands})
        menu.raise_for_status()

    _download_source(generation)
    _apply_runtime_config(config)
    from app.database import connect_db, system_status_col

    await connect_db(max_retries=2, retry_delay=0.5, timeout_ms=8000, ensure_indexes=True)
    await system_status_col().update_one(
        {"_id": "telegram_runtime"},
        {"$set": {
            "generation": generation,
            "bot_username": identity.get("username", ""),
            "webhook_host": base_url,
            "ready": True,
            "runtime_version": "3.3",
        }},
        upsert=True,
    )
    return {"username": identity.get("username", ""), "webhook": True}


@wf.step
async def process_telegram_update_v33(
    config: dict[str, Any],
    generation: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Process one update with detailed v3.3 latency instrumentation."""
    started = time.perf_counter()
    update_id = payload.get("update_id")
    callback = isinstance(payload.get("callback_query"), dict)
    try:
        runtime_started = time.perf_counter()
        application = await _ensure_telegram_runtime(config, generation)
        runtime_ms = int((time.perf_counter() - runtime_started) * 1000)

        from telegram import Update

        _install_preacked_callback_answer()
        parse_started = time.perf_counter()
        update = Update.de_json(payload, application.bot)
        parse_ms = int((time.perf_counter() - parse_started) * 1000)

        handler_started = time.perf_counter()
        if update is not None:
            await application.process_update(update)
        handler_ms = int((time.perf_counter() - handler_started) * 1000)
        total_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "Telegram v3.3 update processed update_id=%s callback=%s runtime_ms=%d parse_ms=%d handler_ms=%d total_ms=%d",
            update_id,
            callback,
            runtime_ms,
            parse_ms,
            handler_ms,
            total_ms,
        )
        return {
            "processed": True,
            "update_id": update_id,
            "callback": callback,
            "runtime_ms": runtime_ms,
            "handler_ms": handler_ms,
            "elapsed_ms": total_ms,
        }
    except Exception as exc:
        logger.exception(
            "Telegram v3.3 update processing failed update_id=%s callback=%s type=%s",
            update_id,
            callback,
            type(exc).__name__,
        )
        return {"processed": False, "update_id": update_id, "callback": callback}


@wf.workflow
async def telegram_slow_update_workflow_v33(
    config: dict[str, Any],
    generation: str,
    payload: dict[str, Any],
) -> None:
    await process_telegram_update_v33(config=config, generation=generation, payload=payload)


@wf.step
async def launch_slow_telegram_update_v33(
    config: dict[str, Any],
    generation: str,
    payload: dict[str, Any],
) -> bool:
    await start(telegram_slow_update_workflow_v33, config, generation, payload)
    return True


@wf.workflow
async def telegram_workflow(
    config: dict[str, Any],
    generation: str,
    base_url: str,
) -> None:
    await configure_telegram_v33(config=config, generation=generation, base_url=base_url)
    path_secret = webhook_path_secret(str(config["telegram_bot_token"]), generation)
    hook_token = f"telegram:{path_secret}"

    async for event in TelegramUpdate.wait(token=hook_token):
        if _should_detach_telegram_update(event.update):
            await launch_slow_telegram_update_v33(
                config=config,
                generation=generation,
                payload=event.update,
            )
            continue
        await process_telegram_update_v33(
            config=config,
            generation=generation,
            payload=event.update,
        )


@wf.step
async def monitor_cycle_step_v33(config: dict[str, Any], generation: str) -> bool:
    """Run one monitor cycle in the isolated v3.3 workflow namespace."""
    started = time.perf_counter()
    _download_source(generation)
    _apply_runtime_config(config)

    from app.database import connect_db, system_status_col
    from app.worker.monitor import init_services, run_monitor_cycle

    await connect_db(max_retries=2, retry_delay=0.25, timeout_ms=6000, ensure_indexes=False)
    if not await _runtime_is_current(generation):
        return False
    await init_services()
    await run_monitor_cycle()
    await system_status_col().update_one(
        {"_id": "vercel_monitor"},
        {"$set": {
            "generation": generation,
            "ready": True,
            "updated_at": datetime.now(timezone.utc),
            "runtime_version": "3.3",
        }},
        upsert=True,
    )
    logger.info(
        "Monitor v3.3 cycle completed generation=%s total_ms=%d",
        generation[:12],
        int((time.perf_counter() - started) * 1000),
    )
    return True


@wf.step
async def chain_monitor_workflow_v33(config: dict[str, Any], generation: str) -> bool:
    await start(monitor_workflow, config, generation)
    return True


@wf.workflow
async def monitor_workflow(config: dict[str, Any], generation: str) -> None:
    for _ in range(MONITOR_CYCLES_PER_RUN):
        active = await monitor_cycle_step_v33(config=config, generation=generation)
        if not active:
            return
        await sleep(MONITOR_INTERVAL)
    await chain_monitor_workflow_v33(config=config, generation=generation)


__all__ = [
    "TelegramUpdate",
    "monitor_workflow",
    "telegram_secret_header",
    "telegram_workflow",
    "webhook_path_secret",
    "wf",
]
