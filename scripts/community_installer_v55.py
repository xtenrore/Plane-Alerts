#!/usr/bin/env python3
"""Plane Alerts v5.5 guided community self-hosting installer."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

REPO = "xtenrore/Plane-Alerts"
REPO_URL = f"https://github.com/{REPO}.git"
GITHUB_API = f"https://api.github.com/repos/{REPO}"
SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
SECRET_KEYS = {
    "TELEGRAM_BOT_TOKEN", "WEBHOOK_SECRET", "MONGO_URI", "OPENSKY_1",
    "OPENSKY_2", "OPENSKY_3", "OPENSKY_4", "OPENSKY_5",
    "OPENSKY_CREDENTIALS_JSON", "GEMINI_API_KEY", "GEMINI_API_KEY_2",
    "GROQ_KEY", "GROQ_KEY_2", "GROQ_API_KEY", "GOOGLE_CONTRAILS_API_KEY",
    "LOCAL_ADSB_AUTH_HEADER", "ADMIN_PASSWORD",
}


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=check, text=True,
                          capture_output=capture)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def semver(value: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(str(value or "").strip())
    if not match:
        raise ValueError(f"Invalid stable release tag: {value!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def api_json(url: str, *, method: str = "GET", data: bytes | None = None,
             headers: dict[str, str] | None = None, timeout: float = 10.0) -> Any:
    request_headers = {"User-Agent": "Plane-Alerts-Community-Installer/5.5", "Accept": "application/json"}
    request_headers.update(headers or {})
    request = Request(url, method=method, data=data, headers=request_headers)
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def latest_stable_release() -> dict[str, Any]:
    payload = api_json(f"{GITHUB_API}/releases/latest")
    if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
        raise RuntimeError("GitHub did not return a stable Plane Alerts release")
    tag = str(payload.get("tag_name") or "")
    semver(tag)
    return payload


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def render_env(template: Path, updates: dict[str, str]) -> str:
    source = template.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in source:
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    return "\n".join(out).rstrip() + "\n"


def redact_value(key: str, value: str) -> str:
    upper = key.upper()
    if upper in SECRET_KEYS or any(token in upper for token in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "AUTHORIZATION")):
        return "***"
    if upper in {"LOCAL_ADSB_RECEIVER_LATITUDE", "LOCAL_ADSB_RECEIVER_LONGITUDE"}:
        return "***"
    if upper == "MONGO_URI":
        parsed = urlsplit(value)
        return f"{parsed.scheme or 'mongodb'}://***@{parsed.hostname or 'configured-host'}"
    return value


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        tmp = Path(handle.name)
    tmp.replace(path)


def harden_env(path: Path) -> None:
    if os.name == "nt":
        user = os.environ.get("USERNAME", "")
        if user:
            subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)", "SYSTEM:(F)", "Administrators:(F)"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        path.chmod(0o600)


def validate_telegram(token: str) -> tuple[bool, str]:
    token = token.strip()
    if not token:
        return False, "Telegram bot token is required"
    try:
        payload = api_json(f"https://api.telegram.org/bot{token}/getMe", timeout=8.0)
        if isinstance(payload, dict) and payload.get("ok"):
            username = str((payload.get("result") or {}).get("username") or "bot")
            return True, f"Telegram authenticated as @{username}"
        return False, "Telegram rejected this bot token"
    except HTTPError as exc:
        return False, f"Telegram rejected this bot token (HTTP {exc.code})"
    except (URLError, TimeoutError, OSError):
        return False, "Telegram could not be reached from this machine"


def _parse_opensky(value: str) -> tuple[str, str] | None:
    value = value.strip()
    if not value:
        return None
    try:
        obj = json.loads(value)
    except json.JSONDecodeError:
        obj = None
    if isinstance(obj, dict):
        client = str(obj.get("clientId") or "").strip()
        secret = str(obj.get("clientSecret") or "").strip()
        return (client, secret) if client and secret else None
    if ":" in value:
        client, secret = value.split(":", 1)
        if client.strip() and secret.strip():
            return client.strip(), secret.strip()
    return None


def validate_opensky(value: str) -> tuple[bool, str]:
    parsed = _parse_opensky(value)
    if parsed is None:
        return False, "OpenSky credential must be clientId:clientSecret or equivalent JSON"
    client_id, client_secret = parsed
    body = urlencode({"grant_type": "client_credentials", "client_id": client_id,
                      "client_secret": client_secret}).encode()
    url = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
    try:
        payload = api_json(url, method="POST", data=body,
                           headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=10.0)
        if isinstance(payload, dict) and payload.get("access_token"):
            return True, "OpenSky OAuth2 credentials authenticated"
        return False, "OpenSky authentication did not return an access token"
    except HTTPError as exc:
        return False, f"OpenSky credentials were not accepted (HTTP {exc.code})"
    except (URLError, TimeoutError, OSError):
        return False, "OpenSky authentication endpoint could not be reached"


def check_receiver(url: str) -> tuple[bool, str]:
    base = url.rstrip("/")
    candidates = [base] if base.lower().endswith(".json") else [
        f"{base}/data/aircraft.json", f"{base}/tar1090/data/aircraft.json", f"{base}/aircraft.json"]
    for candidate in candidates:
        try:
            payload = api_json(candidate, timeout=3.0)
            aircraft = payload.get("aircraft") if isinstance(payload, dict) else None
            if isinstance(aircraft, list):
                return True, f"Receiver reachable; aircraft currently reported: {len(aircraft)}"
        except Exception:
            continue
    return False, "Plane Alerts could not reach a supported aircraft JSON endpoint on this receiver"


def choose(prompt: str, options: list[str], default: int = 1) -> int:
    while True:
        print(f"\n{prompt}")
        for index, option in enumerate(options, 1):
            suffix = " (recommended)" if index == default else ""
            print(f"  [{index}] {option}{suffix}")
        answer = input(f"Choose [{default}]: ").strip()
        if not answer:
            return default
        if answer.lower() in {"q", "quit", "cancel", "c"}:
            raise KeyboardInterrupt
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer)
        print("Please enter one of the numbers shown, or type Cancel.")


def prompt_secret(label: str, existing: bool = False) -> str:
    suffix = " (press Enter to keep current)" if existing else ""
    return getpass.getpass(f"{label}{suffix}: ").strip()


def venv_python(root: Path) -> Path:
    return root / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")


def install_dependencies(root: Path) -> None:
    py = venv_python(root)
    if not py.exists():
        run([sys.executable, "-m", "venv", str(root / ".venv")])
    run([str(py), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])
    run([str(py), "-m", "pip", "install", "-r", str(root / "requirements.txt")])
    run([str(py), str(root / "scripts/build_airport_database.py")], cwd=root)


def installation_paths(root: Path) -> tuple[Path, Path, Path]:
    data = root.parent / "plane-alerts-data" if root.name == "app" else root / ".community"
    data.mkdir(parents=True, exist_ok=True)
    return root / ".env", data / "installation.json", data / "setup-state.json"


def configure(root: Path, *, reconfigure: bool = False) -> dict[str, Any]:
    env_path, metadata_path, checkpoint_path = installation_paths(root)
    current = parse_env(env_path)
    state = {"step": "configuration", "updated_at": now_iso(), "secrets": "not-stored"}
    write_json_atomic(checkpoint_path, state)

    db_choice = choose("Database", ["Local Database (SQLite)", "MongoDB Atlas", "Existing MongoDB Server"], 1)
    updates: dict[str, str] = {"PLANE_ALERTS_COMMUNITY_SELF_HOST": "true"}
    if db_choice == 1:
        updates["DATABASE_BACKEND"] = "sqlite"
        data_dir = metadata_path.parent / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        updates["SQLITE_PATH"] = str(data_dir / "planealerts.db")
        updates["MONGO_URI"] = current.get("MONGO_URI", "mongodb://localhost:27017")
    else:
        updates["DATABASE_BACKEND"] = "mongodb"
        print("\nMongoDB URI format: mongodb+srv://USERNAME:PASSWORD@cluster.example.mongodb.net/")
        if db_choice == 2:
            print("In MongoDB Atlas, create a database user and allow this machine under Network Access before continuing.")
        uri = prompt_secret("MongoDB connection URI", existing=bool(current.get("MONGO_URI")))
        updates["MONGO_URI"] = uri or current.get("MONGO_URI", "")
        if not updates["MONGO_URI"].startswith(("mongodb://", "mongodb+srv://")):
            raise RuntimeError("MongoDB URI must start with mongodb:// or mongodb+srv://")

    print("\nTelegram bot setup: open @BotFather in Telegram, create/select a bot, then copy its token.")
    while True:
        token = prompt_secret("Telegram bot token", existing=bool(current.get("TELEGRAM_BOT_TOKEN")))
        token = token or current.get("TELEGRAM_BOT_TOKEN", "")
        ok, detail = validate_telegram(token)
        print(detail)
        if ok:
            updates["TELEGRAM_BOT_TOKEN"] = token
            break
        if choose("Telegram validation failed", ["Try again", "Cancel setup"], 1) == 2:
            raise KeyboardInterrupt

    open_choice = choose("OpenSky", ["Skip OpenSky for now", "Configure one OAuth2 client credential"], 1)
    if open_choice == 2:
        print("OpenSky uses OAuth2 client credentials. Enter clientId:clientSecret; the secret is never logged.")
        while True:
            credential = prompt_secret("OpenSky credential", existing=bool(current.get("OPENSKY_1")))
            credential = credential or current.get("OPENSKY_1", "")
            ok, detail = validate_opensky(credential)
            print(detail)
            if ok:
                updates["OPENSKY_1"] = credential
                break
            if choose("OpenSky validation failed", ["Try again", "Skip OpenSky"], 1) == 2:
                break

    receiver_choice = choose("Local ADS-B receiver", ["No local receiver", "Configure readsb/dump1090/ultrafeeder receiver"], 1)
    if receiver_choice == 2:
        url = input("Receiver base URL (example http://127.0.0.1:8080): ").strip()
        if not url.startswith(("http://", "https://")):
            raise RuntimeError("Receiver URL must start with http:// or https://")
        ok, detail = check_receiver(url)
        print(detail)
        if not ok and choose("Receiver validation failed", ["Continue with public providers only", "Cancel setup"], 1) == 2:
            raise KeyboardInterrupt
        if ok:
            updates["LOCAL_ADSB_URL"] = url.rstrip("/")
            updates["LOCAL_ADSB_RECEIVER_TYPE"] = "auto"
            print("Receiver location is used only for receiver coverage. Telegram profile locations remain separate.")
            lat = input("Receiver latitude (-90..90): ").strip()
            lon = input("Receiver longitude (-180..180): ").strip()
            coverage = input("Estimated receiver coverage in km [150]: ").strip() or "150"
            lat_f, lon_f, cov_f = float(lat), float(lon), float(coverage)
            if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180 and 1 <= cov_f <= 800):
                raise RuntimeError("Receiver coordinates/coverage are outside supported ranges")
            updates["LOCAL_ADSB_RECEIVER_LATITUDE"] = str(lat_f)
            updates["LOCAL_ADSB_RECEIVER_LONGITUDE"] = str(lon_f)
            updates["LOCAL_ADSB_RECEIVER_COVERAGE_KM"] = str(cov_f)

    updates.setdefault("POLL_INTERVAL_SECONDS", current.get("POLL_INTERVAL_SECONDS", "5") or "5")
    updates.setdefault("PORT", current.get("PORT", "8000") or "8000")
    updates.setdefault("LOG_LEVEL", current.get("LOG_LEVEL", "INFO") or "INFO")
    rendered = render_env(root / ".env.example", {**current, **updates})
    env_path.write_text(rendered, encoding="utf-8")
    harden_env(env_path)

    policy_choice = choose("Stable update policy", ["Automatic stable updates", "Manual updates"], 1)
    metadata = {
        "format": 1,
        "community": True,
        "repository": REPO,
        "installed_at": now_iso(),
        "platform": platform.system(),
        "architecture": platform.machine(),
        "database_backend": updates["DATABASE_BACKEND"],
        "update_policy": "automatic" if policy_choice == 1 else "manual",
        "receiver_configured": bool(updates.get("LOCAL_ADSB_URL")),
    }
    if metadata_path.exists():
        previous = json.loads(metadata_path.read_text(encoding="utf-8"))
        if isinstance(previous, dict):
            metadata["installed_at"] = previous.get("installed_at", metadata["installed_at"])
    write_json_atomic(metadata_path, metadata)
    write_json_atomic(checkpoint_path, {"step": "configuration-complete", "updated_at": now_iso(), "secrets": "not-stored"})
    return metadata


def service_name() -> str:
    return "Plane Alerts Community"


def install_service(root: Path, metadata: dict[str, Any]) -> None:
    env_path, _, _ = installation_paths(root)
    py = venv_python(root)
    if os.name == "nt":
        task = service_name()
        command = f'cmd /c "cd /d {root} && {py} -m uvicorn app.community_main_v55:app --host 0.0.0.0 --port 8000"'
        run(["schtasks", "/Create", "/TN", task, "/SC", "ONSTART", "/RU", "SYSTEM", "/RL", "HIGHEST", "/TR", command, "/F"])
        if metadata.get("update_policy") == "automatic":
            update_command = f'cmd /c "cd /d {root} && {py} scripts\\community_installer_v55.py --update --non-interactive --install-dir {root}"'
            run(["schtasks", "/Create", "/TN", f"{task} Updater", "/SC", "DAILY", "/ST", "04:15", "/RU", "SYSTEM", "/RL", "HIGHEST", "/TR", update_command, "/F"])
        run(["schtasks", "/Run", "/TN", task], check=False)
        return

    unit = Path("/etc/systemd/system/plane-alerts-community.service")
    unit.write_text(
        "[Unit]\nDescription=Plane Alerts Community\nAfter=network-online.target\nWants=network-online.target\n\n"
        "[Service]\nType=simple\nRestart=always\nRestartSec=5\n"
        f"WorkingDirectory={root}\nEnvironmentFile={env_path}\n"
        f"ExecStart={py} -m uvicorn app.community_main_v55:app --host 0.0.0.0 --port 8000\n"
        "\n[Install]\nWantedBy=multi-user.target\n", encoding="utf-8")
    if metadata.get("update_policy") == "automatic":
        update_service = Path("/etc/systemd/system/plane-alerts-community-update.service")
        update_timer = Path("/etc/systemd/system/plane-alerts-community-update.timer")
        update_service.write_text(
            "[Unit]\nDescription=Plane Alerts stable updater\n\n[Service]\nType=oneshot\n"
            f"WorkingDirectory={root}\nExecStart={py} {root / 'scripts/community_installer_v55.py'} --update --non-interactive --install-dir {root}\n",
            encoding="utf-8")
        update_timer.write_text(
            "[Unit]\nDescription=Plane Alerts daily stable update check\n\n[Timer]\nOnCalendar=*-*-* 04:15:00\nPersistent=true\nRandomizedDelaySec=900\n\n[Install]\nWantedBy=timers.target\n",
            encoding="utf-8")
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", "plane-alerts-community.service"])
    if metadata.get("update_policy") == "automatic":
        run(["systemctl", "enable", "--now", "plane-alerts-community-update.timer"])


def restart_service() -> None:
    if os.name == "nt":
        run(["schtasks", "/End", "/TN", service_name()], check=False)
        time.sleep(1)
        run(["schtasks", "/Run", "/TN", service_name()], check=False)
    else:
        run(["systemctl", "restart", "plane-alerts-community.service"], check=False)


def run_doctor(root: Path, *, offline: bool = False) -> bool:
    py = venv_python(root)
    cmd = [str(py), str(root / "scripts/planealerts"), "doctor"]
    if offline:
        cmd.append("--offline")
    return run(cmd, cwd=root, check=False).returncode == 0


def git_head(root: Path) -> str:
    return run(["git", "rev-parse", "HEAD"], cwd=root, capture=True).stdout.strip()


def current_tag(root: Path) -> str:
    result = run(["git", "describe", "--tags", "--exact-match", "HEAD"], cwd=root, check=False, capture=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def backup_for_update(root: Path) -> Path:
    env_path, metadata_path, _ = installation_paths(root)
    backup_root = metadata_path.parent / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root.mkdir(parents=True, exist_ok=True)
    if env_path.exists():
        shutil.copy2(env_path, backup_root / ".env")
    values = parse_env(env_path)
    if values.get("DATABASE_BACKEND", "").lower() == "sqlite":
        db_path = Path(values.get("SQLITE_PATH", ""))
        if db_path.exists():
            target = backup_root / "planealerts.db"
            source = sqlite3.connect(str(db_path))
            destination = sqlite3.connect(str(target))
            try:
                source.backup(destination)
            finally:
                destination.close(); source.close()
    write_json_atomic(backup_root / "rollback.json", {"commit": git_head(root), "tag": current_tag(root), "created_at": now_iso()})
    return backup_root


def local_changes(root: Path) -> bool:
    output = run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root, capture=True).stdout.strip()
    allowed = {".env"}
    return any(line[3:].strip() not in allowed for line in output.splitlines() if len(line) >= 4)


def update(root: Path) -> bool:
    release = latest_stable_release()
    tag = str(release["tag_name"])
    target_version = semver(tag)
    installed = current_tag(root)
    if installed and semver(installed) >= target_version:
        print(f"Plane Alerts {installed} is already the newest stable release.")
        return True
    if local_changes(root):
        raise RuntimeError("Local changes were detected. Plane Alerts will not overwrite them automatically.")
    if not run_doctor(root, offline=True):
        raise RuntimeError("Offline doctor failed; update refused before changing the installation")

    backup = backup_for_update(root)
    rollback = json.loads((backup / "rollback.json").read_text(encoding="utf-8"))
    old_commit = str(rollback["commit"])
    print(f"Updating from {installed or old_commit[:12]} to {tag}...")
    try:
        run(["git", "fetch", "--force", "origin", f"refs/tags/{tag}:refs/tags/{tag}"], cwd=root)
        target_commit = run(["git", "rev-list", "-n", "1", tag], cwd=root, capture=True).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", target_commit):
            raise RuntimeError("Stable tag did not resolve to an immutable commit")
        run(["git", "checkout", "--detach", target_commit], cwd=root)
        install_dependencies(root)
        if not run_doctor(root, offline=True):
            raise RuntimeError("Target release failed offline doctor")
        restart_service()
        time.sleep(3)
        if not run_doctor(root, offline=False):
            raise RuntimeError("Target release failed live doctor")
        print(f"Updated successfully to {tag} ({target_commit[:12]}).")
        return True
    except Exception:
        print("Update validation failed. Rolling back to the previously verified commit...")
        run(["git", "checkout", "--detach", old_commit], cwd=root, check=False)
        if (backup / ".env").exists():
            shutil.copy2(backup / ".env", root / ".env")
        values = parse_env(root / ".env")
        db_path = Path(values.get("SQLITE_PATH", "")) if values.get("SQLITE_PATH") else None
        if db_path and (backup / "planealerts.db").exists():
            db_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup / "planealerts.db", db_path)
        install_dependencies(root)
        restart_service()
        raise


def repair(root: Path) -> None:
    install_dependencies(root)
    env_path, metadata_path, _ = installation_paths(root)
    if not env_path.exists():
        configure(root, reconfigure=True)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {"update_policy": "manual"}
    install_service(root, metadata)
    if not run_doctor(root, offline=False):
        raise RuntimeError("Repair completed but doctor still reports a failure")


def uninstall(root: Path, *, keep_data: bool) -> None:
    if os.name == "nt":
        run(["schtasks", "/Delete", "/TN", service_name(), "/F"], check=False)
        run(["schtasks", "/Delete", "/TN", f"{service_name()} Updater", "/F"], check=False)
    else:
        run(["systemctl", "disable", "--now", "plane-alerts-community.service"], check=False)
        run(["systemctl", "disable", "--now", "plane-alerts-community-update.timer"], check=False)
        for path in ("/etc/systemd/system/plane-alerts-community.service", "/etc/systemd/system/plane-alerts-community-update.service", "/etc/systemd/system/plane-alerts-community-update.timer"):
            Path(path).unlink(missing_ok=True)
        run(["systemctl", "daemon-reload"], check=False)
    if not keep_data:
        env_path, metadata_path, _ = installation_paths(root)
        env_path.unlink(missing_ok=True)
        shutil.rmtree(metadata_path.parent, ignore_errors=True)
    print("Plane Alerts background service removed. Application files may now be deleted safely.")


def support_bundle(root: Path) -> Path:
    env_path, metadata_path, _ = installation_paths(root)
    values = parse_env(env_path)
    payload = {
        "generated_at": now_iso(), "repository": REPO, "commit": git_head(root), "tag": current_tag(root),
        "platform": platform.platform(), "architecture": platform.machine(),
        "config": {key: redact_value(key, value) for key, value in values.items()},
    }
    if metadata_path.exists():
        payload["installation"] = json.loads(metadata_path.read_text(encoding="utf-8"))
    out = metadata_path.parent / "support-bundle.json"
    write_json_atomic(out, payload)
    print(f"Sanitized support bundle: {out}")
    return out


def install(root: Path) -> None:
    print("\nPlane Alerts Setup\n==================")
    print(f"System: {platform.system()} {platform.release()} / {platform.machine()}")
    if sys.version_info < (3, 11):
        raise RuntimeError("Plane Alerts requires Python 3.11 or newer")
    install_dependencies(root)
    metadata = configure(root)
    if not run_doctor(root, offline=True):
        raise RuntimeError("Configuration validation failed. Service was not installed.")
    install_service(root, metadata)
    time.sleep(2)
    if not run_doctor(root, offline=False):
        raise RuntimeError("Plane Alerts started but live doctor validation failed")
    env_path, metadata_path, checkpoint_path = installation_paths(root)
    metadata.update({"installed_tag": current_tag(root), "installed_commit": git_head(root), "verified_at": now_iso()})
    write_json_atomic(metadata_path, metadata)
    write_json_atomic(checkpoint_path, {"step": "ready", "updated_at": now_iso(), "secrets": "not-stored"})
    print("\nInstallation complete. Configure alert locations from Telegram profiles; receiver coordinates are separate.")


def menu(root: Path) -> None:
    while True:
        print("\nPlane Alerts Setup\n==================")
        option = choose("Choose an action", ["Install Plane Alerts", "Repair Existing Installation", "Change Configuration", "Update Plane Alerts", "Run Diagnostics", "Uninstall", "Exit"], 1)
        if option == 1: install(root); return
        if option == 2: repair(root); return
        if option == 3:
            metadata = configure(root, reconfigure=True); install_service(root, metadata); restart_service(); return
        if option == 4: update(root); return
        if option == 5: run_doctor(root, offline=False); support_bundle(root); return
        if option == 6:
            keep = choose("Keep configuration and database?", ["Keep data", "Remove data"], 1) == 1
            uninstall(root, keep_data=keep); return
        return


def self_test() -> None:
    assert semver("v5.5.1") == (5, 5, 1)
    assert _parse_opensky("client:secret") == ("client", "secret")
    assert redact_value("TELEGRAM_BOT_TOKEN", "secret") == "***"
    print("COMMUNITY_INSTALLER_SELF_TEST=ok")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plane Alerts v5.5 community installer")
    parser.add_argument("--install-dir", default=".")
    parser.add_argument("--platform", default="auto")
    parser.add_argument("--non-interactive", action="store_true")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--install", action="store_true")
    group.add_argument("--repair", action="store_true")
    group.add_argument("--reconfigure", action="store_true")
    group.add_argument("--update", action="store_true")
    group.add_argument("--doctor", action="store_true")
    group.add_argument("--uninstall", action="store_true")
    group.add_argument("--support-bundle", action="store_true")
    group.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.install_dir).expanduser().resolve()
    try:
        if args.self_test: self_test(); return 0
        if args.install: install(root); return 0
        if args.repair: repair(root); return 0
        if args.reconfigure:
            metadata = configure(root, reconfigure=True); install_service(root, metadata); restart_service(); return 0
        if args.update: return 0 if update(root) else 2
        if args.doctor: return 0 if run_doctor(root, offline=False) else 2
        if args.uninstall: uninstall(root, keep_data=True); return 0
        if args.support_bundle: support_bundle(root); return 0
        if args.non_interactive:
            parser.error("--non-interactive requires an explicit operation")
        menu(root)
        return 0
    except KeyboardInterrupt:
        print("\nSetup cancelled. Existing configuration was not deliberately removed.")
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
