"""Plane Alerts v5.5 community self-hosting diagnostics.

This doctor is read-only and deliberately excludes AGY. It validates either the
first-class local SQLite backend or MongoDB, then performs bounded Telegram and
provider probes without printing credentials or exact private coordinates.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import platform
import sys
import time
from typing import Iterable
from urllib.parse import urlsplit

import httpx
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import Settings, settings
from app.local_database_v55 import SQLiteDatabase, default_sqlite_path
from app.storage_migrations_v48 import EXPECTED_SCHEMA_VERSION
from app.version import PREDICTION_VERSION, VERSION


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    detail: str
    latency_ms: float | None = None


def _valid_http_url(value: str) -> bool:
    parsed = urlsplit(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _configured_token(value: str) -> bool:
    token = str(value or "").strip()
    return bool(token and token != "your_bot_token_from_botfather")


def _backend() -> str:
    raw = os.getenv("DATABASE_BACKEND", "mongodb").strip().lower()
    if raw in {"local", "sqlite"}:
        return "sqlite"
    if raw in {"mongo", "mongodb", ""}:
        return "mongodb"
    return "invalid"


def _sqlite_path() -> Path:
    raw = os.getenv("SQLITE_PATH", "").strip()
    return Path(raw).expanduser() if raw else default_sqlite_path()


def _opensky_slots(config: Settings) -> list[str]:
    return [
        str(config.opensky_1 or "").strip(),
        str(config.opensky_2 or "").strip(),
        str(config.opensky_3 or "").strip(),
        str(config.opensky_4 or "").strip(),
        str(config.opensky_5 or "").strip(),
    ]


def _parse_opensky_slot(raw: str) -> tuple[str, str] | None:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        client_id = str(payload.get("clientId") or payload.get("username") or "").strip()
        client_secret = str(payload.get("clientSecret") or payload.get("password") or "").strip()
        return (client_id, client_secret) if client_id and client_secret else None
    if ":" in value:
        client_id, client_secret = value.split(":", 1)
        client_id = client_id.strip()
        client_secret = client_secret.strip()
        return (client_id, client_secret) if client_id and client_secret else None
    return None


def static_checks(config: Settings) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    py_ok = sys.version_info >= (3, 11)
    checks.append(
        DoctorCheck(
            "python",
            "ok" if py_ok else "fail",
            f"Python {platform.python_version()} ({'supported' if py_ok else 'requires Python 3.11+'})",
        )
    )
    machine = platform.machine() or "unknown"
    checks.append(DoctorCheck("platform", "ok", f"{platform.system()} {machine}"))
    checks.append(
        DoctorCheck(
            "version",
            "ok" if VERSION and PREDICTION_VERSION else "fail",
            f"Plane Alerts {VERSION}; prediction {PREDICTION_VERSION}",
        )
    )
    checks.append(
        DoctorCheck(
            "telegram-config",
            "ok" if _configured_token(config.telegram_bot_token) else "fail",
            "Telegram token configured" if _configured_token(config.telegram_bot_token) else "TELEGRAM_BOT_TOKEN is required",
        )
    )

    backend = _backend()
    if backend == "sqlite":
        db_detail = "Local SQLite database configured"
        db_status = "ok"
    elif backend == "mongodb":
        mongo = str(config.mongo_uri or "").strip()
        mongo_ok = mongo.startswith("mongodb://") or mongo.startswith("mongodb+srv://")
        db_status = "ok" if mongo_ok else "fail"
        db_detail = "MongoDB URI scheme is valid" if mongo_ok else "MONGO_URI must use mongodb:// or mongodb+srv://"
    else:
        db_status = "fail"
        db_detail = "DATABASE_BACKEND must be sqlite/local or mongodb"
    checks.append(DoctorCheck("database-config", db_status, db_detail))

    radius = float(config.default_radius_km)
    checks.append(
        DoctorCheck(
            "radius",
            "ok" if 0.5 <= radius <= 250.0 else "fail",
            f"Default radius {radius:g} km" if 0.5 <= radius <= 250.0 else "DEFAULT_RADIUS_KM must be between 0.5 and 250 km",
        )
    )
    interval = int(config.poll_interval_seconds)
    if interval == 5:
        interval_status, interval_detail = "ok", "Five-second monitoring cadence configured"
    elif 1 <= interval <= 30:
        interval_status, interval_detail = "warn", f"POLL_INTERVAL_SECONDS={interval}; production baseline is 5 seconds"
    else:
        interval_status, interval_detail = "fail", "POLL_INTERVAL_SECONDS must be between 1 and 30 seconds"
    checks.append(DoctorCheck("monitor-cadence", interval_status, interval_detail))

    providers = {
        "adsb.lol": config.adsb_lol_base_url,
        "adsb.fi": config.adsb_fi_base_url,
        "airplanes.live": config.airplanes_live_base_url,
        "adsb.one": config.adsb_one_base_url,
        "OpenSky": config.opensky_base_url,
    }
    bad = [name for name, url in providers.items() if not _valid_http_url(url)]
    checks.append(
        DoctorCheck(
            "provider-config",
            "fail" if bad else "ok",
            "Provider URLs are valid" if not bad else "Invalid provider URL(s): " + ", ".join(sorted(bad)),
        )
    )

    configured_slots = [raw for raw in _opensky_slots(config) if raw]
    malformed_slots = sum(1 for raw in configured_slots if _parse_opensky_slot(raw) is None)
    if malformed_slots:
        checks.append(
            DoctorCheck(
                "opensky-config",
                "fail",
                f"{malformed_slots} configured OpenSky credential slot(s) are malformed; expected clientId:clientSecret or JSON",
            )
        )
    elif configured_slots:
        checks.append(DoctorCheck("opensky-config", "ok", f"{len(configured_slots)} OpenSky credential slot(s) configured"))
    else:
        checks.append(DoctorCheck("opensky-config", "skip", "OpenSky credentials not configured (optional)"))

    local_url = str(config.local_adsb_url or "").strip()
    if local_url:
        local_status = "ok" if _valid_http_url(local_url) else "fail"
        local_detail = "Local ADS-B receiver configured" if local_status == "ok" else "LOCAL_ADSB_URL must be an http(s) URL"
    else:
        local_status, local_detail = "skip", "Local ADS-B receiver not configured (optional)"
    checks.append(DoctorCheck("local-receiver-config", local_status, local_detail))

    contradictions: list[str] = []
    if str(config.webhook_url or "").strip() and not _configured_token(config.telegram_bot_token):
        contradictions.append("WEBHOOK_URL requires TELEGRAM_BOT_TOKEN")
    if str(config.local_adsb_auth_header or "").strip() and not local_url:
        contradictions.append("LOCAL_ADSB_AUTH_HEADER requires LOCAL_ADSB_URL")
    checks.append(
        DoctorCheck(
            "configuration-contradictions",
            "fail" if contradictions else "ok",
            "; ".join(contradictions) if contradictions else "No self-host configuration contradictions detected",
        )
    )
    checks.append(
        DoctorCheck(
            "agy-exclusion",
            "ok",
            "Community installer and doctor do not configure, start, call or authenticate AGY",
        )
    )
    checks.append(
        DoctorCheck(
            "coordinates",
            "ok",
            "Observer coordinates remain Telegram profile data; stored ranges are validated without printing values",
        )
    )
    return checks


async def _inspect_coordinates(db, limit: int = 500) -> DoctorCheck:
    inspected = 0
    invalid = 0
    cursor = db["locations"].find({}, {"latitude": 1, "longitude": 1, "lat": 1, "lon": 1}).limit(limit)
    async for row in cursor:
        inspected += 1
        lat = row.get("latitude", row.get("lat"))
        lon = row.get("longitude", row.get("lon"))
        if lat is None or lon is None:
            continue
        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except (TypeError, ValueError):
            invalid += 1
            continue
        if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
            invalid += 1
    return DoctorCheck(
        "stored-coordinates",
        "fail" if invalid else "ok",
        f"Inspected {inspected} stored location record(s); invalid={invalid}",
    )


async def _sqlite_database_check(timeout_s: float) -> list[DoctorCheck]:
    started = time.perf_counter()
    db: SQLiteDatabase | None = None
    try:
        db = SQLiteDatabase(_sqlite_path())
        await asyncio.wait_for(db.command("ping"), timeout=timeout_s)
        ok, detail = await asyncio.wait_for(db.integrity_check(), timeout=timeout_s)
        elapsed = (time.perf_counter() - started) * 1000.0
        checks = [
            DoctorCheck(
                "database",
                "ok" if ok else "fail",
                "SQLite integrity check passed" if ok else f"SQLite integrity check failed ({detail})",
                round(elapsed, 1),
            )
        ]
        migration = await db["schema_migrations"].find_one({"version": EXPECTED_SCHEMA_VERSION})
        checks.append(
            DoctorCheck(
                "database-schema",
                "ok" if migration else "warn",
                f"Schema version {EXPECTED_SCHEMA_VERSION} recorded" if migration else "Schema migration record not found yet",
            )
        )
        checks.append(await _inspect_coordinates(db))
        return checks
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000.0
        return [
            DoctorCheck("database", "fail", f"SQLite probe failed ({type(exc).__name__})", round(elapsed, 1)),
            DoctorCheck("database-schema", "skip", "Schema validation skipped because local database is unavailable"),
            DoctorCheck("stored-coordinates", "skip", "Coordinate validation skipped because local database is unavailable"),
        ]
    finally:
        if db is not None:
            await db.close()


async def _mongo_database_check(config: Settings, timeout_s: float) -> list[DoctorCheck]:
    started = time.perf_counter()
    client = AsyncIOMotorClient(
        config.mongo_uri,
        serverSelectionTimeoutMS=max(250, int(timeout_s * 1000)),
        connectTimeoutMS=max(250, int(timeout_s * 1000)),
        socketTimeoutMS=max(250, int(timeout_s * 1000)),
    )
    try:
        await asyncio.wait_for(client.admin.command("ping"), timeout=timeout_s)
        db = client[config.database_name]
        elapsed = (time.perf_counter() - started) * 1000.0
        checks = [DoctorCheck("database", "ok", "MongoDB ping succeeded", round(elapsed, 1))]
        migration = await db["schema_migrations"].find_one({"version": EXPECTED_SCHEMA_VERSION})
        checks.append(
            DoctorCheck(
                "database-schema",
                "ok" if migration else "warn",
                f"Schema version {EXPECTED_SCHEMA_VERSION} recorded" if migration else "Schema migration record not found yet",
            )
        )
        checks.append(await _inspect_coordinates(db))
        return checks
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000.0
        return [
            DoctorCheck("database", "fail", f"MongoDB probe failed ({type(exc).__name__}); check URI, DNS and network access", round(elapsed, 1)),
            DoctorCheck("database-schema", "skip", "Schema validation skipped because MongoDB is unavailable"),
            DoctorCheck("stored-coordinates", "skip", "Coordinate validation skipped because MongoDB is unavailable"),
        ]
    finally:
        client.close()


async def _telegram_check(config: Settings, timeout_s: float) -> DoctorCheck:
    if not _configured_token(config.telegram_bot_token):
        return DoctorCheck("telegram", "fail", "Telegram token is not configured")
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
            response = await client.get(f"https://api.telegram.org/bot{config.telegram_bot_token}/getMe")
        elapsed = (time.perf_counter() - started) * 1000.0
        if response.status_code == 200:
            payload = response.json() if response.content else {}
            username = str(((payload or {}).get("result") or {}).get("username") or "")
            detail = "Telegram API authenticated" + (f" as @{username}" if username else "")
            return DoctorCheck("telegram", "ok", detail, round(elapsed, 1))
        return DoctorCheck("telegram", "fail", f"Telegram API returned HTTP {response.status_code}", round(elapsed, 1))
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000.0
        return DoctorCheck("telegram", "fail", f"Telegram probe failed ({type(exc).__name__})", round(elapsed, 1))


async def _http_health(name: str, url: str, timeout_s: float) -> DoctorCheck:
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
            response = await client.get(url)
        elapsed = (time.perf_counter() - started) * 1000.0
        status = "ok" if response.status_code < 500 else "warn"
        return DoctorCheck(name, status, f"HTTP {response.status_code}", round(elapsed, 1))
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000.0
        return DoctorCheck(name, "warn", f"Probe failed ({type(exc).__name__})", round(elapsed, 1))


async def _opensky_auth_check(config: Settings, timeout_s: float) -> DoctorCheck:
    credentials = [_parse_opensky_slot(raw) for raw in _opensky_slots(config) if raw]
    credentials = [pair for pair in credentials if pair is not None]
    if not credentials:
        return DoctorCheck("opensky-auth", "skip", "OpenSky credentials not configured")
    client_id, client_secret = credentials[0]
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
            response = await client.post(
                config.opensky_token_url,
                data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        elapsed = (time.perf_counter() - started) * 1000.0
        if response.status_code == 200 and str((response.json() or {}).get("access_token") or ""):
            return DoctorCheck("opensky-auth", "ok", "OpenSky OAuth2 credential accepted", round(elapsed, 1))
        return DoctorCheck("opensky-auth", "warn", f"OpenSky token endpoint returned HTTP {response.status_code}", round(elapsed, 1))
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000.0
        return DoctorCheck("opensky-auth", "warn", f"OpenSky authentication probe failed ({type(exc).__name__})", round(elapsed, 1))


async def runtime_checks(config: Settings, timeout_s: float) -> list[DoctorCheck]:
    if _backend() == "sqlite":
        checks = await _sqlite_database_check(timeout_s)
    elif _backend() == "mongodb":
        checks = await _mongo_database_check(config, timeout_s)
    else:
        checks = [
            DoctorCheck("database", "fail", "Invalid DATABASE_BACKEND"),
            DoctorCheck("database-schema", "skip", "Invalid database configuration"),
            DoctorCheck("stored-coordinates", "skip", "Invalid database configuration"),
        ]
    checks.append(await _telegram_check(config, timeout_s))
    checks.append(await _opensky_auth_check(config, timeout_s))
    provider_pairs = (
        ("provider-adsb.lol", config.adsb_lol_base_url),
        ("provider-adsb.fi", config.adsb_fi_base_url),
        ("provider-airplanes.live", config.airplanes_live_base_url),
        ("provider-adsb.one", config.adsb_one_base_url),
        ("provider-opensky", config.opensky_base_url),
    )
    checks.extend(await asyncio.gather(*(_http_health(name, url, timeout_s) for name, url in provider_pairs)))
    if config.local_adsb_url:
        checks.append(await _http_health("local-receiver", config.local_adsb_url, timeout_s))
    else:
        checks.append(DoctorCheck("local-receiver", "skip", "Local receiver not configured"))
    return checks


async def run_doctor(config: Settings = settings, *, offline: bool = False, timeout_s: float = 1.5) -> list[DoctorCheck]:
    checks = static_checks(config)
    if offline:
        checks.extend(
            [
                DoctorCheck("database", "skip", "Offline mode"),
                DoctorCheck("database-schema", "skip", "Offline mode"),
                DoctorCheck("stored-coordinates", "skip", "Offline mode"),
                DoctorCheck("telegram", "skip", "Offline mode"),
                DoctorCheck("opensky-auth", "skip", "Offline mode"),
                DoctorCheck("provider-health", "skip", "Offline mode"),
                DoctorCheck("local-receiver", "skip", "Offline mode"),
            ]
        )
        return checks
    checks.extend(await runtime_checks(config, timeout_s))
    return checks


def _print_text(checks: Iterable[DoctorCheck]) -> None:
    for check in checks:
        latency = f" ({check.latency_ms:.1f} ms)" if check.latency_ms is not None else ""
        print(f"[{check.status.upper():4}] {check.name}: {check.detail}{latency}")


def _exit_code(checks: Iterable[DoctorCheck]) -> int:
    return 2 if any(check.status == "fail" for check in checks) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="planealerts", description="Plane Alerts community self-hosting diagnostics")
    sub = parser.add_subparsers(dest="command")
    doctor = sub.add_parser("doctor", help="validate configuration and dependency health")
    doctor.add_argument("--offline", action="store_true", help="run configuration checks without network access")
    doctor.add_argument("--json", action="store_true", dest="as_json", help="emit machine-readable JSON")
    doctor.add_argument("--timeout", type=float, default=1.5, help="per-service probe timeout in seconds")
    args = parser.parse_args(argv)
    if args.command != "doctor":
        parser.print_help()
        return 2
    timeout_s = max(0.25, min(10.0, float(args.timeout)))
    checks = asyncio.run(run_doctor(settings, offline=bool(args.offline), timeout_s=timeout_s))
    if args.as_json:
        print(json.dumps({"plane_alerts_version": VERSION, "prediction_version": PREDICTION_VERSION, "checks": [asdict(check) for check in checks]}, sort_keys=True, separators=(",", ":")))
    else:
        _print_text(checks)
    return _exit_code(checks)


if __name__ == "__main__":
    raise SystemExit(main())
