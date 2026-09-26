"""Serverless ingress and secure GitHub-OIDC bootstrap for Plane? v3.3."""
from __future__ import annotations

import asyncio
import hmac
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import jwt
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pymongo import MongoClient
from vercel.workflow import start

from plane_workflows import (
    TelegramUpdate,
    monitor_workflow,
    telegram_secret_header,
    telegram_workflow,
    webhook_path_secret,
)

app = FastAPI(title="Plane? Telegram Bot v3.3", docs_url=None, redoc_url=None)
logger = logging.getLogger(__name__)

OIDC_ISSUER = "https://token.actions.githubusercontent.com"
OIDC_AUDIENCE = "plane-bot-vercel-bootstrap"
EXPECTED_REPOSITORY = "xtenrore/Gemini-Telegram-Bot-Exprience"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_JWKS = jwt.PyJWKClient(f"{OIDC_ISSUER}/.well-known/jwks")


class RuntimeConfig(BaseModel):
    telegram_bot_token: str = Field(min_length=20)
    mongo_uri: str = Field(min_length=12)
    gemini_api_key: str = Field(min_length=10)
    generation: str = Field(min_length=40, max_length=40)
    database_name: str = "aircraft_bot"
    groq_api_key: str = ""
    opensky_credentials_json: str = ""
    admin_password: str = ""
    admin_telegram_id: int | None = None

    def workflow_payload(self) -> dict[str, Any]:
        data = self.model_dump()
        data.pop("generation", None)
        return data


def _bearer(value: str | None) -> str:
    if not value or not value.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="GitHub OIDC bearer token required")
    return value[7:].strip()


def _verify_github_oidc(value: str | None, *, allow_non_main: bool = False) -> dict[str, Any]:
    token = _bearer(value)
    try:
        key = _JWKS.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            issuer=OIDC_ISSUER,
            audience=OIDC_AUDIENCE,
            options={"require": ["exp", "iat", "iss", "aud", "repository", "ref"]},
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid GitHub OIDC identity") from exc

    if claims.get("repository") != EXPECTED_REPOSITORY:
        raise HTTPException(status_code=403, detail="Repository identity rejected")
    ref = str(claims.get("ref") or "")
    if not allow_non_main and ref != "refs/heads/main":
        raise HTTPException(status_code=403, detail="Production bootstrap requires main")
    if allow_non_main and not (ref == "refs/heads/main" or ref.startswith("refs/heads/chatgpt/")):
        raise HTTPException(status_code=403, detail="Preview bootstrap ref rejected")
    return claims


def _db_check_and_mark(config: RuntimeConfig, *, state: str, base_url: str = "") -> dict[str, Any]:
    client = MongoClient(
        config.mongo_uri,
        serverSelectionTimeoutMS=8000,
        connectTimeoutMS=8000,
        maxPoolSize=2,
        appname="plane-vercel-bootstrap",
    )
    try:
        client.admin.command("ping")
        db = client[config.database_name]
        col = db["system_status"]
        current = col.find_one({"_id": "vercel_runtime"}) or {}
        if state == "inspect":
            return current
        col.update_one(
            {"_id": "vercel_runtime"},
            {"$set": {
                "generation": config.generation,
                "state": state,
                "base_url": base_url,
                "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
        return current
    finally:
        client.close()


def _verify_mongo_runtime(config: RuntimeConfig) -> tuple[bool, bool, bool]:
    client = MongoClient(
        config.mongo_uri,
        serverSelectionTimeoutMS=8000,
        connectTimeoutMS=8000,
        maxPoolSize=2,
        appname="plane-vercel-verify",
    )
    try:
        client.admin.command("ping")
        db = client[config.database_name]
        runtime = db["system_status"].find_one({"_id": "vercel_runtime"}) or {}
        monitor = db["system_status"].find_one({"_id": "vercel_monitor"}) or {}
        telegram = db["system_status"].find_one({"_id": "telegram_runtime"}) or {}
        generation_ok = runtime.get("generation") == config.generation and runtime.get("state") == "running"
        monitor_ok = monitor.get("generation") == config.generation and bool(monitor.get("ready"))
        telegram_ok = telegram.get("generation") == config.generation and bool(telegram.get("ready"))
        return generation_ok, monitor_ok, telegram_ok
    finally:
        client.close()


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "plane-telegram-bot", "version": "3.3", "status": "online"}


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "plane-telegram-bot", "version": "3.3"}


@app.post("/bootstrap")
async def bootstrap(
    request: Request,
    config: RuntimeConfig,
    authorization: str | None = Header(default=None),
    x_plane_preview: str | None = Header(default=None),
) -> dict[str, Any]:
    preview = x_plane_preview == "1"
    claims = _verify_github_oidc(authorization, allow_non_main=preview)
    if not SHA_RE.fullmatch(config.generation):
        raise HTTPException(status_code=400, detail="Invalid deployment generation")
    if config.generation != str(claims.get("sha") or ""):
        raise HTTPException(status_code=403, detail="Generation does not match GitHub identity")

    base_url = str(request.base_url).rstrip("/")
    current = await asyncio.to_thread(_db_check_and_mark, config, state="inspect")
    if (
        current.get("generation") == config.generation
        and current.get("state") == "running"
        and current.get("base_url") == base_url
    ):
        return {"ok": True, "already_running": True, "generation": config.generation[:12]}

    await asyncio.to_thread(_db_check_and_mark, config, state="bootstrapping", base_url=base_url)
    payload = config.workflow_payload()
    try:
        await start(telegram_workflow, payload, config.generation, base_url)
        await start(monitor_workflow, payload, config.generation)
    except Exception:
        await asyncio.to_thread(_db_check_and_mark, config, state="failed", base_url=base_url)
        raise
    await asyncio.to_thread(_db_check_and_mark, config, state="running", base_url=base_url)
    return {"ok": True, "started": True, "generation": config.generation[:12]}


@app.post("/telegram/{path_secret}")
async def telegram_ingress(
    path_secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> Any:
    """Accept Telegram updates and ACK callback buttons in the webhook response.

    Telegram supports invoking a Bot API method directly from the JSON webhook
    response. For callback queries v3.3 uses that path to execute
    ``answerCallbackQuery`` without waiting for MongoDB, Gemini or the durable handler
    to finish. The durable workflow still receives the original update and performs
    the actual state/UI change.
    """
    started = time.perf_counter()
    expected_header = telegram_secret_header(path_secret)
    if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        expected_header, x_telegram_bot_api_secret_token
    ):
        raise HTTPException(status_code=403, detail="Telegram secret rejected")

    payload = await request.json()
    if not isinstance(payload, dict) or "update_id" not in payload:
        raise HTTPException(status_code=400, detail="Invalid Telegram update")

    callback = payload.get("callback_query")
    callback_id = ""
    if isinstance(callback, dict):
        callback_id = str(callback.get("id") or "")

    event = TelegramUpdate(update=payload)
    resume_started = time.perf_counter()
    try:
        await event.resume(f"telegram:{path_secret}")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Telegram workflow is not ready") from exc

    resume_ms = int((time.perf_counter() - resume_started) * 1000)
    total_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "Telegram v3.3 ingress accepted update_id=%s callback=%s resume_ms=%d total_ms=%d",
        payload.get("update_id"),
        bool(callback_id),
        resume_ms,
        total_ms,
    )

    if callback_id:
        return JSONResponse(
            content={
                "method": "answerCallbackQuery",
                "callback_query_id": callback_id,
            },
            headers={"X-Plane-Version": "3.3"},
        )

    return {"ok": True}


@app.post("/verify")
async def verify_runtime(
    request: Request,
    config: RuntimeConfig,
    authorization: str | None = Header(default=None),
    x_plane_preview: str | None = Header(default=None),
) -> dict[str, Any]:
    preview = x_plane_preview == "1"
    claims = _verify_github_oidc(authorization, allow_non_main=preview)
    if config.generation != str(claims.get("sha") or ""):
        raise HTTPException(status_code=403, detail="Generation does not match GitHub identity")

    base_url = str(request.base_url).rstrip("/")
    generation_ok, monitor_ok, telegram_db_ok = await asyncio.to_thread(_verify_mongo_runtime, config)

    token = config.telegram_bot_token
    path_secret = webhook_path_secret(token, config.generation)
    expected_webhook = f"{base_url}/telegram/{path_secret}"
    telegram_ok = False
    webhook_ok = False
    gemini_ok = False
    username = ""
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            me = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            info = me.json()
            telegram_ok = me.status_code == 200 and bool(info.get("ok"))
            username = str((info.get("result") or {}).get("username") or "")
            wh = await client.get(f"https://api.telegram.org/bot{token}/getWebhookInfo")
            whj = wh.json()
            webhook_ok = wh.status_code == 200 and (whj.get("result") or {}).get("url") == expected_webhook
        except Exception:
            pass
        try:
            gm = await client.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                params={"key": config.gemini_api_key},
            )
            gemini_ok = gm.status_code == 200
        except Exception:
            pass

    ready = all([generation_ok, monitor_ok, telegram_db_ok, telegram_ok, webhook_ok, gemini_ok])
    return {
        "ready": ready,
        "generation": generation_ok,
        "mongodb": generation_ok,
        "monitor": monitor_ok,
        "telegram": telegram_ok,
        "telegram_runtime": telegram_db_ok,
        "webhook": webhook_ok,
        "gemini": gemini_ok,
        "bot_username": username,
    }
