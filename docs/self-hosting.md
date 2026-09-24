# Self-hosting Plane Alerts

Plane Alerts v5.5.1 supports guided and manual self-hosting on Linux x86-64, Linux ARM64, Raspberry Pi 4/5 with a 64-bit OS, Windows x64 and Windows ARM64. The alert-critical prediction path remains the same deterministic/statistical code used in production.

## Recommended: guided stable-release installer

Download the installer for your platform from the newest stable GitHub release. Stable v5.5.x releases publish six installer entry points plus `SHA256SUMS.txt`:

- `setup-windows.bat` — Windows x64/AMD64
- `setup-windows-arm64.bat` — Windows ARM64
- `setup.bat` — Windows launcher
- `setup-linux.sh` — Linux x64/AMD64
- `setup-linux-arm64.sh` — Linux ARM64
- `setup-raspberry-pi.sh` — Raspberry Pi 4/5 64-bit

The guided installer checks platform compatibility, installs required prerequisites where supported, clones the newest stable semantic tag rather than `main`, creates a virtual environment, validates required configuration, and supports repair/reconfigure/update/rollback flows.

Native guided installs may use either:

- **Local Database (SQLite)** — the recommended simple self-host option; stored on persistent local data and backed up before updater changes.
- **MongoDB** — Atlas or another existing MongoDB server.

The installer's local ADS-B receiver coordinates describe receiver coverage only. They are separate from Telegram observer/profile locations and are not placed in Git Prediction Lab evidence.

## Docker Compose

Requirements:

- Docker Engine with Compose v2
- a Telegram bot token from BotFather
- outbound HTTPS access for public ADS-B providers and Telegram
- at least 2 GB RAM; 4 GB is recommended on Raspberry Pi when MongoDB shares the host

Copy `.env.example` to `.env` and set at minimum `TELEGRAM_BOT_TOKEN`. A blank `ADMIN_TELEGRAM_ID` is valid. Although `.env.example` defaults native self-hosting to SQLite, Docker Compose explicitly sets `DATABASE_BACKEND=mongodb` and supplies its bundled MongoDB URI. Do not commit `.env`.

The web administration API is deliberately disabled while `ADMIN_PASSWORD` is blank. Set a strong `ADMIN_PASSWORD` before using the admin dashboard/API; a blank password does not create anonymous root access.

Run:

```bash
docker compose pull mongo
docker compose build plane-alerts
docker compose run --rm plane-alerts planealerts doctor --offline
docker compose up -d
docker compose ps
```

The app is exposed on port `8000` by default. Set `PLANE_ALERTS_PORT` before `docker compose up` to choose a different host port.

Two named volumes are persistent:

- `mongo_data:/data/db` — normal Plane Alerts application/user/configuration data.
- `prediction_lab_data:/data/prediction_lab` — v5.5 Prediction Lab audit/shadow evidence and migration archive.

Both survive normal container replacement/restarts. Do not place either volume inside an updater-replaced source directory.

Once running, validate the installation without revealing secrets:

```bash
docker compose exec plane-alerts planealerts doctor
docker compose exec plane-alerts planealerts doctor --json
```

`planealerts doctor` checks Python/version compatibility, required configuration, contradictory settings, database reachability, stored coordinate ranges without printing coordinates, Telegram authentication, public ADS-B provider reachability, and the optional local receiver. A failed optional provider probe is reported as a warning; required configuration or database/Telegram failures are failures.

## Prediction Lab persistence

v5.5.1 removes new high-volume Prediction Lab audit/evaluation writes from MongoDB. Evidence is written asynchronously to `PREDICTION_LAB_ROOT`, which is `/data/prediction_lab` in the supplied Compose configuration and a persistent community-data path for guided native installs.

For a single self-hosted instance, keeping that persistent path is sufficient for runtime durability. The repository's Railway-to-Git synchronization workflow is production-operations tooling and is not required for ordinary self-hosting.

On Mongo-backed upgrades, the historical collections `prediction_lab_audit`, `prediction_shadow_evaluations` and `prediction_sentinel_routes` are exported with source/export count and SHA-256 integrity verification before retirement. Normal application collections and `flight_route_samples` remain in MongoDB.

The migration is wired into actual Mongo startup. An incomplete/unverified migration fails closed. Atlas quota-full index maintenance can be deferred only long enough to complete the verified Prediction Lab export/drop; normal indexes must then succeed before storage startup is considered ready.

When backing up an installation, preserve `mongo_data` and `prediction_lab_data` for Compose. Guided SQLite installs should preserve the installer-managed community data directory, which contains SQLite state, installer metadata/checkpoints and Prediction Lab evidence.

## Raspberry Pi / ARM64

Use a 64-bit Raspberry Pi OS or another ARM64 Linux distribution. The dedicated Raspberry Pi installer accepts supported Pi 4/5 64-bit systems and rejects unsupported architecture clearly. The production Dockerfile is deliberately limited to `amd64` and `arm64`; CI builds both architectures.

If the Pi also runs readsb/dump1090/ultrafeeder, configure its HTTP aircraft feed during guided setup or set `LOCAL_ADSB_URL`. Keep `LOCAL_ADSB_RECEIVER_TYPE=auto` unless an explicit parser is required. Local ADS-B remains optional and public providers remain available as fallback according to existing provider-resilience rules.

## Manual native Python installation

Python 3.11+ is required. A virtual environment is strongly recommended:

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python scripts/build_airport_database.py
```

For local SQLite:

```bash
DATABASE_BACKEND=sqlite \
SQLITE_PATH="$PWD/.community/data/planealerts.db" \
PREDICTION_LAB_ROOT="$PWD/.community/prediction_lab" \
TELEGRAM_BOT_TOKEN='your-token' \
python scripts/planealerts doctor
```

For MongoDB:

```bash
DATABASE_BACKEND=mongodb \
MONGO_URI='mongodb://127.0.0.1:27017' \
PREDICTION_LAB_ROOT='/var/lib/plane-alerts/prediction_lab' \
TELEGRAM_BOT_TOKEN='your-token' \
python scripts/planealerts doctor
```

Then start the application with:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Use a persistent writable Prediction Lab path; do not use `/tmp` for production evidence. The bare `planealerts` executable is installed inside the project Docker image. From a native source checkout use `python scripts/planealerts doctor` or `python -m app.doctor_v49 doctor`.

## Configuration validation

Important rules include:

- `TELEGRAM_BOT_TOKEN` is required for the Telegram runtime.
- `DATABASE_BACKEND` must be `sqlite` or `mongodb`.
- `SQLITE_PATH` should be persistent when SQLite is selected.
- `MONGO_URI` must use `mongodb://` or `mongodb+srv://` when MongoDB is selected.
- `ADMIN_TELEGRAM_ID` may be blank; a nonblank value must be numeric.
- `ADMIN_PASSWORD` must be configured before sensitive web-admin APIs can be used; blank fails closed.
- production monitoring is designed around a five-second `POLL_INTERVAL_SECONDS`.
- `LOCAL_ADSB_URL`, when present, must be a valid HTTP(S) receiver URL; credentials belong in `LOCAL_ADSB_AUTH_HEADER`, not in the URL.
- `LOCAL_ADSB_AUTH_HEADER` without `LOCAL_ADSB_URL` is contradictory.
- exact observer coordinates are never printed by the doctor command.
- `PREDICTION_LAB_ROOT`, when overridden, should be a persistent writable path and must not contain credentials in its pathname.

Optional Gemini/Groq/contrail enrichment remains non-authoritative and is not required for the deterministic alert path.

## Health and recovery

Container rollout readiness is based on `/ready`; `/health` remains a liveness/diagnostic endpoint that always returns structured health information. Both services use `restart: unless-stopped`.

`/ready` requires a verified monitoring configuration, a live worker and an initialized Telegram runtime. A warmed process can remain ready while database persistence is temporarily degraded because v4.8 last-known-good configuration/persistence isolation remains active. A cold process does not fabricate missing configuration and therefore does not pass readiness.

To inspect Compose status:

```bash
docker compose ps
docker compose logs --tail=200 plane-alerts
docker compose exec plane-alerts planealerts doctor
curl -fsS http://127.0.0.1:${PLANE_ALERTS_PORT:-8000}/ready
```

Do not interpret missing ADS-B coverage as proof that a route did or did not occur.

## Backup, update and Rollback

Before a version upgrade, back up normal application storage and persistent local data. Keep `.env` and local receiver configuration separately; do not put secrets into backups intended for sharing.

For v5.5.1, keep the pre-upgrade Mongo backup until the Prediction Lab migration reports verified export counts and the application is production-healthy. The file archive is additive evidence; normal application data remains in the configured normal database.

The guided updater discovers only the newest stable semantic GitHub release, refuses dirty tracked source changes, backs up persistent configuration/local SQLite state, validates the new release with doctor checks, and rolls back to the previous stable tag if the update gate fails. It never follows `main` as the installed stable version.

Manual Compose upgrades should keep the previous verified tag/commit and both named volumes:

```bash
git fetch --tags
git checkout <verified-release-tag-or-commit>
docker compose build --no-cache plane-alerts
docker compose run --rm plane-alerts planealerts doctor --offline
docker compose up -d
```

The immediate rollback code target for this architecture release is v5.4.3 commit `2546e0ff854ce32511b4c358ba162e842397ff66`.

If the v5.5.1 migration has already retired the old Prediction Lab Mongo collections, v5.4.3 can still use normal application Mongo data, but migrated historical Lab evidence is not automatically imported back into MongoDB. Restore a pre-upgrade backup only if you specifically need the old Mongo-backed Lab storage while testing a rollback; do not overwrite newer normal user/configuration data casually.

## Reporting a problem

Use the repository issue tracker: https://github.com/xtenrore/Plane-Alerts/issues/new/choose

Include Plane Alerts version, prediction version, platform/architecture, `planealerts doctor --json` output, and relevant redacted logs. Never attach Telegram tokens, API keys, Mongo credentials, local ADS-B auth headers, exact private observer coordinates, Prediction Lab privacy salts, or other secrets.
