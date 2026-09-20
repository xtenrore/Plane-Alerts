from __future__ import annotations

from pathlib import Path

import yaml

from app.config import Settings
from app.doctor_v49 import static_checks
from app.version import PREDICTION_VERSION, VERSION

ROOT = Path(__file__).resolve().parents[1]


def _config(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "telegram_bot_token": "123456:test-token",
        "mongo_uri": "mongodb://localhost:27017",
        "default_radius_km": 15.0,
        "poll_interval_seconds": 5,
        "local_adsb_url": "",
        "local_adsb_auth_header": "",
        "agy_worker_url": "",
        "agy_worker_token": "",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_release_identity_v49_or_later_has_canonical_prediction_identifier() -> None:
    assert tuple(int(part) for part in VERSION.split(".")) >= (4, 9, 0)
    assert isinstance(PREDICTION_VERSION, str) and PREDICTION_VERSION.strip()


def test_runtime_requirements_are_exactly_pinned() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").strip()
    assert requirements.endswith("-r requirements.lock")

    lines = [
        line.strip()
        for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert len(lines) >= 20
    assert all("==" in line for line in lines)
    assert not any(">=" in line or "~=" in line for line in lines)


def test_docker_compose_has_health_restart_and_persistent_mongo() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    app = services["plane-alerts"]
    mongo = services["mongo"]

    assert app["restart"] == "unless-stopped"
    assert mongo["restart"] == "unless-stopped"
    assert "healthcheck" in app and "healthcheck" in mongo
    assert app["depends_on"]["mongo"]["condition"] == "service_healthy"
    assert any("mongo_data:/data/db" in str(value) for value in mongo["volumes"])
    assert "mongo_data" in compose["volumes"]


def test_dockerfile_installs_locked_dependencies_doctor_health_and_arm64_guard() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY requirements.txt requirements.lock ./" in dockerfile
    assert "planealerts" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "arm64" in dockerfile and "amd64" in dockerfile
    assert "/health" in dockerfile


def test_self_hosting_docs_and_public_issue_path_exist() -> None:
    text = (ROOT / "docs" / "self-hosting.md").read_text(encoding="utf-8")
    for phrase in (
        "Docker Compose",
        "Raspberry Pi",
        "ARM64",
        "planealerts doctor",
        "Rollback",
        "issues/new/choose",
    ):
        assert phrase in text
    assert (ROOT / "LICENSE").exists()
    assert (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").exists()


def test_doctor_static_checks_are_secret_safe_and_accept_valid_configuration() -> None:
    secret = "123456:test-token"
    checks = static_checks(_config(telegram_bot_token=secret))
    assert not any(check.status == "fail" for check in checks)
    rendered = "\n".join(check.detail for check in checks)
    assert secret not in rendered
    assert "mongodb://localhost:27017" not in rendered


def test_doctor_reports_configuration_contradictions_without_secret_values() -> None:
    secret = "Bearer local-secret-value"
    checks = static_checks(
        _config(
            local_adsb_url="",
            local_adsb_auth_header=secret,
            agy_worker_url="",
            agy_worker_token="worker-secret",
        )
    )
    contradiction = next(check for check in checks if check.name == "configuration-contradictions")
    assert contradiction.status == "fail"
    assert "LOCAL_ADSB_AUTH_HEADER requires LOCAL_ADSB_URL" in contradiction.detail
    assert "AGY_WORKER_TOKEN requires AGY_WORKER_URL" in contradiction.detail
    assert secret not in contradiction.detail
    assert "worker-secret" not in contradiction.detail


def test_doctor_warns_when_monitor_interval_differs_from_production_baseline() -> None:
    checks = static_checks(_config(poll_interval_seconds=10))
    cadence = next(check for check in checks if check.name == "monitor-cadence")
    assert cadence.status == "warn"
    assert "production baseline is 5 seconds" in cadence.detail
