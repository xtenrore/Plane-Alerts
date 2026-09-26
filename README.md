# Plane Alerts

Plane Alerts is a Telegram-based aircraft spotting alert system. It combines live ADS-B observations with deterministic trajectory, closest-point-of-approach (CPA), ETA, confidence and destination/path validation so alerts are based on whether an aircraft is physically expected to pass the observer rather than simple proximity or a temporary heading.

AI is not part of the live qualification path. It does not decide trajectory, CPA, ETA, confidence, destination/path qualification, pass/no-pass, cancellation or notification timing.

**Current code version: Plane Alerts v5.8.3**

**Current prediction version: `5.3-3d-proximity-age-aware`**

Telegram: **[@planebotnotifierbot](https://t.me/planebotnotifierbot)**

## v5.8.3 — Dead workflow cleanup

The Vercel workflow runtime now has one unambiguous active module: `vercel_runtime/plane_workflows.py`. The active v3.3 workflow implementation previously lived beside an obsolete v3.2 implementation under the temporary-looking `plane_workflows_fixed.py` name.

- The exact active v3.3 workflow implementation now owns the stable `plane_workflows.py` filename.
- Vercel ingress imports the canonical module and `pyproject.toml` registers `plane_workflows:wf` as the workflow entrypoint.
- The retired v3.2 workflow implementation and the misleading `plane_workflows_fixed.py` filename are removed.
- Vercel latency regressions now test the canonical workflow together with `plane_runtime_support.py` instead of depending on both old and new workflow files.
- Architecture coverage prevents `plane_workflows_fixed` references from returning and verifies the canonical workflow remains on the `planev33` durable namespace rather than the retired `planev32` graph.

This is an architecture/maintenance release. Railway runtime configuration, dependencies, storage schema, providers, installer behavior and the deterministic prediction/alert path are unchanged. The prediction version remains `5.3-3d-proximity-age-aware`. Full community platform validation remains part of the weekly community release process.

## Real-time architecture

The alert-critical path is deliberately ordered by priority:

```text
ADS-B ingestion
  -> freshness / canonical observation
  -> deterministic trajectory
  -> horizontal CPA + uncertainty-aware 3D CPA / ETA / confidence
  -> cached background destination resolution
  -> deterministic destination/path conflict check
  -> qualification / cancellation lifecycle
  -> Telegram delivery
```

Destination-provider calls never block the five-second monitoring loop. Provider metadata supplies intended airport information; deterministic live geometry decides whether that destination is compatible with a genuine observer pass. Historical route samples and airport/runway inference remain supporting evidence for Prediction Lab, analytics and diagnostics rather than live alert authorities.

Non-critical persistence, photography enrichment, historical learning and Prediction Lab evidence are isolated from the five-second monitoring path. A slow provider, database query, filesystem write or analytical service must not unnecessarily delay live aircraft evaluation.

## Prediction behavior

Plane Alerts continuously recalculates aircraft motion from fresh observations and bounded history. The production stack includes:

- current distance and horizontal CPA,
- altitude-aware 3D relevance when altitude evidence is trustworthy,
- ETA/time-to-CPA with provider-reported position age,
- confidence and uncertainty handling,
- cached destination intent from public route providers,
- deterministic airport-before-observer and destination/path-conflict validation,
- cancellation latching and fresh direct-presence recovery,
- observed-pass semantics that do not treat a projected close pass as ground truth.

Missing or stale ADS-B coverage is handled explicitly; missing data is not proof that a prediction succeeded or failed.

## Next 60 Minutes

`/next60` prefers fresh live deterministic approach state. Historical candidates are derived from bounded route history. The 30–60 minute horizon remains shadow-only and cannot override live CPA.

Each visible aircraft can be opened in Flightradar24 from Telegram. Callsigns are used for direct live-flight links where available; Plane Alerts does not fabricate Flightradar24 flight IDs.

## Photography and spotting guidance

Plane Alerts provides deterministic spotting guidance for camera settings, framing, sun position, atmospheric conditions, upper-air conditions, contrail probability and shooting-window timing. Optional external enrichment must not control trajectory, CPA, ETA, confidence, qualification, cancellation or alert timing.

## Telegram commands

Core user commands include:

```text
/start        Initial setup
/profiles     Manage alert profiles
/status       Show monitoring status
/next60       Next 60 Minutes forecast
/forecast     Alias for /next60
/location     Set monitoring / shooting location
/preferences  Edit the active alert profile
/camera       Set camera body
/lens         Set aircraft lens
/photo        Get live shooting guidance
/conditions   Show weather / sun / haze conditions
/spotting     Open Spotting Mode
/help         Show help
```

Profile and setup flows include explicit navigation and recovery behavior so stale or interrupted Telegram callbacks do not trap users.

## Storage and Prediction Lab

MongoDB remains durable application persistence for users, Telegram identity, locations, profiles, preferences and bounded configuration/state. High-volume route history, notification lifecycle telemetry and Prediction Lab evidence use bounded persistent-volume storage.

Prediction Lab evidence is synchronized through the authenticated bounded HTTP bridge and is acknowledged for deletion only after the exact bytes have been validated and pushed to the repository evidence branch. Invalid or sensitive evidence is preserved rather than silently discarded.

## Self-hosting

See [`docs/self-hosting.md`](docs/self-hosting.md) for supported installation, configuration, database, update and rollback procedures.

At minimum, self-hosting requires a Telegram bot token and persistent storage. Public ADS-B providers are supported, and an optional local readsb/dump1090/ultrafeeder receiver can be configured through `LOCAL_ADSB_URL`.

Typical Docker Compose flow:

```bash
cp .env.example .env
# edit TELEGRAM_BOT_TOKEN and any optional provider settings
docker compose build plane-alerts
docker compose run --rm plane-alerts planealerts doctor --offline
docker compose up -d
```

Never commit `.env` or expose provider credentials in logs or issue reports.

## Testing and release safety

Railway production CI compiles the application, rebuilds and verifies airport reference data, runs the full pytest suite, replays Error Museum/provider/arrival/storage regressions, runs deterministic performance checks and validates dependency consistency. Production deployment uses the exact successful current `main` commit.

Railway production releases and community/self-host stable releases are separate channels. Long Windows ARM64, Linux ARM64, Raspberry Pi, installer, updater and rollback matrices belong to the weekly community-release validation process and are not implied by a Railway-only deployment.

## Documentation

- [`CHANGELOG.md`](CHANGELOG.md) — release summary
- [`docs/releases/`](docs/releases/) — version-specific release notes
- [`docs/self-hosting.md`](docs/self-hosting.md) — installation, upgrade and rollback
- [`docs/prediction-lab-pipeline.md`](docs/prediction-lab-pipeline.md) — evidence pipeline
- [`docs/error_museum/`](docs/error_museum/) — reproducible prediction regressions

## Security and privacy

Do not expose Telegram tokens, MongoDB credentials, provider keys, admin passwords, webhook secrets or exact private observer coordinates. Admin APIs fail closed when `ADMIN_PASSWORD` is blank. Runtime AI is optional and never controls physical alert decisions.

## License

Plane Alerts is licensed under the MIT License. See [`LICENSE`](LICENSE).
