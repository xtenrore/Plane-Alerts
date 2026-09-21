# Self-hosting Plane Alerts v5.5

Plane Alerts v5.5 provides a guided community installer for Windows, Linux and Raspberry Pi. This is the recommended self-hosting path. It uses the same deterministic/statistical Plane Alerts prediction code as the hosted project while keeping the community runtime separate from the owner's Railway deployment and private AGY tooling.

The physical prediction identifier remains `5.3-3d-proximity-age-aware`.

## Pick the correct installer

Download the matching file from the latest stable GitHub Release:

| Machine | Installer |
| --- | --- |
| Windows 10/11 or Windows Server, Intel/AMD 64-bit | `setup.bat` or `setup-windows.bat` |
| Windows ARM64 | `setup.bat` or `setup-windows-arm64.bat` |
| Debian/Ubuntu-family Linux, x86-64 | `setup-linux.sh` |
| Debian/Ubuntu-family Linux, ARM64 | `setup-linux-arm64.sh` |
| Raspberry Pi 4/5 with a 64-bit OS | `setup-raspberry-pi.sh` |

`setup.bat` detects Windows architecture. When the matching platform script is not beside it, it downloads that script plus `SHA256SUMS.txt` from the latest stable release and refuses to execute the file if the SHA-256 checksum does not match.

The platform-specific entry points themselves resolve the latest non-draft, non-prerelease semantic-version release and clone that exact tag. They do not clone repository `main` as an update mechanism.

## Requirements

The installer bootstraps the normal software prerequisites where the supported platform package manager is available. You still need:

- outbound HTTPS access to GitHub, Telegram and the public ADS-B providers you use;
- a Telegram bot token from BotFather;
- sufficient local disk for Plane Alerts, its virtual environment, airport reference data, logs/backups and optional SQLite database;
- a machine that can remain powered on if you want continuous alerts.

For Raspberry Pi, use a 64-bit OS. Pi 4/5-class hardware is the primary validation target. The installer warns rather than silently assuming unsupported Pi hardware.

## Starting setup

### Windows

Run the downloaded `.bat` as Administrator. `setup.bat` automatically selects the x64 or ARM64 installer.

The Windows installer uses Windows Package Manager (`winget`) to install Git and Python 3.11 when they are not already available through the expected commands. It installs Plane Alerts under `%ProgramData%\PlaneAlerts` by default and uses Windows Task Scheduler for the always-on runtime and optional daily stable updater.

### Linux x64

```bash
chmod +x setup-linux.sh
sudo ./setup-linux.sh
```

The default installation directory is `/opt/plane-alerts`. Supported Debian/Ubuntu-family systems use `apt` for Git, Python and build prerequisites. The runtime is installed as `plane-alerts-community.service` under systemd.

### Linux ARM64

```bash
chmod +x setup-linux-arm64.sh
sudo ./setup-linux-arm64.sh
```

This entry point rejects non-ARM64 systems rather than silently installing the wrong path.

### Raspberry Pi

```bash
chmod +x setup-raspberry-pi.sh
sudo ./setup-raspberry-pi.sh
```

The Pi installer verifies 64-bit ARM, checks the hardware model, reports RAM/free-disk information, and looks for common readsb/dump1090/ultrafeeder services/data before opening the normal guided setup.

## Guided setup menu

Re-running a platform installer against an existing official Plane Alerts checkout opens the setup/maintenance menu instead of blindly reinstalling files.

The menu provides:

1. Install Plane Alerts
2. Repair Existing Installation
3. Change Configuration
4. Update Plane Alerts
5. Run Diagnostics
6. Uninstall
7. Exit

The shared installer keeps non-secret checkpoint state so interrupted setup can be diagnosed without storing Telegram/OpenSky/MongoDB credentials in checkpoint files.

## Database selection

### Local Database — recommended

Choose **Local Database (SQLite)** for the simplest personal self-hosting setup. The installer creates a persistent SQLite database in the Plane Alerts community data area and writes:

```env
DATABASE_BACKEND=sqlite
SQLITE_PATH=<persistent-local-path>/planealerts.db
```

The SQLite backend implements the async document operations used by Plane Alerts, including queries, projections, sorting, updates/upserts/replacements, unique indexes, TTL cleanup, migration state, integrity checks and consistent backup behavior.

SQLite is a first-class backend. It is not represented as a fake `MONGO_URI`.

### MongoDB Atlas

Choose **MongoDB Atlas** when you want managed MongoDB. Before continuing, create a MongoDB database user and allow the self-host machine's network access in Atlas. Paste the complete Atlas connection URI into the secret prompt. The installer validates URI structure without printing the credential.

### Existing MongoDB Server

Choose **Existing MongoDB Server** when you already operate MongoDB. Use a valid `mongodb://` or `mongodb+srv://` URI. Plane Alerts keeps its existing database name/configuration semantics.

## Telegram token setup

The installer tells you to open `@BotFather`, create or select a bot and copy its token. The token prompt does not echo the value.

Before setup can continue, the installer calls Telegram's `getMe`. An invalid/revoked token is rejected immediately. A network failure is reported separately so it is not misrepresented as an invalid credential.

The token is saved only in the local `.env`, which receives restrictive file permissions appropriate to the host OS.

## OpenSky credentials

OpenSky is optional because Plane Alerts has public-provider fallback.

Current Plane Alerts OpenSky authentication uses OAuth2 client credentials. The installer accepts the same forms as the runtime:

```text
clientId:clientSecret
```

or JSON with `clientId` and `clientSecret`. The installer calls the OpenSky token endpoint and only accepts the credential after a bearer token is returned.

Leave OpenSky skipped if you do not have credentials.

## Local ADS-B receiver

Supported families include readsb, dump1090, dump1090-fa and ultrafeeder/tar1090. Give the installer the receiver base URL, for example:

```text
http://127.0.0.1:8080
```

Plane Alerts probes common aircraft JSON paths such as `/data/aircraft.json`, `/tar1090/data/aircraft.json` and `/aircraft.json`.

If the feed is accepted, setup asks for receiver latitude, longitude and an estimated useful coverage radius.

These values describe **receiver coverage only**. They never become the user's Plane Alerts alert location. Monitoring locations remain Telegram profile data. A remote Telegram profile outside local receiver coverage must continue using public providers instead of being constrained by the receiver's physical location.

Leave local ADS-B unconfigured for public-provider-only operation.

## Background service and restarts

### Windows

The installer creates a Scheduled Task named `Plane Alerts Community` that starts at system boot under SYSTEM and launches:

```text
python -m uvicorn app.community_main_v55:app --host 0.0.0.0 --port 8000
```

### Linux / Raspberry Pi

The installer creates `plane-alerts-community.service`, enables it at boot and starts it immediately. It uses restart-on-failure/always behavior so a normal crash or machine reboot does not require manual recovery.

The service uses `app.community_main_v55:app`, not the owner/private AGY runtime.

## Community AGY exclusion

AGY is not required for self-hosted Plane Alerts and must remain absent from the community path.

The installer:

- does not ask for AGY credentials;
- does not write AGY configuration;
- does not create an AGY service/task;
- does not run AGY diagnostics;
- does not start/restart/update AGY;
- does not use Railway AI or a Railway AGY endpoint.

The community runtime places a zero-capability `app.agy_console` stub into the import path before loading shared `app.main`, so the private console is not imported by the community service. It also removes `/agy` from the Telegram command list.

## Doctor / diagnostics

Run diagnostics from an installed checkout with:

```bash
.venv/bin/python scripts/planealerts doctor
```

On Windows:

```text
.venv\Scripts\python.exe scripts\planealerts doctor
```

Use `--offline` for configuration/database checks that should not probe external services.

The v5.5 doctor understands SQLite and MongoDB. It checks:

- Python/version/configuration;
- selected database backend;
- SQLite integrity/schema or MongoDB reachability/schema;
- Telegram authentication;
- public ADS-B provider reachability;
- OpenSky credential configuration/authentication where configured;
- optional local receiver reachability;
- stored coordinate validity without printing exact coordinate values.

A failed optional provider is a warning rather than proof that monitoring is impossible. Required configuration/database/Telegram failures remain failures.

## Sanitized support bundle

The setup menu can generate a local support bundle. It includes release/commit, OS/architecture, non-secret configuration labels and installation metadata.

The bundle redacts tokens, passwords, API keys, authorization headers, MongoDB credentials, receiver coordinates and other secret-like fields. Review it before attaching it to a public issue anyway.

## Stable updates

During initial setup choose either:

- **Automatic stable updates** — daily stable-release check;
- **Manual updates** — updates only when you run the update action.

Automatic checks use a Scheduled Task on Windows or a systemd timer on Linux/Pi.

The updater only accepts the latest stable GitHub Release. It does not update from `main`, an open branch, draft release or prerelease.

Before changing code it:

1. runs offline doctor;
2. refuses tracked source modifications;
3. records the current immutable commit/tag;
4. copies `.env` into a timestamped backup;
5. uses SQLite's backup API for a consistent database backup when SQLite is active.

It then fetches the stable tag, resolves its immutable commit, checks out that commit detached, installs the release's exact pinned dependencies, runs doctor, restarts the service and requires live doctor to pass.

## Rollback

If target validation fails during an update, Plane Alerts automatically returns to the saved previous commit, restores `.env`, restores the SQLite snapshot when applicable, reinstalls the previous release's pinned dependencies and restarts the service.

Backups are versioned/timestamped. A new update does not deliberately delete the previous known-good release snapshot before the target has been validated.

For MongoDB, use your normal MongoDB/Atlas backup policy in addition to Plane Alerts configuration backups. The updater does not pretend that copying source code is a database backup.

## Repair

Use **Repair Existing Installation** if dependencies, the virtual environment or background service are damaged. Repair reinstalls the pinned runtime into the existing installation, preserves `.env`/database data, re-registers the community background service and runs doctor afterward.

Repair does not reset Telegram profiles or replace the database with defaults.

## Change configuration

Use **Change Configuration** to rerun the guided configuration without deleting the current installation. Secret prompts allow keeping the existing value rather than forcing a replacement.

Do not edit `.env.example` with real credentials; it is only a template. Actual credentials belong in `.env`.

## Uninstall

The uninstall menu removes the background service/task and updater schedule. It asks whether local configuration/database data should be kept.

Keeping data allows later reinstallation/recovery. Removing data deletes the local community configuration/data area after the services have been disabled.

## Advanced: Docker Compose

Docker Compose remains an advanced/manual self-hosting option for users who specifically want the existing container + MongoDB topology.

Requirements:

- Docker Engine with Compose v2;
- a Telegram bot token;
- outbound HTTPS access;
- sufficient memory/disk for both the application and MongoDB.

Example:

```bash
git clone https://github.com/xtenrore/Plane-Alerts.git
cd Plane-Alerts
cp .env.example .env
# for this advanced path set DATABASE_BACKEND=mongodb and the Compose Mongo URI
docker compose build plane-alerts
docker compose run --rm plane-alerts planealerts doctor --offline
docker compose up -d
```

Docker Compose remains MongoDB-based. The guided installer is the recommended v5.5 path for users who want local SQLite and managed updates/rollback.

## Known limitations

- Local receivers only provide aircraft they can physically receive; public providers remain necessary for remote profile locations.
- Raspberry Pi CI includes ARM64/QEMU runtime and Pi 5 platform-guard simulation; that is not presented as physical hardware certification for every Pi/SD-card/receiver combination.
- Automatic updater rollback can restore Plane Alerts-managed SQLite/configuration state. External MongoDB backups remain the operator's responsibility.
- A machine that is powered off cannot generate alerts; autostart only helps after the OS boots again.
- Provider outages/rate limits can reduce coverage. Missing ADS-B data is not evidence that an aircraft did or did not pass.

## Reporting a problem

Use the GitHub issue templates and include:

- release tag and commit;
- operating system and architecture;
- database backend;
- installer used;
- whether local ADS-B is configured;
- sanitized doctor/support-bundle output;
- redacted service logs.

Never include Telegram tokens, OpenSky client secrets, MongoDB passwords/URIs with credentials, local receiver authorization headers, AI API keys or exact private coordinates.
