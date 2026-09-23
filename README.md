# Plane Alerts

Plane Alerts is a Telegram-based aircraft spotting alert system. It combines live ADS-B observations with deterministic trajectory, closest-point-of-approach (CPA), ETA, confidence, route-history and terminal-area logic so alerts are based on whether an aircraft is actually expected to pass the observer, not proximity alone.

AI is not part of the live qualification path. It does not decide trajectory, CPA, ETA, confidence, pass/no-pass, runway use, terminal state, cancellation or notification timing.

**Current code version: Plane Alerts v5.4.3**  
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

Non-critical persistence, photography enrichment, historical learning and shadow evaluation are isolated from the five-second monitoring path. A slow provider, database query or analytical write must not unnecessarily delay live aircraft evaluation.

## v5.4.3

v5.4.3 removes the retired external agent sidecar integration from Plane Alerts. The dedicated Railway service and state volume are gone, the private Telegram command is removed, and the associated runtime modules, Docker/dependency files, helper scripts, environment settings and agent-only tests are no longer part of the project.

The useful v5.3 provider-age and stale-cancellation regressions remain under a neutral test name because they protect the production predictor rather than any external agent integration.

This release does not intentionally alter trajectory, CPA, ETA, confidence, terminal inference, qualification, cancellation, alert timing or provider polling. The physical prediction version remains `5.3-3d-proximity-age-aware`.

## Prediction behavior

Plane Alerts continuously recalculates aircraft motion from fresh observations and bounded history. The production stack preserves the established safety layers around the deterministic predictor, including:

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

MongoDB remains the primary production persistence layer, but live monitoring does not synchronously depend on every analytical write.

A verified last-known-good active configuration can continue to drive live monitoring during a temporary MongoDB outage. A cold process without verified configuration does not invent state. Encounter persistence uses bounded queues and delayed writes cannot silently overwrite newer lifecycle records.

`/health` provides structured diagnostics. `/ready` is the rollout/readiness endpoint and requires a usable monitoring configuration, live worker and initialized Telegram runtime.

## Prediction Lab and shadow evaluation

Shadow candidates can be evaluated against later observed outcomes without selecting the live model. Observed in-radius passes can become scoreable ground truth; lifecycle cancellation alone and missing ADS-B coverage remain unresolved until separately supported by evidence.

Operator commands include:

```bash
planealerts diagnostics
planealerts metrics
planealerts shadow-eval
```

Candidate promotion is never automatic.

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

Never commit `.env` or expose provider credentials in logs or issue reports.

## Configuration

Important environment settings are documented in `.env.example`. Notable groups include:

- Telegram/webhook configuration,
- MongoDB,
- public ADS-B providers and OpenSky credentials,
- optional local ADS-B receiver,
- optional photography/explanation providers,
- monitoring cadence/radius,
- shadow-evaluation feature flags,
- admin authentication,
- host/port/logging.

Runtime AI credentials are optional and are not required for the deterministic alert path.

## Testing and release safety

CI compiles the application, rebuilds/verifies airport data, runs the full pytest suite, replays Error Museum/provider/terminal/storage regressions, validates self-hosting configuration, builds amd64 and ARM64 images, and runs deterministic performance gates.

The v5.4.3 removal regression explicitly verifies that the retired sidecar runtime files, Telegram command, bridge configuration and agent-specific CI references are absent.

Useful local checks:

```bash
python -m compileall -q app vercel_runtime worker.py
python scripts/build_airport_database.py
python scripts/verify_airport_database.py
pytest -q
pytest -q tests/test_no_retired_agent_runtime_v543.py
pytest -q tests/test_v53_prediction_regressions.py
```

Production deployment is gated on successful CI for the exact current `main` commit.

## Documentation

- [`CHANGELOG.md`](CHANGELOG.md) — release summary
- [`docs/releases/`](docs/releases/) — version-specific release notes
- [`docs/self-hosting.md`](docs/self-hosting.md) — installation, upgrade and rollback
- [`docs/V4_PREDICTION_LAB.md`](docs/V4_PREDICTION_LAB.md) — Prediction Lab background
- [`docs/error_museum/`](docs/error_museum/) — reproducible prediction failures/regressions

## Security and privacy

Do not expose Telegram tokens, MongoDB credentials, provider keys, admin passwords, webhook secrets or exact private observer coordinates. Admin APIs fail closed when `ADMIN_PASSWORD` is blank. Exact user coordinates are not included in normal operator diagnostics.

## License

Plane Alerts is licensed under the MIT License. See [`LICENSE`](LICENSE).
