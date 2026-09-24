# Self-hosting Plane Alerts

Plane Alerts v5.5.0 supports reproducible self-hosting on x86-64 and ARM64 Linux, including Raspberry Pi 4/5-class systems running a 64-bit OS. The alert-critical prediction path is the same deterministic/statistical code used in production.

## Recommended: Docker Compose

Requirements:

- Docker Engine with Compose v2
- a Telegram bot token from BotFather
- outbound HTTPS access for public ADS-B providers and Telegram
- at least 2 GB RAM; 4 GB is recommended on Raspberry Pi when MongoDB shares the host

Copy `.env.example` to `.env` and set at minimum `TELEGRAM_BOT_TOKEN`. A blank `ADMIN_TELEGRAM_ID` is valid. Docker Compose supplies its own local MongoDB URI, so you do not need an external Mongo service for this setup. Do not commit `.env`.

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

`planealerts doctor` checks Python/version compatibility, required configuration, contradictory settings, MongoDB reachability, stored coordinate ranges without printing coordinates, Telegram authentication, public ADS-B provider reachability, and the optional local receiver. A failed optional provider probe is reported as a warning; required configuration or database/Telegram failures are failures.

## Prediction Lab persistence

v5.5.0 removes new high-volume Prediction Lab audit/evaluation writes from MongoDB. Evidence is written asynchronously to `PREDICTION_LAB_ROOT`, which is `/data/prediction_lab` in the supplied Compose configuration.

For a single self-hosted instance, keeping the persistent volume is sufficient for runtime durability. The repository's Railway-to-Git synchronization workflow is production-operations tooling and is not required for ordinary self-hosting.

The historical Mongo collections `prediction_lab_audit`, `prediction_shadow_evaluations` and `prediction_sentinel_routes` are exported and integrity-checked before retirement. Normal application collections and `flight_route_samples` remain in MongoDB.

When backing up an installation, preserve both `mongo_data` and `prediction_lab_data` if Prediction Lab evidence/history matters to you.

## Raspberry Pi / ARM64

Use a 64-bit Raspberry Pi OS or another ARM64 Linux distribution. Verify the host reports `aarch64`/`arm64`, then run the same Compose commands above. The production Dockerfile is limited deliberately to `amd64` and `arm64`; CI builds both architectures so unsupported architectures fail clearly rather than failing later at runtime.

If the Pi also runs readsb/dump1090/ultrafeeder, set `LOCAL_ADSB_URL` to its HTTP aircraft feed and keep `LOCAL_ADSB_RECEIVER_TYPE=auto` unless you need an explicit parser. Local ADS-B remains optional and public providers remain available as fallback according to the existing provider-resilience rules.

## Native Python installation

Python 3.11+ is required. A virtual environment is strongly recommended:

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python scripts/build_airport_database.py
TELEGRAM_BOT_TOKEN='your-token' MONGO_URI='mongodb://127.0.0.1:27017' PREDICTION_LAB_ROOT='/var/lib/plane-alerts/prediction_lab' python scripts/planealerts doctor
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

For native installs, create the Prediction Lab root on persistent local storage and grant the service account read/write access. Do not use `/tmp` for production evidence.

The bare `planealerts` executable is installed inside the project Docker image. From a native source checkout use `python scripts/planealerts doctor` or `python -m app.doctor_v49 doctor`; optionally place your own symlink to `scripts/planealerts` on `PATH`.

## Configuration validation

Plane Alerts validates typed settings at startup. Important rules include:

- `TELEGRAM_BOT_TOKEN` is required for the Telegram runtime.
- `ADMIN_TELEGRAM_ID` may be blank; a nonblank value must be numeric.
- `ADMIN_PASSWORD` must be configured before sensitive web-admin APIs can be used; blank fails closed.
- `MONGO_URI` must use `mongodb://` or `mongodb+srv://`.
- production monitoring is designed around a five-second `POLL_INTERVAL_SECONDS`.
- `LOCAL_ADSB_URL`, when present, must be a valid HTTP(S) receiver URL; credentials belong in `LOCAL_ADSB_AUTH_HEADER`, not in the URL.
- `LOCAL_ADSB_AUTH_HEADER` without `LOCAL_ADSB_URL` is contradictory.
- exact observer coordinates are never printed by the doctor command.
- `PREDICTION_LAB_ROOT`, when overridden, should be a persistent writable path and must not contain credentials in its pathname.

The optional Google Contrails integration continues to use `GOOGLE_CONTRAILS_API_KEY` where configured. It is enrichment only and must not control prediction or alert timing.

## Health and recovery

Container rollout readiness is based on `/ready`; `/health` remains a liveness/diagnostic endpoint that always returns structured health information. MongoDB has its own health check. Both services use `restart: unless-stopped`.

`/ready` requires a verified monitoring configuration, a live worker and an initialized Telegram runtime. A warmed Plane Alerts process can remain ready while MongoDB is temporarily degraded because v4.8 last-known-good configuration/persistence isolation remains active. A cold process does not fabricate missing configuration and therefore does not pass readiness.

To inspect status:

```bash
docker compose ps
docker compose logs --tail=200 plane-alerts
docker compose exec plane-alerts planealerts doctor
curl -fsS http://127.0.0.1:${PLANE_ALERTS_PORT:-8000}/ready
```

Do not interpret missing ADS-B coverage as proof that a route did or did not occur.

## Backup and migration

Before a version upgrade, back up MongoDB and persistent local data. Keep the `.env` file and local receiver configuration separately; do not put secrets into backups intended for sharing.

For v5.5.0 specifically, keep the pre-upgrade Mongo backup until the Prediction Lab migration reports verified export counts and the application is production-healthy. The file archive is additive evidence; normal application data remains in MongoDB.

Upgrade one release at a time when crossing documented migration boundaries:

```bash
git fetch --tags
git checkout <verified-release-tag-or-commit>
docker compose build --no-cache plane-alerts
docker compose run --rm plane-alerts planealerts doctor --offline
docker compose up -d
```

## Rollback

Keep the previously verified Git tag/commit before upgrading. To roll back application code, check out that exact tag/commit and rebuild only the Plane Alerts image. Do not delete `mongo_data` or `prediction_lab_data`.

For v5.5.0, the immediate application rollback target is v5.4.3 commit `2546e0ff854ce32511b4c358ba162e842397ff66`.

If the v5.5 migration has already retired the old Prediction Lab Mongo collections, v5.4.3 can still use normal application Mongo data, but the migrated historical Lab evidence is not automatically imported back into MongoDB. Restore a pre-upgrade backup only if you specifically need the old Mongo-backed Lab storage while testing a rollback; do not overwrite newer normal user/configuration data casually.

## Reporting a problem

Use the repository issue tracker: https://github.com/xtenrore/Plane-Alerts/issues/new/choose

Include Plane Alerts version, prediction version, platform/architecture, `planealerts doctor --json` output, and relevant redacted logs. Never attach Telegram tokens, API keys, Mongo credentials, local ADS-B auth headers, exact private observer coordinates, Prediction Lab privacy salts, or other secrets.
