# Plane Alerts

Plane Alerts is a Telegram-based aircraft spotting alert system. It combines live ADS-B observations with deterministic trajectory, closest-point-of-approach (CPA), ETA, confidence, route-history and terminal-area logic so alerts are based on whether an aircraft is actually expected to pass the observer, not proximity alone.

AI is not part of the live qualification path. It does not decide trajectory, CPA, ETA, confidence, pass/no-pass, runway use, terminal state, cancellation or notification timing.

**Current code version: Plane Alerts v4.9.0**  
**Current prediction version: `4.7.3-terminal-delivery-landing-path`**

Telegram: **[@planebotnotifierbot](https://t.me/planebotnotifierbot)**

## Real-time architecture

The alert-critical path is deliberately ordered by priority:

```text
ADS-B ingestion
  -> freshness / canonical observation
  -> deterministic trajectory
  -> CPA / ETA / confidence
  -> route + terminal evidence
  -> v4.7.3 terminal-arrival delivery guard
  -> qualification / cancellation lifecycle
  -> Telegram delivery
```

v4.8 keeps non-critical persistence outside that path. Once a process has loaded a verified active configuration, live monitoring uses bounded in-memory copies of active user/profile configuration and encounter lifecycle state. Mongo refresh and persistence happen on bounded background loops.

A slow analytics write must not turn the five-second monitoring interval into a ten-second interval.

## v4.9 project maturity and self-hosting

v4.9 makes Plane Alerts reproducible outside Railway without creating a second prediction implementation. Runtime Python dependencies are exactly pinned, Docker builds support amd64 and ARM64, and `docker-compose.yml` provides a health-checked MongoDB with a persistent volume and restart policies.

The read-only `planealerts doctor` command validates important configuration and dependency health without printing secrets or exact observer coordinates. It checks version/Python compatibility, Telegram, MongoDB, ADS-B provider reachability, optional local ADS-B, stored coordinate ranges, radius/cadence settings and contradictory configuration.

Self-hosting, Raspberry Pi/ARM64, upgrade and rollback instructions are maintained in [`docs/self-hosting.md`](docs/self-hosting.md). Release changes are summarized in [`CHANGELOG.md`](CHANGELOG.md). Problems can be reported through the repository issue templates.

## Storage resilience

MongoDB remains the primary production persistence layer, but it is not treated as the authority for every individual five-second computation.

### Last-known-good configuration

Active location/preferences/admin-control data is loaded as an atomic configuration image. Location and preference documents must share the same `config_revision`; mixed old/new pairs are not published to the live worker.

Successful active-profile materialization invalidates the cached image so it is refreshed promptly. If the refresh fails, Plane Alerts retains the previous verified image rather than replacing it with partial state.

A cold process without a verified configuration does **not** invent monitoring state during a database outage.

### Encounter state and duplicate protection

Active approach/alert lifecycle state is cached in memory and restored from Mongo at startup when available. Lifecycle updates are applied to memory immediately and coalesced for bounded background persistence.

Persisted writes use timestamp guards so delayed work cannot silently overwrite a newer lifecycle record. Delivery state still follows the v4.4 rule: a Telegram lifecycle notification is not considered delivered until Telegram delivery succeeds.

A warm process can therefore continue trajectory, CPA, qualification, cancellation and pass detection while Mongo is temporarily unavailable. A process restart during a simultaneous Mongo outage cannot reconstruct state that never reached durable storage; readiness stays conservative instead of guessing.

### Bounded queues and backpressure

Storage work has explicit limits:

- encounter-state cache: 4,096 records
- critical encounter persistence queue: 4,096 coalesced records
- system-status queue: 32 coalesced records
- optional analytical queue: 512 coalesced records
- Mongo connection pool: 5 connections
- background writes: bounded batches with per-operation timeout
- outage retries: bounded exponential backoff

Under pressure, optional/diagnostic work is dropped before live processing. Critical-state pressure is surfaced explicitly in diagnostics rather than allowing unbounded memory growth.

Existing bounded route-history, provider-learning, Prediction Lab and enrichment workers remain isolated from the live alert path.

### Database degraded mode

When Mongo becomes unavailable after a verified warm start, Plane Alerts can continue:

- ADS-B ingestion and provider failover
- trajectory calculation
- CPA / ETA / confidence
- active qualification and cancellation
- physical-pass detection
- Telegram alerts for users whose active configuration is already known

Features that require new durable state may be temporarily unavailable or delayed, including profile/settings writes, historical persistence, Prediction Lab writes, analytics and other optional evidence.

Telegram settings failures return a clear temporary-unavailability message. The application never pretends a setting was saved when persistence failed.

`/health` and `/ready` distinguish application readiness from database degradation. A warm live worker may remain operational while `database_degraded` is true; a cold process without verified configuration is not marked ready.

### Storage diagnostics

Health output includes bounded, content-free storage telemetry:

- database state
- read latency p50 / p95 / p99 / max
- write latency p50 / p95 / p99 / max
- failures and timeouts
- retry and reconnect counts
- critical and optional queue depth
- optional/critical dropped-write counters
- last successful database command
- expected schema version

Queries, document bodies, credentials and precise user coordinates are not included in these diagnostics.

## Schema migrations and backup/export

Plane Alerts uses explicit, versioned Mongo schema migrations. Migrations have a unique version and ID, are restart-safe/idempotent where practical, verify after application, and refuse a version whose recorded migration ID does not match the expected migration.

The storage export/import utility is allow-listed rather than a raw database dump. Exact location documents are excluded by default and require explicit opt-in. Secret-like fields are removed. Import validates the export format/schema, rejects unsupported collections and malformed records, uses natural identities to avoid duplicates, and does not overwrite a record that is demonstrably newer.

Error Museum evidence is not expired or downsampled.

## SQLite status

SQLite persistence for self-hosted user/configuration state remains intentionally disabled. The mutable persistence model is Mongo-centric and a second partial backend would introduce feature divergence and restart-safety risk.

Plane Alerts does use a separate read-only SQLite database for the compiled worldwide airport/runway reference catalogue. That static database is unrelated to user/alert persistence.

## Airport, runway and terminal intelligence

The v4.7 family remains intact. Plane Alerts uses a local worldwide aviation-reference database built from a commit-pinned OurAirports snapshot and maintained overrides. Runtime monitoring does not call OurAirports or MongoDB for static airport/runway geometry.

Terminal evidence includes sustained vector changes, downwind/base transitions, final intercept, holding-like behavior, go-arounds and missed approaches. Broad runway/base/final/holding suppression hypotheses remain shadow-only.

The v4.7.3 initial terminal-arrival guard remains authoritative only until the first successful Telegram delivery. It requires fresh physical arrival evidence, cannot be triggered by destination metadata alone, releases for genuine physical entry/go-around/trajectory contradiction, and uses landing geometry that ends at the far runway end. The LTBA/ISL Error Museum regressions and LTFM protections remain release gates.

Fresh live geometry remains authoritative.

## ADS-B providers and local receiver support

Plane Alerts can combine public ADS-B providers with an optional local readsb/dump1090-compatible receiver. Local data is preferred when healthy and fresh; bounded public-provider participation preserves fallback and wider coverage.

Supported local receiver families include readsb, dump1090, dump1090-fa and ultrafeeder/tar1090 aircraft JSON.

```env
LOCAL_ADSB_URL=http://receiver.local
LOCAL_ADSB_RECEIVER_TYPE=auto
LOCAL_ADSB_TIMEOUT_SECONDS=0.8
LOCAL_ADSB_MAX_POSITION_AGE_SECONDS=12
LOCAL_ADSB_AUTH_HEADER=
```

Leave `LOCAL_ADSB_URL` blank for public-only operation. Do not embed credentials in the URL.

Provider state tracks latency, failures, timeouts, malformed responses, stale-position rate, rate limits and bounded circuit-breaker cooldown. Multiple sources reporting the same ICAO24 are merged deterministically with provenance instead of depending on request-completion order.

## Profiles and aircraft filtering

`/profiles` manages persistent alert profiles. Each profile can have its own location, radius, aircraft selection and advanced rules. Profiles can be created, activated, edited, renamed, duplicated and deleted.

Advanced filtering inherits deterministically:

```text
Profile defaults
  -> Category override
  -> Aircraft-specific override
```

Selecting an aircraft never bypasses trajectory, CPA, confidence or lifecycle checks.

## Prediction Lab and Error Museum

Prediction Lab records forecasts before outcomes are known and later compares them with observed behavior. Missing ADS-B coverage remains unresolved rather than counted as a hit or miss. Longer-range 30–60 minute expectations remain shadow-only until enough trustworthy outcomes exist.

Database outage does not turn a missing outcome write into a successful prediction or a miss. Error Museum fixtures remain permanent regression evidence and are not subject to transient-data TTL cleanup.

## Photography and contrails

Plane Alerts provides deterministic spotting guidance for camera settings, framing, sun position, atmospheric conditions, upper-air conditions, contrail probability and shooting-window timing.

Google Contrails, when configured through `GOOGLE_CONTRAILS_API_KEY`, remains optional enrichment. Its failure or storage cache cannot influence trajectory, CPA, ETA, confidence, airport/runway inference, qualification, cancellation or alert timing.

## Telegram commands

| Command | Purpose |
| --- | --- |
| `/start` | Initial setup |
| `/profiles` | Create, switch and manage alert profiles |
| `/status` | Show monitoring status |
| `/next60` | Aircraft expected in the next 60 minutes |
| `/forecast` | Alias for `/next60` |
| `/location` | Update the active spotting location |
| `/preferences` | Configure aircraft selection and advanced filters |
| `/camera` | Configure camera body |
| `/lens` | Configure lens |
| `/photo` | Current shooting guidance |
| `/conditions` | Weather, sun and atmospheric information |
| `/spotting` | Spotting tools |
| `/help` | Command help |

The private `/agy` console is owner-only and does not control live physical prediction.

## Testing and release gates

CI compiles the application, builds/verifies the pinned airport database, runs the full pytest suite, preserves all inherited Error Museum/provider/Telegram/terminal/storage regressions, and executes deterministic performance gates.

v4.9 additionally verifies the exact dependency lock, Docker Compose configuration, `planealerts doctor`, a fresh amd64 image and an ARM64 build path.

```text
python -m compileall -q app vercel_runtime worker.py
python scripts/build_airport_database.py
python scripts/verify_airport_database.py
pytest -q
pytest -q tests/test_self_hosting_v49.py
python scripts/benchmark_v49_doctor.py
docker compose config --quiet
pip check
```

All prior release regression and benchmark commands remain enforced in GitHub Actions.

## Deployment

Production runs on Railway. Deployment occurs only after the exact `main` commit passes CI and final engineering review. The deployed build reports its exact commit through runtime metadata; deployment SHAs are not hard-coded in source.

The main service runs Telegram, shared ADS-B polling, deterministic prediction, route history, photography intelligence and Next60. A separate AGY service performs post-outcome investigation and may suggest hypotheses, but it does not control trajectory or notification decisions. Railway AI is not used.

The existing AGY persistent volume and unrelated staged Railway configuration changes are not modified as part of a normal source release.

## Running locally

The supported self-hosting path is Docker Compose:

```bash
git clone https://github.com/xtenrore/Plane-Alerts.git
cd Plane-Alerts
cp .env.example .env
docker compose build plane-alerts
docker compose run --rm plane-alerts planealerts doctor --offline
docker compose up -d
```

For native Python and Raspberry Pi/ARM64 instructions, health checks, backups, upgrades and rollback, see [`docs/self-hosting.md`](docs/self-hosting.md).

## Known limitations

- A cold restart during a complete Mongo outage cannot safely reconstruct configuration or a just-delivered alert that was never durably persisted; readiness remains false rather than fabricating state.
- Optional analytics can be dropped under prolonged storage pressure and are counted in diagnostics.
- Mutable SQLite persistence is not implemented.
- The worldwide airport catalogue is community-maintained; maintained Plane Alerts overrides and fresh physical observations take precedence where stronger evidence exists.
- Broader runway/history suppression remains shadow-only pending representative outcome evidence.
- Raspberry Pi validation is automated ARM64 Docker build validation; it is not a claim of testing every physical Pi model, receiver or storage device.
