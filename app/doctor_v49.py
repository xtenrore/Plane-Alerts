"""Plane Alerts v4.9 self-hosting diagnostics.

The doctor command is deliberately read-only. It validates configuration and
performs bounded connectivity probes without printing secrets or exact user
coordinates.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
import platform
import sys
import time
from typing import Iterable
from urllib.parse import urlsplit

import httpx
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import Settings, settings
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


def static_checks(config: Settings) -> list[DoctorCheck]:
    """Return secret-safe checks that require no network access."""
    checks: list[DoctorCheck] = []

    py_ok = sys.version_info >= (3, 11)
    checks.append(
        DoctorCheck(
            "python",
            "ok" if py_ok else "fail",
            f"Python {platform.python_version()} ({'supported' if py_ok else 'requires Python 3.11+'})",
        )
    )

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

    mongo = str(config.mongo_uri or "").strip()
    mongo_ok = mongo.startswith("mongodb://") or mongo.startswith("mongodb+srv://")
    checks.append(
        DoctorCheck(
            "database-config",
            "ok" if mongo_ok else "fail",
            "MongoDB URI scheme is valid" if mongo_ok else "MONGO_URI must use mongodb:// or mongodb+srv://",
        )
    )

    radius = float(config.default_radius_km)
    radius_ok = 0.5 <= radius <= 250.0
    checks.append(
        DoctorCheck(
            "radius",
            "ok" if radius_ok else "fail",
            f"Default radius {radius:g} km" if radius_ok else "DEFAULT_RADIUS_KM must be between 0.5 and 250 km",
        )
    )

    interval = int(config.poll_interval_seconds)
    if interval == 5:
        interval_status = "ok"
        interval_detail = "Five-second monitoring cadence configured"
    elif 1 <= interval <= 30:
        interval_status = "warn"
        interval_detail = f"POLL_INTERVAL_SECONDS={interval}; production baseline is 5 seconds"
    else:
        interval_status = "fail"
        interval_detail = "POLL_INTERVAL_SECONDS must be between 1 and 30 seconds"
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

    local_url = str(config.local_adsb_url or "").strip()
    if local_url:
        local_status = "ok" if _valid_http_url(local_url) else "fail"
        local_detail = "Local ADS-B receiver configured" if local_status == "ok" else "LOCAL_ADSB_URL must be an http(s) URL"
    else:
        local_status = "skip"
        local_detail = "Local ADS-B receiver not configured (optional)"
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
            "; ".join(contradictions) if contradictions else "No configuration contradictions detected",
        )
    )

    checks.append(
        DoctorCheck(
            "coordinates",
            "ok",
            "Observer coordinates are profile data; live doctor validates stored coordinate ranges without printing values",
        )
    )
    return checks


async def _database_check(config: Settings, timeout_s: float) -> tuple[DoctorCheck, DoctorCheck]:
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
        inspected = 0
        invalid = 0
        cursor = db["locations"].find({}, {"latitude": 1, "longitude": 1, "lat": 1, "lon": 1}).limit(500)
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
        elapsed = (time.perf_counter() - started) * 1000.0
        db_check = DoctorCheck("database", "ok", "MongoDB ping succeeded", round(elapsed, 1))
        coordinate_check = DoctorCheck(
            "stored-coordinates",
            "fail" if invalid else "ok",
            f"Inspected {inspected} stored location record(s); invalid={invalid}",
        )
        return db_check, coordinate_check
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000.0
        detail = f"MongoDB probe failed ({type(exc).__name__}); check URI, DNS and network access"
        return (
            DoctorCheck("database", "fail", detail, round(elapsed, 1)),
            DoctorCheck("stored-coordinates", "skip", "Coordinate validation skipped because MongoDB is unavailable"),
        )
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
            return DoctorCheck("telegram", "ok", "Telegram API authenticated", round(elapsed, 1))
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
        # A 4xx from a provider root still proves DNS/TLS/HTTP reachability; 5xx
        # is treated as degraded provider health.
        status = "ok" if response.status_code < 500 else "warn"
        return DoctorCheck(name, status, f"HTTP {response.status_code}", round(elapsed, 1))
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000.0
        return DoctorCheck(name, "warn", f"Probe failed ({type(exc).__name__})", round(elapsed, 1))


async def runtime_checks(config: Settings, timeout_s: float) -> list[DoctorCheck]:
    db_check, coordinate_check = await _database_check(config, timeout_s)
    checks: list[DoctorCheck] = [db_check, coordinate_check]
    checks.append(await _telegram_check(config, timeout_s))

    provider_pairs = (
        ("provider-adsb.lol", config.adsb_lol_base_url),
        ("provider-adsb.fi", config.adsb_fi_base_url),
        ("provider-airplanes.live", config.airplanes_live_base_url),
        ("provider-adsb.one", config.adsb_one_base_url),
        ("provider-opensky", config.opensky_base_url),
    )
    provider_checks = await asyncio.gather(*(_http_health(name, url, timeout_s) for name, url in provider_pairs))
    checks.extend(provider_checks)

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
                DoctorCheck("stored-coordinates", "skip", "Offline mode"),
                DoctorCheck("telegram", "skip", "Offline mode"),
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
    parser = argparse.ArgumentParser(prog="planealerts", description="Plane Alerts self-hosting diagnostics")
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
        print(
            json.dumps(
                {
                    "plane_alerts_version": VERSION,
                    "prediction_version": PREDICTION_VERSION,
                    "checks": [asdict(check) for check in checks],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        _print_text(checks)
    return _exit_code(checks)


if __name__ == "__main__":
    raise SystemExit(main())
