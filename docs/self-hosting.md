# Self-hosting Plane Alerts

Plane Alerts v4.9 supports reproducible self-hosting on x86-64 and ARM64 Linux, including Raspberry Pi 4/5-class systems running a 64-bit OS. The alert-critical prediction path is the same deterministic/statistical code used in production.

## Recommended: Docker Compose

Requirements:

- Docker Engine with Compose v2
- a Telegram bot token from BotFather
- outbound HTTPS access for public ADS-B providers and Telegram
- at least 2 GB RAM; 4 GB is recommended on Raspberry Pi when MongoDB shares the host

Copy `.env.example` to `.env` and set at minimum `TELEGRAM_BOT_TOKEN`. Docker Compose supplies its own local MongoDB URI, so you do not need an external Mongo service for this setup. Do not commit `.env`.

Run:

```bash
docker compose pull mongo
docker compose build plane-alerts
docker compose run --rm plane-alerts planealerts doctor --offline
docker compose up -d
docker compose ps
```

The app is exposed on port `8000` by default. Set `PLANE_ALERTS_PORT` before `docker compose up` to choose a different host port. Mongo data is stored in the named `mongo_data` volume and survives normal container replacement/restarts.

Once running, validate the installation without revealing secrets:

```bash
docker compose exec plane-alerts planealerts doctor
docker compose exec plane-alerts planealerts doctor --json
```

`planealerts doctor` checks Python/version compatibility, required configuration, contradictory settings, MongoDB reachability, stored coordinate ranges without printing coordinates, Telegram authentication, public ADS-B provider reachability, and the optional local receiver. A failed optional provider probe is reported as a warning; required configuration or database/Telegram failures are failures.

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
TELEGRAM_BOT_TOKEN='your-token' MONGO_URI='mongodb://127.0.0.1:27017' planealerts doctor
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

For a source checkout where `planealerts` has not been installed into `PATH`, use `python -m app.doctor_v49 doctor` or `python scripts/planealerts doctor`.

## Configuration validation

Plane Alerts validates typed settings at startup. v4.9 additionally checks operational contradictions with `planealerts doctor`. Important rules include:

- `TELEGRAM_BOT_TOKEN` is required for the Telegram runtime.
- `MONGO_URI` must use `mongodb://` or `mongodb+srv://`.
- production monitoring is designed around a five-second `POLL_INTERVAL_SECONDS`.
- `LOCAL_ADSB_URL`, when present, must be a valid HTTP(S) receiver URL; credentials belong in `LOCAL_ADSB_AUTH_HEADER`, not in the URL.
- `LOCAL_ADSB_AUTH_HEADER` without `LOCAL_ADSB_URL` is contradictory.
- exact observer coordinates are never printed by the doctor command.

The optional Google Contrails integration continues to use `GOOGLE_CONTRAILS_API_KEY` where configured. It is enrichment only and must not control prediction or alert timing.

## Health and recovery

Container health is based on `/health`. MongoDB has its own health check. Both services use `restart: unless-stopped`. A warmed Plane Alerts process keeps the v4.8 last-known-good configuration/persistence isolation behavior during Mongo trouble; a cold process still refuses to fabricate missing configuration.

To inspect status:

```bash
docker compose ps
docker compose logs --tail=200 plane-alerts
docker compose exec plane-alerts planealerts doctor
```

Do not interpret missing ADS-B coverage as proof that a route did or did not occur.

## Backup and migration

Before a version upgrade, back up MongoDB or use the repository's allow-listed Plane Alerts export tooling. Keep the `.env` file and local receiver configuration separately; do not put secrets into backups intended for sharing. Schema migrations are versioned and must remain idempotent/additive unless a release explicitly documents otherwise.

Upgrade one release at a time when crossing documented migration boundaries:

```bash
git fetch --tags
git checkout v4.9.0
docker compose build --no-cache plane-alerts
docker compose run --rm plane-alerts planealerts doctor --offline
docker compose up -d
```

## Rollback

Keep the previously verified Git tag/commit before upgrading. To roll back application code, check out that exact tag/commit and rebuild only the Plane Alerts image. Do not delete the Mongo volume. If a future release documents a destructive/non-backward-compatible migration, follow that release's migration notes and restore a compatible database backup rather than forcing older code against a newer schema.

For the v4.9 release family, the immediate rollback target is the verified v4.8.1 commit `bc7f34f98beb2e7f0838d3aac6b9fdbf1ae24ecb`.

## Reporting a problem

Use the repository issue tracker: https://github.com/xtenrore/Plane-Alerts/issues/new/choose

Include Plane Alerts version, prediction version, platform/architecture, `planealerts doctor --json` output, and relevant redacted logs. Never attach Telegram tokens, API keys, Mongo credentials, local ADS-B auth headers, exact private observer coordinates, or other secrets.
