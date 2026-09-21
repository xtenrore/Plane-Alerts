# Plane Alerts

Plane Alerts is a Telegram-based aircraft spotting alert system. It combines live ADS-B observations with deterministic trajectory, closest-point-of-approach (CPA), ETA, confidence, route-history and terminal-area logic so alerts are based on whether an aircraft is actually expected to pass the observer, not proximity alone.

AI is not part of the live qualification path. It does not decide trajectory, CPA, ETA, confidence, pass/no-pass, runway use, terminal state, cancellation or notification timing.

**Current code version: Plane Alerts v5.4.2**  
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
  -> v4.7.3 terminal-arrival delivery guard
  -> qualification / cancellation lifecycle
  -> Telegram delivery
```

v4.8 keeps non-critical persistence outside that path. Once a process has loaded a verified active configuration, live monitoring uses bounded in-memory copies of active user/profile configuration and encounter lifecycle state. Mongo refresh and persistence happen on bounded background loops.

A slow analytics write must not turn the five-second monitoring interval into a ten-second interval.

## v5.4.2 general reliability audit fixes

v5.4.2 fixes the remaining non-AGY findings from the verified 2026-09-21 general audit. First-time setup only enables monitoring after a successful coherent profile save; partial profile materialization keeps the previous per-user last-known-good generation and can repair a torn generation from the authoritative active profile after restart.

Self-hosting now accepts a blank optional `ADMIN_TELEGRAM_ID`, sensitive admin APIs fail closed when `ADMIN_PASSWORD` is blank, and rollout/container readiness uses `/ready` rather than liveness-only `/health`. The v4.6 interaction layer is installed explicitly during fresh runtime composition, and cached live-user loading again queues nonblocking observer-elevation enrichment tied to the exact saved coordinates.

Preset Save callbacks are bound to one draft session, historical photography snapshots retain their real elapsed observation age, and untargeted `/photo` selection uses the same canonical aircraft filter as alerts. Effective inherited altitude rules are validated before save, and current help/photography/admin product-version text uses the canonical release identity.

These changes do not intentionally alter trajectory, CPA, ETA, confidence, terminal inference, qualification, cancellation, alert timing or provider polling. The physical prediction version remains `5.3-3d-proximity-age-aware`.

## v5.4.1 AGY reliability hotfix

v5.4.1 is limited to AGY reliability and release identity. It preserves durable AGY quota waits across restarts, deployment/startup overrides, force-run tokens, enable changes and tooling-recovery paths; unknown quota reset text now remains blocked instead of assuming a recovery window, while verified reset times always include the required ten-minute guard.

AGY context refresh now isolates Mongo failures per collection so a timeout in one evidence source cannot starve later inputs. Cached evidence exposes per-collection freshness, profile auditing reads the authoritative `profiles` collection, and `/findings` pagination returns the earliest next bounded page without skipping older unhandled sequences.

These changes do not alter trajectory, CPA, ETA, confidence, terminal inference, qualification, cancellation, alert timing or provider polling. The physical prediction version remains `5.3-3d-proximity-age-aware`. Normal Plane Alerts deployment still excludes the AGY Railway service; AGY deployment is handled separately under the no-paid-credit quota-safety rules.

## v5.4 user experience and presets

v5.4 makes the existing advanced profile system easier and safer to configure without changing the physical prediction model. `/profiles` now provides clearer profile navigation, explicit Back/Cancel/Close/Status paths, and guided creation through either Quick Preset or the full Custom Setup flow.

Six editable starting presets are included: Casual Observer, Aircraft Photographer, Airport-Adjacent, Rare Aircraft Hunter, Military Watcher, and Local SDR Mode. Presets only populate existing profile configuration such as monitoring radius and aircraft groups; saved coordinates and unrelated preferences are preserved, and every preset value remains editable afterward. Local SDR Mode changes alert preferences only and does not enable or configure a receiver.

`/preferences` opens the active profile editor instead of dropping directly into one sub-menu. `/setup` is non-destructive: reopening setup no longer deletes a working location or preferences before replacement settings are saved. The active profile remains in force until the user explicitly saves changes or confirms a preset.

Preset validation routes users directly to missing location, radius or aircraft settings. Stale profile/preset callbacks recover to the profile home instead of trapping the conversation. Telegram labels and callback payloads remain compact, and the existing Next 60 Mini App now consistently uses Plane Alerts branding.

v5.4 does not change trajectory, CPA, ETA, confidence, terminal inference, qualification, cancellation, alert timing or provider polling. The physical prediction version therefore remains `5.3-3d-proximity-age-aware`.

## v5.3 multi-location and scale

v5.3 reduces duplicated computation when many observers share the same regional ADS-B snapshot. Observer-independent aircraft motion is projected once and reused through bounded process-local caches, including the authoritative v4.3 midpoint-integrated motion path. The established production wrapper order remains `shared base -> v4.3 midpoint -> v4.4 direct presence -> v4.6 confidence -> critical timing`.

A bounded spatial index filters geographically impossible user/aircraft pairs before per-user prediction. The grid is only an acceleration structure: exact spherical great-circle membership against the existing `radius + 120 km` monitoring envelope remains authoritative, and every surviving candidate still receives its own horizontal CPA, 3D relevance, ETA, confidence, route/terminal, qualification, cancellation and lifecycle evaluation.

Motion caches are limited to 1,024 entries and contain no user coordinates or lifecycle state. Per-user candidate work is explicitly bounded and surfaced in scale diagnostics. Existing shared provider snapshots and enrichment caches are reused, so v5.3 does not add another ADS-B polling layer or multiply provider calls.

v5.3 also fixes two evidence-backed live-prediction issues. Provider-reported ADS-B position age is now applied to the midpoint ETA/entry timing path, removing a reproducible late-ETA bias identified by AGY seq140 and aligning the wrapper with the base freshness semantics. A stale observation inside the configured radius can no longer clear a cancellation latch; only fresh direct physical presence can recover it. Because the first change affects physical ETA output, the prediction version advances to `5.3-3d-proximity-age-aware`. Terminal-arrival thresholds, qualification thresholds and alert-delivery timing policy are unchanged.

## v5.2 shadow models and automatic evaluation

v5.2 makes prediction development data-driven without changing the live physical predictor. The authoritative physical prediction version for that release was `5.1-3d-proximity`; existing v4.6 linear and turn-aware candidate models continue to run as shadow-only evidence and cannot qualify, cancel, time or send alerts.

Prediction Lab snapshots record application release version and shadow feature-flag state. When a later outcome has explicit scoreable ground truth, Plane Alerts can evaluate the production control and shadow candidates side by side for CPA error, ETA error, false-positive/false-negative behavior, alert lead time and confidence calibration. Results are grouped by release and model ID so changes can be compared over time.

Ground truth remains conservative. An observed in-radius pass is scoreable. A lifecycle cancellation is not automatically a successful negative outcome, because the aircraft may later pass nearby. Missing ADS-B coverage is unresolved. If a denominator does not exist, the corresponding rate remains unknown rather than being reported as zero.

ETA error is scored only when the actual closest physical observation has a matched timestamp. If that timestamp is unavailable, CPA/classification evidence may still be scoreable, but ETA remains unavailable rather than substituting lifecycle-resolution time.

Shadow evaluation is isolated in the existing bounded Prediction Lab optional-work path and adds no ADS-B provider request or synchronous operation to the five-second alert-critical calculation. Evaluation records have deterministic IDs, bounded lookup windows and a 14-day TTL.

Shadow feature flags are evaluation-only:

```env
PLANE_SHADOW_EVALUATION_ENABLED=true
PLANE_SHADOW_V46_LINEAR_ENABLED=true
PLANE_SHADOW_V46_TURN_ENABLED=true
```

These flags can disable evaluation candidates; they cannot select the model used for live alerts.

Operators can inspect the bounded evaluation report with:

```bash
planealerts shadow-eval
planealerts shadow-eval --days 7 --limit 2000
```

Candidate promotion is never automatic. A candidate must first have enough scoreable samples, representative horizon coverage, no major safety regression, successful replay evidence and successful live-shadow evidence. Even then the evaluator only marks it eligible for engineering review.

## v5.1 3D proximity and advanced geometry

v5.1 keeps horizontal CPA as an explicit, always-visible result and adds deterministic altitude-aware relevance rather than replacing the proven horizontal model. When altitude evidence is physically plausible, Plane Alerts calculates vertical separation, true three-dimensional/slant CPA, time to 3D CPA, and the horizontal and vertical components at that closest point.

Altitude may suppress an otherwise horizontally qualifying approach only when the altitude evidence is trustworthy and a conservative uncertainty-adjusted 3D lower bound still proves the aircraft remains outside the configured radius. Missing, stale, malformed or discontinuous altitude fails open to the established horizontal result. This prevents bad altitude data from silently suppressing a real nearby pass.

Observer terrain elevation is optional. If it is not already stored, Plane Alerts schedules a free Open-Meteo terrain lookup through the existing bounded optional-enrichment worker; the live predictor never waits for that network request. Until observer elevation is known, a conservative global terrain envelope is used and ambiguous cases retain horizontal qualification.

Altitude relevance can be disabled per preferences through `proximity_3d.altitude_relevance`. Horizontal CPA remains available regardless of this setting.

v5.1 also tightens pass-versus-cancellation semantics. A close projected CPA is not ground truth: an active encounter is finalized as passed only after Plane Alerts has actually observed the aircraft inside the configured radius and a later fresh observation shows it receding from the observed closest point. Missing or stale ADS-B never counts as a completed pass.

v5.1.1 is a release-infrastructure-only patch. It keeps the same physical predictor and moves the exact-main Railway deployment gate out of the Railway CLI container so the trusted post-CI deploy can verify the tested SHA without depending on tools missing from that container.

v5.1.2 fixes the production wrapper-chain compatibility bug discovered during v5.1.1 verification. The installed v4.6 confidence layer accepts and forwards the v5.1 `altitude_relevance` option and preserves unknown observer elevation, so the intended `5.1-3d-proximity` model can execute through the monitor/critical-timing layer.

v5.1.3 completes that repair after v5.1.2 production verification exposed the older v4.4 direct-presence and v4.3 midpoint wrappers beneath v4.6. Both accept and forward the current v5.1 signature. The release gate recreates the full installed chain — core v5.1 trajectory -> v4.3 midpoint -> v4.4 direct presence -> v4.6 confidence -> critical timing — including unknown observer elevation and both enabled/disabled altitude relevance. The physical prediction version and all CPA/ETA, terminal, qualification and alert-timing thresholds remain unchanged.

## v5.0 observability and explainability

v5.0 makes the existing system understandable without creating a second predictor. The read-only `planealerts diagnostics` command explains recent aircraft decisions using bounded Prediction Lab evidence: trajectory state, current distance versus projected CPA, ETA, confidence, observation freshness, active/candidate ADS-B providers, route/terminal evidence and runway candidates where available.

`planealerts metrics` shows persisted provider request/error/timeout counts, latency percentiles, last success, stale-position rate, circuit state, monitor timing, storage latency, bounded queue/drop state and notification telemetry. These diagnostics are attached to the monitor's existing system-status heartbeat, so v5.0 adds no ADS-B request and no additional synchronous Mongo write to the five-second path.

Operator examples:

```bash
planealerts metrics
planealerts diagnostics --aircraft 4bab24 --limit 5
planealerts diagnostics --callsign THY5DQ --limit 10
```

User IDs are pseudonymized in the operator output. Credentials, provider endpoints and exact observer coordinates are not returned. Prediction Lab counts are explicitly described as sampled/rate-limited rather than as a count of every five-second calculation.

The AGY sidecar is storage-resilient in v5.0. Mongo reads have short bounded timeouts and query max-time. A network failure opens a cooldown circuit; while degraded, the bridge immediately reuses the last-known-good redacted context on `/agy-state` and continues independent `CHATGPT_HANDOFF_JSON` log delivery instead of repeatedly blocking and printing Mongo traceback storms.

## v4.9 project maturity and self-hosting

v4.9 makes Plane Alerts reproducible outside Railway without creating a second prediction implementation. Runtime Python dependencies are exactly pinned, Docker builds support amd64 and ARM64, and `docker-compose.yml` provides a health-checked MongoDB with a persistent volume and restart policies.

The read-only `planealerts doctor` command validates important configuration and dependency health without printing secrets or exact observer coordinates. It checks version/Python compatibility, Telegram, MongoDB, ADS-B provider reachability, optional local ADS-B, stored coordinate ranges, radius/cadence settings and contradictory configuration.

Self-hosting, Raspberry Pi/ARM64, upgrade and rollback instructions are maintained in [`docs/self-hosting.md`](docs/self-hosting.md). Release changes are summarized in [`CHANGELOG.md`](CHANGELOG.md). Problems can be reported through the repository issue templates.

## Storage resilience

MongoDB remains the primary production persistence layer, but it is not treated as the authority for every individual five-second computation.

### Last-known-good configuration

Active location/preferences/admin-control data is loaded as an atomic configuration image. Location and preference documents must share the same `config_revision`; mixed old/new pairs are not published to the live worker.

Successful active-profile materialization invalidates the cached image so it is refreshed promptly. If one write of a new configuration generation fails, the authoritative active profile remains on the previous committed config and a warm worker preserves that user's prior coherent last-known-good image instead of dropping the user. On restart, a torn legacy materialization can be repaired from the authoritative active profile before it is admitted to the cache.

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

`/health` and `/ready` distinguish application readiness from database degradation. A warm live worker may remain operational while `database_degraded` is true; a cold process without verified configuration is not marked ready. Rollout/container health uses `/ready`.

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

v5.4 adds six editable presets and two clear profile-creation paths: Quick Preset and Custom Setup. Applying a preset to an existing profile requires confirmation, preserves its saved location, and resets hidden category/aircraft overrides so the visible preset behaves as described. `/setup` and `/preferences` reopen the active configuration safely instead of destroying or bypassing the existing profile state.

Advanced filtering inherits deterministically:

```text
Profile defaults
  -> Category override
  -> Aircraft-specific override
```

v5.4.2 validates the effective inherited altitude range before saving. Selecting an aircraft never bypasses trajectory, CPA, confidence or lifecycle checks.

## Prediction Lab and Error Museum

Prediction Lab records forecasts before outcomes are known and later compares them with observed behavior. v5.2 evaluates the current production control and existing shadow candidates only when a later outcome carries explicit scoreable ground truth. Missing ADS-B coverage remains unresolved rather than counted as a hit or miss. Longer-range 30–60 minute expectations remain shadow-only until enough trustworthy outcomes exist.

Lifecycle cancellation alone is not ground truth for false-positive or cancellation-accuracy claims. Those metrics remain unavailable until a later coverage-validated negative outcome exists. Database outage does not turn a missing outcome write into a successful prediction or a miss. Error Museum fixtures remain permanent regression evidence and are not subject to transient-data TTL cleanup.

## Photography and contrails

Plane Alerts provides deterministic spotting guidance for camera settings, framing, sun position, atmospheric conditions, upper-air conditions, contrail probability and shooting-window timing.

Untargeted `/photo` selection uses the active profile's canonical aircraft filter. A retained notification snapshot is historical unless fresh live observations replace it; elapsed time since capture is included in its position age and a stale snapshot cannot manufacture a current shooting countdown.

Google Contrails, when configured through `GOOGLE_CONTRAILS_API_KEY`, remains optional enrichment. Its failure or storage cache cannot influence trajectory, CPA, ETA, confidence, airport/runway inference, qualification, cancellation or alert timing.

## Telegram commands

| Command | Purpose |
| --- | --- |
| `/start` | Initial setup |
| `/setup` | Safely review or change the active alert setup |
| `/profiles` | Create, switch and manage alert profiles |
| `/status` | Show monitoring status |
| `/next60` | Aircraft expected in the next 60 minutes |
| `/forecast` | Alias for `/next60` |
| `/location` | Update the active spotting location |
| `/preferences` | Edit the active alert profile |
| `/camera` | Configure camera body |
| `/lens` | Configure lens |
| `/photo` | Current shooting guidance |
| `/conditions` | Weather, sun and atmospheric information |
| `/spotting` | Spotting tools |
| `/help` | Command help |

The private `/agy` console is owner-only and does not control live physical prediction.

## Testing and release gates

CI compiles the application, builds/verifies the pinned airport database, runs the full pytest suite, preserves all inherited Error Museum/provider/Telegram/terminal/storage regressions, and executes deterministic performance gates.

v4.9 additionally verifies the exact dependency lock, Docker Compose configuration, `planealerts doctor`, a fresh amd64 image and an ARM64 build path. v5.0 additionally gates operator explainability, AGY Mongo timeout fallback, diagnostics formatting overhead, fresh-image CLI availability and all inherited self-hosting checks. v5.1 additionally gates low/high-altitude geometry, overhead and crossing passes, climb/descent, missing or anomalous altitude, unknown observer elevation, configurable altitude relevance, observed-pass lifecycle semantics and deterministic 3D geometry overhead. v5.1.2 adds predictor-option compatibility coverage for v4.6 and critical timing. v5.1.3 extends that regression to the actual installed production stack: core v5.1 trajectory -> v4.3 midpoint -> v4.4 direct presence -> v4.6 confidence -> critical timing. v5.2 adds automatic shadow-evaluation regressions, a replay fixture that refuses unresolved cancellation/missing-coverage scoring, `shadow-eval` fresh-image validation and a deterministic evaluation-overhead benchmark. v5.3 adds multi-user state-isolation, brute-force spatial equivalence, fresh-interpreter production-wrapper composition, bounded-memory/work, AGY provider-age/stale-latch regressions, and a 500-user/600-aircraft deterministic scale gate. v5.4 adds six-preset mapping/editability checks, non-destructive setup recovery, navigation/stale-callback recovery, Mini App branding validation, callback-size checks, a deterministic preset/profile micro-benchmark, and fresh-image v5.4 runtime wiring verification. v5.4.1 adds offline AGY quota-hold/parser/tooling-recovery regressions, per-collection Mongo fairness/freshness checks, authoritative-profile context coverage, and lossless findings-pagination coverage without launching AGY inference. v5.4.2 adds exact regressions for setup activation, torn profile generations, fail-closed admin access, rollout readiness, fresh-import interaction wiring, elevation enrichment, stale preset callbacks, historical photography freshness, canonical photo filtering, inherited altitude validation and canonical product-version displays.

```text
python -m compileall -q app vercel_runtime worker.py
python scripts/build_airport_database.py
python scripts/verify_airport_database.py
pytest -q
pytest -q tests/test_general_audit_fixes_v542.py
pytest -q tests/test_shadow_evaluation_v52.py
pytest -q tests/test_scale_v53.py tests/test_runtime_scale_v53.py tests/test_v53_agy_regressions.py tests/test_v35_shared_polling.py
pytest -q tests/test_user_experience_v54.py
pytest -q tests/test_agy_audit_fixes.py tests/test_agy_prediction_lab.py tests/test_agy_mongo_resilience_v50.py
python scripts/evaluate_v52_shadow_replay.py
python scripts/benchmark_v54_profile_ux.py
python scripts/benchmark_v53_scale.py
python scripts/benchmark_v52_shadow_eval.py
python scripts/benchmark_v51_geometry.py
python scripts/benchmark_v50_observability.py
python scripts/benchmark_v49_doctor.py
docker compose config --quiet
pip check
```

All prior release regression and benchmark commands remain enforced in GitHub Actions.

## Deployment

Production runs on Railway. Deployment occurs only after the exact `main` commit passes CI and final engineering review. The deployed build reports its exact commit through runtime metadata; deployment SHAs are not hard-coded in source.

The trusted post-CI deployment gate first verifies on a standard GitHub-hosted runner that the successful workflow came from this repository's `main` push and that the tested SHA is still the current `main` head. Only then does the separate Railway CLI container receive and deploy that exact SHA.

The main service runs Telegram, shared ADS-B polling, deterministic prediction, route history, photography intelligence and Next60. A separate AGY service performs post-outcome investigation and may suggest hypotheses, but it does not control trajectory or notification decisions. Railway AI is not used.

Normal Plane Alerts source releases deploy the main service only. The AGY service, its persistent volume, and unrelated staged Railway configuration changes are not restarted or modified as part of this release path.

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
