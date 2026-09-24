# Plane Alerts

Plane Alerts is a Telegram-based aircraft spotting alert system. It combines live ADS-B observations with deterministic trajectory, closest-point-of-approach (CPA), ETA, confidence, route-history and terminal-area logic so alerts are based on whether an aircraft is actually expected to pass the observer, not proximity alone.

AI is not part of the live qualification path. It does not decide trajectory, CPA, ETA, confidence, pass/no-pass, runway use, terminal state, cancellation or notification timing.

**Current code version: Plane Alerts v5.5.2**
**Current prediction version: `5.3-3d-proximity-age-aware`**

Telegram: **[@planebotnotifierbot](https://t.me/planebotnotifierbot)**

## Real-time architecture

The alert-critical path is deliberately ordered by priority:

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

Non-critical persistence, photography enrichment, historical learning and Prediction Lab evidence are isolated from the five-second monitoring path. A slow provider, database query, filesystem write or analytical service must not unnecessarily delay live aircraft evaluation.

## v5.5.2 — Railway Volume CLI Compatibility Patch

v5.5.2 corrects Railway CLI target-selector ordering in the production deployment and Prediction Lab evidence-sync workflows after the v5.5.1 rollout exposed a CLI parsing mismatch. The persistent `/data/prediction_lab` volume is now discovered or created using the current Railway `volume` command grammar, and volume file list/download/rename operations use the same verified selector ordering.

This is an operations/release compatibility patch only. It does not change the Prediction Lab evidence schema, MongoDB migration semantics, live trajectory/CPA/ETA logic, qualification/cancellation behavior, alert timing, or the physical prediction version.

## v5.5.1 — File-Backed Prediction Lab

v5.5.1 moves high-volume Prediction Lab audit, shadow-evaluation and sentinel evidence out of MongoDB and into a bounded persistent file spool. Railway uses `/data/prediction_lab`; Docker Compose uses the same path on a dedicated persistent volume.

Historical `prediction_lab_audit`, `prediction_shadow_evaluations` and `prediction_sentinel_routes` records are exported to verified NDJSON chunks with count and SHA-256 integrity checks before those legacy collections are retired. Normal application MongoDB data remains in place.

A dedicated `prediction-lab-data` Git branch receives bounded synchronized evidence batches. Runtime does not hold GitHub credentials or perform Git network work. Files are acknowledged only after a successful Git push, making failed/conflicting syncs resumable. Commits to the data branch cannot trigger production deployment.

The repository provides durable case/event IDs and Collector, Investigator and Release checkpoints for external scheduled evidence processing. Missing ADS-B coverage remains inconclusive, and 30–60 minute `/next60` history predictions remain shadow-only.

The fixed high-risk evaluation network covers Istanbul, London, Frankfurt, Paris, Amsterdam, Madrid, Rome and Vienna. Configured locations belonging to current delegated admins are also evaluated in memory using privacy-safe repository identifiers rather than admin identity or private observer coordinates.

This release does not change physical prediction behavior. The prediction version remains `5.3-3d-proximity-age-aware`.

## Prediction behavior

Plane Alerts continuously recalculates aircraft motion from fresh observations and bounded history. The production stack preserves the established deterministic safety layers, including:

- current distance and horizontal CPA,
- altitude-aware 3D relevance when altitude evidence is trustworthy,
- ETA/time-to-CPA with provider-reported position age,
- confidence and uncertainty handling,
- route-history supporting evidence,
- airport/runway and terminal-arrival guards,
- cancellation latching and fresh direct-presence recovery,
- observed-pass semantics that do not treat a projected close pass as ground truth.

Missing or stale ADS-B coverage is handled explicitly. Missing data is not proof that a prediction succeeded or failed.

## Multi-location and scale

Observer-independent aircraft motion is reused through bounded process-local caches. Candidate filtering uses an acceleration-only spatial grid followed by exact spherical membership checks against each user's monitoring envelope. Per-user CPA, 3D relevance, confidence, route/terminal, qualification, cancellation and lifecycle state remain independent.

The scale path does not create a second ADS-B feed layer or multiply provider requests for every user.

## Storage resilience

MongoDB remains application persistence for users, configuration and normal product state. Prediction Lab high-volume audit/evaluation telemetry is file-backed in v5.5.1.

A verified last-known-good active configuration can continue to drive live monitoring during a temporary MongoDB outage. A cold process without verified configuration does not invent state. Encounter persistence uses bounded queues and delayed writes cannot silently overwrite newer lifecycle records.

`/health` provides structured diagnostics. `/ready` is the rollout/readiness endpoint and requires a usable monitoring configuration, live worker and initialized Telegram runtime.

## Prediction Lab and automated evidence pipeline

Repository structure:

```text
prediction_lab/raw/YYYY-MM-DD/
prediction_lab/unchecked/YYYY-MM-DD/
prediction_lab/reviewed/YYYY-MM-DD/
prediction_lab/error_museum/
prediction_lab/archive/mongo-import/
prediction_lab/state/
prediction_lab/schemas/
```

The Collector converts new raw events into durable unchecked cases. The Investigator/Fixer independently verifies evidence before producing reviewed cases, replay regressions or engineering branches. The Daily Release task independently validates candidate fixes before using sequential v5.5.x patch releases. Checkpoints advance only after their corresponding durable work succeeds.

See [`docs/prediction-lab-pipeline.md`](docs/prediction-lab-pipeline.md) for the evidence/sync contract.

Operator commands include:

```bash
planealerts diagnostics
planealerts metrics
planealerts shadow-eval
```

Candidate promotion is never automatic.

## Next 60 Minutes

`/next60` prefers fresh live deterministic approach state. Historical shadow candidates are derived from bounded normal flight-route history rather than Prediction Lab MongoDB. The 30–60 minute horizon stays evaluation-only and history confidence cannot override live CPA.

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

Profile and setup flows include explicit navigation/recovery behavior so stale or interrupted Telegram callbacks do not trap users.

## Self-hosting

See [`docs/self-hosting.md`](docs/self-hosting.md) for Docker Compose, native Python, Raspberry Pi/ARM64, upgrade and rollback instructions.

At minimum, self-hosting requires a Telegram bot token and persistent storage. Public ADS-B providers are supported, and an optional local readsb/dump1090/ultrafeeder receiver can be configured through `LOCAL_ADSB_URL`.

Typical Docker Compose flow:

```bash
cp .env.example .env
# edit TELEGRAM_BOT_TOKEN and any optional provider settings
docker compose build plane-alerts
docker compose run --rm plane-alerts planealerts doctor --offline
docker compose up -d
```

`mongo_data` and `prediction_lab_data` are persistent named volumes. Never commit `.env` or expose provider credentials in logs or issue reports.

## Configuration

Important environment settings are documented in `.env.example`. Runtime AI credentials are optional and are not required for the deterministic alert path. `PREDICTION_LAB_ROOT` defaults to `/data/prediction_lab` in production/container deployments and should point to persistent storage when overridden.

## Testing and release safety

CI compiles the application, rebuilds/verifies airport data, runs the full pytest suite, replays Error Museum/provider/terminal/storage regressions, validates Prediction Lab migration/sync/idempotency/cadence behavior, validates self-hosting configuration, builds amd64 and ARM64 images, and runs deterministic performance gates.

Production deployment is gated on successful CI for the exact current `main` commit. Stable tags are immutable semantic `vX.Y.Z` releases.

## Documentation

- [`CHANGELOG.md`](CHANGELOG.md) — release summary
- [`docs/releases/`](docs/releases/) — version-specific release notes
- [`docs/self-hosting.md`](docs/self-hosting.md) — installation, upgrade and rollback
- [`docs/prediction-lab-pipeline.md`](docs/prediction-lab-pipeline.md) — v5.5 evidence pipeline
- [`docs/V4_PREDICTION_LAB.md`](docs/V4_PREDICTION_LAB.md) — Prediction Lab background
- [`docs/error_museum/`](docs/error_museum/) — reproducible prediction failures/regressions

## Security and privacy

Do not expose Telegram tokens, MongoDB credentials, provider keys, admin passwords, webhook secrets or exact private observer coordinates. Admin APIs fail closed when `ADMIN_PASSWORD` is blank. Prediction Lab repository evidence replaces direct user identifiers with local salted references and does not include private admin observer coordinates.

## License

Plane Alerts is licensed under the MIT License. See [`LICENSE`](LICENSE).
