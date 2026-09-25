# Plane Alerts

Plane Alerts is a Telegram-based aircraft spotting alert system. It combines live ADS-B observations with deterministic trajectory, closest-point-of-approach (CPA), ETA, confidence and destination/path validation so alerts are based on whether an aircraft is physically expected to pass the observer, not proximity or a temporary heading alone.

AI is not part of the live qualification path. It does not decide trajectory, CPA, ETA, confidence, destination/path qualification, pass/no-pass, cancellation or notification timing.

**Current code version: Plane Alerts v5.6.11**  
**Current prediction version: `5.3-3d-proximity-age-aware`**

Telegram: **[@planebotnotifierbot](https://t.me/planebotnotifierbot)**

## Real-time architecture

The alert-critical path is deliberately ordered by priority:

```text
ADS-B ingestion
  -> freshness / canonical observation
  -> deterministic trajectory
  -> horizontal CPA + uncertainty-aware 3D CPA / ETA / confidence
  -> cached background destination resolution (ADSB.lol + ADSBDB)
  -> deterministic destination/path conflict check
  -> qualification / cancellation lifecycle
  -> Telegram delivery
```

Destination provider calls never block the five-second monitoring loop. Provider metadata only supplies intended airport information; deterministic live geometry decides whether that destination is compatible with a genuine observer pass. Provider outage or genuinely ambiguous conflict safely falls back to live trajectory behavior; when disagreement contains one clearly supported nearby terminal destination and a materially distant alternative, live terminal geometry may resolve the conflict without trusting provider priority. Historical route samples and airport/runway inference remain available for Prediction Lab, analytics and diagnostics, but they are not live alert authorities.

Non-critical persistence, photography enrichment, historical learning and Prediction Lab evidence are isolated from the five-second monitoring path. A slow provider, database query, filesystem write or analytical service must not unnecessarily delay live aircraft evaluation.

## v5.6.11 — Resilient Prediction Lab Spool Drain

v5.6.11 fixes the production 409 discovered by the first live v5.6.10 HTTP-bridge drain attempt. One malformed, legacy, sensitive or otherwise non-exportable raw object can no longer block valid evidence behind it. Rejected objects remain untouched on the Railway volume; the bridge continues scanning within a bounded 5,000-object window and exports only valid evidence.

Each batch still contains at most 500 files / 25 MiB, and GitHub still independently verifies every member path, byte count, SHA-256, JSON/NDJSON schema and credential-safety rule before pushing exact bytes to `prediction-lab-data`. Runtime deletion remains impossible until that repository push succeeds and the exact path + size + SHA-256 acknowledgement is posted back. Rejected objects are reported only as sanitized reason counts such as schema, invalid JSON, credential-like, read/stat error or oversized; their content is never surfaced through workflow diagnostics.

The workflow now also exposes a sanitized bridge error detail if a future batch cannot produce any exportable evidence, so another HTTP 409 cannot hide the concrete rejection class. Route-history SQLite, notification-history SQLite, state, user/profile/location/settings data and historical archive data remain outside the bridge's selectable raw tree.

No trajectory, CPA, ETA, confidence, destination/path qualification, cancellation or alert-timing behavior changes in v5.6.11. The physical prediction version remains `5.3-3d-proximity-age-aware`.

## v5.6.10 — Authenticated Prediction Lab Spool Bridge

v5.6.10 removes Railway SFTP from Prediction Lab evidence synchronization after production proved that project-token CI could reach the project but could not use Railway file transport without a user SSH key.

The runtime now exposes a narrowly scoped admin-authenticated bridge that exports only validated `prediction_lab/raw` JSON/NDJSON evidence in bounded 500-file / 25 MiB ZIP batches created in ephemeral `/tmp` storage. Each manifest records the exact repository-relative path, byte count and SHA-256. GitHub independently revalidates the ZIP member set, path boundary, byte count, hash, schema and credential safety before committing the exact bytes to `prediction-lab-data`.

Deletion remains strictly after repository acknowledgement: only after the Git push succeeds does GitHub POST the exact path + size + SHA-256 manifest back to the runtime. The bridge rechecks those bytes immediately before unlinking. Route-history SQLite, notification-history SQLite, state, user/profile/location/settings data and historical archive data are outside the bridge's selectable tree.

The workflow uses the existing Railway project token only to read the already-configured `ADMIN_PASSWORD` and public service domain; no Railway SSH key or runtime GitHub credential is introduced. Merge-triggered sync also waits until the running bridge reports exactly `5.6.10`, avoiding a race with the previous Railway deployment.

No trajectory, CPA, ETA, confidence, destination/path qualification, cancellation or alert-timing behavior changes in v5.6.10. The physical prediction version remains `5.3-3d-proximity-age-aware`.

## v5.6.9 — Live Service Filesystem Prediction Lab Drain

v5.6.9 fixes the remaining Railway transfer failure discovered by the first v5.6.8 bootstrap run. The volume attachment was resolvable, but the selected-volume SFTP target failed while listing `/raw` before any validation, repository push or deletion occurred.

The synchronization workflow now uses Railway's supported `service files` interface against the exact live mount at `/data/prediction_lab`. It confirms the mount first, lists and downloads only evidence below that root, converts absolute service paths back to repository-relative paths only after enforcing the mount boundary, and exposes Railway's actual command output on failure instead of hiding it behind a generic process exception.

Repository acknowledgement remains the destructive boundary: a remote object can be removed only after validation and a successful push to `prediction-lab-data`. The v5.6.8 bounded 500-object / 25 MiB transfer, confirmed non-interactive removal and serialized self-drain continuation remain in place. No route-history SQLite, notification-history SQLite, state, user/profile/location/settings or durable application data is selected for removal.

No trajectory, CPA, ETA, confidence, destination/path qualification, cancellation or alert-timing behavior changes in v5.6.9. The physical prediction version remains `5.3-3d-proximity-age-aware`.

## v5.6.8 — Repository-Acknowledged Spool Drain

v5.6.8 completes the storage-recovery path for a Prediction Lab volume that has already reached zero free bytes. Each bounded sync batch is still validated and pushed to `prediction-lab-data` first; only then are the exact repository-acknowledged remote objects removed from the Railway volume with a confirmed non-interactive delete.

A workflow-file merge to `main` bootstraps an immediate drain pass so emergency recovery does not rely only on delayed scheduled execution. Large successful batches dispatch another bounded pass, while workflow concurrency keeps drain passes serialized and the chain stops automatically once the backlog is no longer large. The five-minute schedule remains the steady-state fallback.

The v5.6.7 low-space backpressure and lossless NDJSON compaction remain active. No route-history SQLite, notification-history SQLite, state, user/profile/location/settings or durable application data is selected for cleanup. No trajectory, CPA, ETA, confidence, destination/path qualification, cancellation or alert-timing behavior changes in v5.6.8; the physical prediction version remains `5.3-3d-proximity-age-aware`.

## v5.6.7 — Prediction Lab Spool Drain & Storage Backpressure

v5.6.7 fixes the persistent-volume failure exposed after v5.6.6. The Prediction Lab repository sync now parses Railway's volume-relative file listing directly, drains bounded evidence batches every five minutes, and supports lossless NDJSON bundles so a large raw backlog can be synchronized efficiently.

Optional Prediction Lab telemetry now reserves disk headroom for operational storage. Routine analytical snapshots are shed before the volume reaches the critical reserve, all optional evidence is shed at critical pressure, and repeated low-space/ENOSPC failures are rate-limited instead of generating traceback storms. Unsynced evidence is never deleted merely to make space: raw JSON may be atomically compacted into a verified NDJSON bundle, with originals removed only after the bundle has been written and read back successfully. Repository acknowledgement still happens only after validation and a successful Git push.

No trajectory, CPA, ETA, confidence, destination/path qualification, cancellation or alert-timing behavior changes in v5.6.7. The physical prediction version remains `5.3-3d-proximity-age-aware`.

## v5.6 — Retired Integration Cleanup

v5.6 removes the remaining names, hand-off labels and stale documentation references from the retired external-agent integration. The Railway runtime already operated without that sidecar; this release also removes the final stale production token and adds a repository-wide regression so those references cannot return.

There is no trajectory, CPA, ETA, destination-path, qualification, cancellation, provider, database or alert-timing change. The physical prediction version remains `5.3-3d-proximity-age-aware`, and the v5.5.4 destination-path arrival fix is preserved unchanged.

## v5.5.4 — Provider-First Destination Path Guard

v5.5.4 replaces the accumulated route-history, expected-turn and runway/terminal alert-veto chain with one destination-aware qualification layer.

ADSB.lol route data is used as the primary destination source and ADSBDB provides an independent additional/fallback source. Results are cached and refreshed only through bounded background work. A new close-CPA candidate may wait one monitor cycle for a destination result, but an external route lookup is never awaited inside the monitoring path and a pending lookup cannot cancel an already-active alert.

When a reliable destination is known, Plane Alerts compares the live aircraft position, destination airport, observer, speed, altitude/descent, predicted CPA and projected path. An arrival can be held when the destination airport or landing area is physically reached before the supposed observer pass, or when continuing to the observer CPA would move the aircraft materially away from its known destination. This fixes the class of false alert where an IST/LTFM or LTBA arrival points toward the observer for several seconds but must turn or land before reaching them.

The guard fails open when providers are unavailable or a disagreement remains physically ambiguous. A disagreement may be resolved only when live terminal geometry decisively supports one nearby airport over a materially farther alternative; provider brand priority alone is never enough. Destination authority also releases when fresh live movement clearly diverges from the airport, including a go-around/diversion pattern. Fresh physical presence inside the configured observer radius always wins over destination metadata.

The underlying trajectory/3D prediction version remains `5.3-3d-proximity-age-aware`; v5.5.4 changes destination-aware alert qualification rather than the motion predictor itself.

## v5.5.3 — Startup Migration Isolation

v5.5.3 fixes the Railway readiness regression exposed by v5.5.2. After MongoDB connected, the required historical Prediction Lab archive migration plus index/schema maintenance still ran inside FastAPI startup. On a production-sized archive that low-priority work could outlive Railway's readiness window even though normal application reads were already available.

Railway now schedules the same verified migration/index/schema maintenance in a retrying background task after normal Mongo connectivity succeeds. FastAPI, Telegram initialization and the live monitoring worker can become ready without waiting for historical archive work. Migration integrity is unchanged: record counts and SHA-256 hashes must verify before legacy Prediction Lab collections are retired, and normal application MongoDB data is not part of that retirement path.

## v5.5.2 — Railway Volume CLI Compatibility Patch

v5.5.2 corrects Railway CLI target-selector ordering in the production deployment and Prediction Lab evidence-sync workflows after the v5.5.1 rollout exposed a CLI parsing mismatch. The persistent `/data/prediction_lab` volume is discovered or created using the current Railway `volume` command grammar, and volume file list/download/rename operations use the same verified selector ordering.

## v5.5.1 — File-Backed Prediction Lab

v5.5.1 moves high-volume Prediction Lab audit/shadow-evaluation and sentinel evidence out of MongoDB and into a bounded persistent file spool. Railway uses `/data/prediction_lab`; Docker Compose uses the same path on a dedicated persistent volume.

Historical `prediction_lab_audit`, `prediction_shadow_evaluations` and `prediction_sentinel_routes` records are exported to verified NDJSON chunks with count and SHA-256 integrity checks before those legacy collections are retired. Normal application MongoDB data remains in place.

A dedicated `prediction-lab-data` branch receives bounded synchronized evidence batches. Runtime does not hold GitHub credentials or perform Git network work. Files are acknowledged only after a successful Git push, making failed/conflicting syncs resumable. Missing ADS-B coverage remains inconclusive, and 30–60 minute `/next60` history predictions remain shadow-only.

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

Route history and runway/terminal inference remain useful evidence for Prediction Lab and diagnostics, but they do not veto live alerts. Missing or stale ADS-B coverage is handled explicitly; missing data is not proof that a prediction succeeded or failed.

## Multi-location and scale

Observer-independent aircraft motion is reused through bounded process-local caches. Candidate filtering uses an acceleration-only spatial grid followed by exact spherical membership checks against each user's monitoring envelope. Per-user CPA, 3D relevance, confidence, destination/path qualification, cancellation and lifecycle state remain independent.

The scale path does not create a second ADS-B feed layer or multiply provider requests for every user.

## Storage resilience

MongoDB remains application persistence for users, configuration and normal product state. Prediction Lab high-volume audit/evaluation telemetry is file-backed.

A verified last-known-good active configuration can continue to drive live monitoring during a temporary MongoDB outage. A cold process without verified configuration does not invent state. Encounter persistence uses bounded queues and delayed writes cannot silently overwrite newer lifecycle records. On Railway, historical Prediction Lab migration and normal index/schema maintenance run as low-priority retrying work after normal Mongo connectivity is available, so they do not hold application readiness hostage.

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

The Collector converts new raw events into durable unchecked cases. The Investigator/Fixer independently verifies evidence before producing reviewed cases, replay regressions or engineering branches. Checkpoints advance only after their corresponding durable work succeeds.

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

Railway production CI compiles the application, rebuilds/verifies airport reference data, runs the full pytest suite, replays Error Museum/provider/arrival/storage regressions, runs deterministic performance and cadence checks, and validates dependency consistency. Production deployment uses the exact successful current `main` commit.

Community/self-host stable releases are separate. The long Windows ARM64/Linux ARM64/Raspberry Pi/installer matrix is owned by the dedicated ChatGPT **Weekly Community Release** scheduled task, not by normal Railway CI and not by a GitHub weekly cron. GitHub exposes that community matrix and the community publisher as manual workflows for the scheduled ChatGPT task to invoke after it determines the required deferred validation. An ordinary Railway deployment is not automatically a community-stable release.

## Documentation

- [`CHANGELOG.md`](CHANGELOG.md) — release summary
- [`docs/releases/`](docs/releases/) — version-specific release notes
- [`docs/self-hosting.md`](docs/self-hosting.md) — installation, upgrade and rollback
- [`docs/prediction-lab-pipeline.md`](docs/prediction-lab-pipeline.md) — evidence pipeline
- [`docs/V4_PREDICTION_LAB.md`](docs/V4_PREDICTION_LAB.md) — Prediction Lab background
- [`docs/error_museum/`](docs/error_museum/) — reproducible prediction failures/regressions

## Security and privacy

Do not expose Telegram tokens, MongoDB credentials, provider keys, admin passwords, webhook secrets or exact private observer coordinates. Admin APIs fail closed when `ADMIN_PASSWORD` is blank. Prediction Lab repository evidence replaces direct user identifiers with local salted references and does not include private admin observer coordinates.

## License

Plane Alerts is licensed under the MIT License. See [`LICENSE`](LICENSE).
