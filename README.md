# Plane Alerts

Plane Alerts is a Telegram-based aircraft spotting alert system. It combines live ADS-B observations with deterministic trajectory, closest-point-of-approach (CPA), ETA, confidence, route-history and terminal-area logic so alerts are based on whether an aircraft is actually expected to pass the observer, not proximity alone.

AI is not part of the live qualification path. It does not decide trajectory, CPA, ETA, confidence, pass/no-pass, runway use, terminal state, cancellation or notification timing.

**Current code version: Plane Alerts v5.5.0**  
**Current prediction version: `5.3-3d-proximity-age-aware`**

Telegram hosted service: **[@planebotnotifierbot](https://t.me/planebotnotifierbot)**

## v5.5 guided community self-hosting

v5.5 adds a guided, community-only self-hosting path without changing the production prediction model. Community installations are distributed as stable GitHub Releases and are deliberately isolated from the owner's Railway deployment and private AGY service.

Choose the installer for the machine that will stay on and run Plane Alerts:

| Platform | Installer |
| --- | --- |
| Windows x64 / AMD64 | `setup.bat` or `setup-windows.bat` |
| Windows ARM64 | `setup.bat` or `setup-windows-arm64.bat` |
| Linux x64 / AMD64 | `setup-linux.sh` |
| Linux ARM64 | `setup-linux-arm64.sh` |
| Raspberry Pi 4/5, 64-bit OS | `setup-raspberry-pi.sh` |

The recommended route is to download the matching file from the latest stable GitHub Release. `setup.bat` can also detect Windows architecture and download the matching architecture-specific installer; that downloaded script is checked against the release `SHA256SUMS.txt` before execution.

Full setup, repair, update, rollback and troubleshooting documentation is in [`docs/self-hosting.md`](docs/self-hosting.md). Release-specific details are in [`docs/releases/v5.5.0.md`](docs/releases/v5.5.0.md).

### What the guided installer does

The installer checks the platform and architecture, bootstraps required Git/Python tooling through supported package-manager paths, creates a Python virtual environment, installs exact pinned dependencies, builds the local airport reference database, guides configuration, validates required services and installs an always-on background service.

The setup menu supports:

- fresh installation,
- repair of an existing installation,
- configuration changes,
- stable updates,
- diagnostics and sanitized support information,
- uninstall while keeping or removing local data.

Community services use `app.community_main_v55:app`. They do not use Railway and do not start or register AGY.

## Database choices

The guided installer offers three explicit storage paths:

1. **Local Database (SQLite)** — recommended for personal self-hosting.
2. **MongoDB Atlas** — for users who want a managed MongoDB service.
3. **Existing MongoDB Server** — for users who already operate MongoDB.

SQLite is a real persistence backend selected with `DATABASE_BACKEND=sqlite`; it is not disguised as a `MONGO_URI`. The v5.5 compatibility layer provides the async document operations required by Plane Alerts, including query/update semantics, sorting, projections, unique indexes, TTL cleanup, migration state, restart persistence, integrity checks and consistent backup support.

MongoDB remains the production persistence backend and remains available for self-hosting when explicitly selected.

## Telegram and OpenSky setup

The wizard explains where the Telegram bot token comes from, accepts it without echoing it, and calls Telegram `getMe` before setup is considered valid.

OpenSky is optional. Plane Alerts uses OpenSky OAuth2 client credentials. Each configured OpenSky slot accepts either:

```text
clientId:clientSecret
```

or JSON containing `clientId` and `clientSecret`. The installer validates the credential against the OpenSky token endpoint before saving it.

Public ADS-B providers remain available when OpenSky credentials are skipped.

## Local ADS-B receivers

Plane Alerts can use an optional local readsb, dump1090, dump1090-fa or ultrafeeder/tar1090 aircraft JSON feed while retaining public-provider fallback.

Typical configuration:

```env
LOCAL_ADSB_URL=http://receiver.local
LOCAL_ADSB_RECEIVER_TYPE=auto
LOCAL_ADSB_TIMEOUT_SECONDS=0.8
LOCAL_ADSB_MAX_POSITION_AGE_SECONDS=12
LOCAL_ADSB_AUTH_HEADER=
```

v5.5 keeps two location concepts separate:

- **receiver location** describes where the physical ADS-B antenna/receiver is and how far its feed is reasonably useful;
- **Telegram profile locations** are the places the user actually wants Plane Alerts to monitor.

The installer never substitutes receiver coordinates for an alert profile location. If a profile is outside the configured local receiver coverage envelope, public providers continue to supply that profile instead of treating the local feed as geographically authoritative.

## Stable updates and rollback

Community updates follow the latest **stable semantic-version GitHub Release**, never repository `main` or another moving development ref.

Before updating, Plane Alerts:

1. runs preflight diagnostics;
2. refuses to overwrite tracked local source modifications;
3. saves rollback metadata;
4. backs up `.env`;
5. creates a consistent SQLite backup when SQLite is in use;
6. fetches the stable release tag and resolves its immutable commit;
7. installs exact pinned dependencies;
8. runs target diagnostics;
9. restarts the background service;
10. requires live diagnostics to pass.

If target validation fails, the updater restores the previous commit, configuration and SQLite backup and restarts the previous runtime.

Automatic stable update checks can be enabled during setup. Windows uses Task Scheduler; Linux and Raspberry Pi use a systemd timer. Manual update mode remains available.

## AGY is not part of community self-hosting

AGY is private owner-side development/audit infrastructure and is intentionally excluded from the community installation path.

The community installer contains no AGY credential prompt, service installation, health requirement or update operation. The community runtime stubs the private console before loading the shared application and removes `/agy` from the Telegram command menu. Community diagnostics do not require or contact AGY.

This separation is deliberate: AGY is not required for Plane Alerts to calculate trajectories, CPA, ETA, qualification, cancellation or alert timing.

## Railway isolation

The owner's hosted production service still runs on Railway. Community release work is built on a dedicated release branch and published as GitHub Release assets.

The Railway deployment workflow remains restricted to successful tested pushes to `main`. A community v5.5 release does not deploy, restart or reconfigure the Railway Plane Alerts service or the separate AGY service.

## Real-time architecture

The alert-critical path remains:

```text
ADS-B ingestion
  -> freshness / canonical observation
  -> deterministic trajectory
  -> horizontal CPA + uncertainty-aware 3D CPA / ETA / confidence
  -> route + terminal evidence
  -> terminal-arrival delivery guard
  -> qualification / cancellation lifecycle
  -> Telegram delivery
```

The production physical prediction identifier remains `5.3-3d-proximity-age-aware` in v5.5.0. The self-hosting work does not intentionally change trajectory, CPA, ETA, route/terminal inference, qualification thresholds, cancellation thresholds or alert timing.

## Storage resilience

Once a process has loaded a verified active configuration, live monitoring uses bounded in-memory copies of active user/profile configuration and encounter state. Persistence is kept away from the five-second physical calculation path wherever possible.

Active profile materialization uses coherent `config_revision` generations so a mixed old/new location-preference pair is not published. Encounter lifecycle writes use bounded coalesced queues and timestamp guards so delayed persistence does not silently overwrite a newer state.

A cold process without verified configuration does not invent monitoring state during a storage outage.

## Airport, runway and terminal intelligence

Plane Alerts builds a commit-pinned worldwide airport/runway reference database locally. Runtime monitoring performs local lookups rather than calling an airport-data service for static geometry.

Terminal logic includes sustained vector changes, downwind/base transitions, final intercept, holding-like behavior, go-arounds and missed approaches. Fresh physical geometry remains authoritative; destination metadata alone is not enough to declare a nearby pass or landing path.

Permanent Error Museum cases protect against previously observed false alerts and lifecycle failures.

## Profiles and aircraft filtering

`/profiles` manages persistent alert profiles. Each profile can have its own monitoring location, radius, aircraft selection and advanced rules. Profiles can be created, activated, edited, renamed, duplicated and deleted.

Six editable starting presets are included: Casual Observer, Aircraft Photographer, Airport-Adjacent, Rare Aircraft Hunter, Military Watcher and Local SDR Mode. Presets are starting configurations; users can edit every exposed setting afterward.

Advanced rules inherit deterministically:

```text
Profile defaults
  -> Category override
  -> Aircraft-specific override
```

Selecting an aircraft never bypasses trajectory, CPA, confidence, route/terminal or lifecycle safety checks.

## Prediction Lab and Error Museum

Prediction Lab records forecasts before outcomes are known and later compares them with observed behavior. Missing ADS-B coverage remains unresolved rather than being counted as a hit or miss. Lifecycle cancellation alone is not treated as ground truth for a successful negative outcome.

Longer-range 30–60 minute expectations remain shadow-only until enough trustworthy outcomes exist. Candidate models cannot automatically promote themselves into the live prediction path.

Error Museum fixtures are permanent regression evidence and are not removed by transient-data cleanup.

## Photography and environment intelligence

Plane Alerts provides deterministic camera, framing, solar, atmospheric, upper-air and contrail guidance. Optional AI services can explain or enrich results, but they are not permitted to decide physical trajectory, CPA, ETA, qualification, cancellation or notification timing.

Google Contrails, when configured, remains optional enrichment only.

## Telegram commands

| Command | Purpose |
| --- | --- |
| `/start` | Initial setup |
| `/setup` | Safely review or change the active setup |
| `/profiles` | Create, switch and manage alert profiles |
| `/status` | Show monitoring status |
| `/next60` | Aircraft expected in the next 60 minutes |
| `/forecast` | Alias for `/next60` |
| `/location` | Update the active profile location |
| `/preferences` | Edit the active alert profile |
| `/camera` | Configure camera body |
| `/lens` | Configure lens |
| `/photo` | Current shooting guidance |
| `/conditions` | Weather, sun and atmospheric information |
| `/spotting` | Spotting tools |
| `/help` | Command help |

The community runtime does not register the private `/agy` command.

## Diagnostics

For a source checkout or installed community instance:

```bash
python scripts/planealerts doctor --offline
python scripts/planealerts doctor
```

The v5.5 doctor understands both SQLite and MongoDB. It checks configuration, database integrity/schema, Telegram authentication, provider reachability, OpenSky configuration and local receiver health without printing tokens, passwords, authorization headers or exact private coordinates.

## Testing and release gates

The standard CI gate still compiles the application, rebuilds/verifies the pinned airport database, runs the full pytest suite and all inherited replay/benchmark gates.

v5.5 adds dedicated community-installer CI covering:

- native Linux x64 installer guards and tests;
- native Windows x64 dependency installation and installer tests;
- ARM64 execution/build paths under QEMU;
- Raspberry Pi 5 64-bit platform-guard simulation under ARM64 QEMU;
- SQLite restart, migration, TTL and backup behavior;
- stable-release updater/rollback invariants;
- explicit community AGY-exclusion assertions.

QEMU/platform simulation is not presented as physical Raspberry Pi hardware certification.

## Advanced/manual self-hosting

Docker Compose remains available as an advanced/manual MongoDB-based self-hosting path. The guided installer is the recommended community path for v5.5 because it supplies local SQLite, platform detection, service registration, validation and stable update/rollback behavior.

See [`docs/self-hosting.md`](docs/self-hosting.md) for both paths.

## Production deployment

Production deployment remains separate from the community installer. The trusted Railway workflow verifies that a successful workflow came from a `main` push and that the tested SHA is still current `main` before deploying that exact source.

Normal Plane Alerts production deployment does not restart or modify the separate AGY service.

## Version history

Detailed release notes are maintained under [`docs/releases`](docs/releases) and summarized in [`CHANGELOG.md`](CHANGELOG.md). The v5.5 community release notes are [`docs/releases/v5.5.0.md`](docs/releases/v5.5.0.md).

## Reporting a problem

Use the repository issue templates and include:

- Plane Alerts version and commit/tag,
- platform and architecture,
- selected database backend,
- whether a local ADS-B receiver is configured,
- redacted `planealerts doctor --json` or the generated sanitized support bundle,
- relevant redacted service logs.

Never post Telegram tokens, OpenSky client secrets, MongoDB credentials, local receiver authorization headers, AI API keys or exact private observer/receiver coordinates.
