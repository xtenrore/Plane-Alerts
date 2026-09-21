# Plane Alerts

Plane Alerts is a Telegram-based aircraft spotting alert system. It combines live ADS-B observations with deterministic trajectory, closest-point-of-approach (CPA), ETA, confidence, route-history and terminal-area logic so alerts are based on whether an aircraft is actually expected to pass the observer, not proximity alone.

AI is not part of the live qualification path. Runtime AI does not decide trajectory, CPA, ETA, confidence, pass/no-pass, airport/runway state, cancellation or notification timing.

**Current code version: Plane Alerts v5.2.1**  
**Current physical prediction version: `5.1-3d-proximity`**

Telegram: **[@planebotnotifierbot](https://t.me/planebotnotifierbot)**

## Alert-critical architecture

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

The monitor targets a five-second cadence. Optional work—analytics, Prediction Lab, photography, weather, historical learning, AGY context and shadow evaluation—uses bounded queues/caches and must not block the live alert path.

## Current release: v5.2.1

v5.2.1 is a documentation-truth patch on top of the production-verified v5.2 shadow-evaluation release. It does not change the physical predictor or live alert behavior.

The important ETA-evaluation rule is now explicit: a shadow-evaluation ETA is scored only when Plane Alerts has a matched physical closest-observation timestamp. If that timestamp is unavailable, verified CPA/classification evidence may still be scored, but ETA remains unavailable. Lifecycle-resolution time is not substituted as physical CPA time.

## v5.2 shadow models and automatic evaluation

The authoritative physical prediction remains `5.1-3d-proximity`. Existing v4.6 linear and turn-aware candidates run shadow-only and cannot qualify, cancel, time or send alerts.

Prediction Lab can compare the control and shadow candidates after a scoreable outcome for:

- CPA error
- ETA error when physical closest time exists
- false-positive / false-negative behavior when valid denominators exist
- cancellation accuracy when later truth is actually resolved
- alert lead time
- confidence calibration
- release-to-release model evidence

Ground truth is intentionally conservative. An observed in-radius pass is scoreable. A lifecycle cancellation is not automatically a successful negative outcome, and missing ADS-B coverage is unresolved rather than counted as a hit or miss.

Shadow evaluation remains isolated in the existing optional Prediction Lab path. Evaluation records are bounded, indexed and TTL-expired after 14 days. Candidate promotion is never automatic.

Operator command:

```bash
planealerts shadow-eval
planealerts shadow-eval --days 7 --limit 2000
```

Evaluation-only feature flags:

```env
PLANE_SHADOW_EVALUATION_ENABLED=true
PLANE_SHADOW_V46_LINEAR_ENABLED=true
PLANE_SHADOW_V46_TURN_ENABLED=true
```

These flags can disable shadow evaluation; they cannot select the live prediction model.

## v5.1 3D proximity

Plane Alerts retains horizontal CPA as an explicit result and adds deterministic altitude-aware relevance. When altitude evidence is physically plausible, it calculates vertical separation, slant/3D CPA and time to 3D CPA.

Missing, stale, malformed or discontinuous altitude fails open to the established horizontal predictor. Observer terrain elevation is optional; enrichment remains outside the alert-critical path. A projected close pass is not treated as ground truth: an encounter is finalized as passed only after fresh physical observations show actual in-radius entry and later recession from the observed closest point.

## Observability

Read-only operator tools:

```bash
planealerts doctor --offline
planealerts diagnostics --aircraft <icao24> --limit 5
planealerts diagnostics --callsign <callsign> --limit 10
planealerts metrics
planealerts shadow-eval
```

Diagnostics expose bounded timing/provider/storage/decision evidence without returning credentials or exact observer coordinates. AGY Mongo reads use short timeouts, a cooldown circuit and last-known-good redacted context so Atlas problems do not create repeated blocking traceback storms.

## Storage resilience

MongoDB is the primary durable store, but live five-second monitoring uses verified in-memory configuration and encounter state after warm startup. Slow persistence is isolated behind bounded queues and coalesced writes.

A warm process can continue live ADS-B ingestion, trajectory/CPA/ETA/confidence, qualification/cancellation, observed-pass detection and Telegram alerts during a temporary Mongo outage. A cold process without a verified configuration does not invent state.

Error Museum evidence is permanent. Missing storage writes or missing ADS-B coverage are never converted into successful or failed prediction outcomes.

## Airport and terminal intelligence

The v4.7 family remains an active release gate. Static airport/runway data is local and does not require live OurAirports calls.

The v4.7.3 initial terminal-arrival guard protects against false first alerts around airports such as LTFM and LTBA/ISL. Destination metadata alone is not enough; the guard requires fresh physical arrival evidence and releases for genuine physical entry, go-around or trajectory contradiction. Fresh live geometry remains authoritative.

## ADS-B providers and local receivers

Plane Alerts can combine public ADS-B providers with an optional local readsb/dump1090-compatible receiver. Duplicate ICAO24 observations are merged deterministically with provenance, while stale/outlier data is rejected or downgraded.

```env
LOCAL_ADSB_URL=http://receiver.local
LOCAL_ADSB_RECEIVER_TYPE=auto
LOCAL_ADSB_TIMEOUT_SECONDS=0.8
LOCAL_ADSB_MAX_POSITION_AGE_SECONDS=12
LOCAL_ADSB_AUTH_HEADER=
```

Leave `LOCAL_ADSB_URL` blank for public-only operation. Do not embed credentials in the URL.

## Profiles and filtering

`/profiles` manages persistent alert profiles. Profiles can carry their own location, radius, aircraft selection and advanced rules. Aircraft filtering never bypasses trajectory, CPA, confidence, terminal protection or lifecycle checks.

Advanced rules inherit deterministically:

```text
Profile defaults
  -> Category override
  -> Aircraft-specific override
```

## Photography and contrails

Plane Alerts also provides deterministic spotting guidance for camera settings, framing, sun position, atmospheric conditions, upper-air conditions, contrail probability and shooting-window timing.

Google Contrails, when configured with `GOOGLE_CONTRAILS_API_KEY`, is optional enrichment only. Its result cannot control trajectory, CPA, ETA, qualification, cancellation or alert timing.

## Telegram commands

| Command | Purpose |
| --- | --- |
| `/start` | Initial setup |
| `/profiles` | Create, switch and manage alert profiles |
| `/status` | Monitoring status |
| `/next60` | Shadow next-hour aircraft expectations |
| `/forecast` | Alias for `/next60` |
| `/location` | Update active spotting location |
| `/preferences` | Aircraft selection and advanced filters |
| `/camera` | Configure camera body |
| `/lens` | Configure lens |
| `/photo` | Current shooting guidance |
| `/conditions` | Weather, sun and atmospheric information |
| `/spotting` | Spotting tools |
| `/help` | Command help |

The private `/agy` console is owner-only and does not control live physical prediction.

## Self-hosting

Plane Alerts supports pinned Python dependencies, Docker, Docker Compose, amd64 and ARM64/Raspberry Pi builds. See [`docs/self-hosting.md`](docs/self-hosting.md).

The standard production release gate includes compilation, the full pytest suite, Error Museum/replay tests, provider and Telegram regressions, terminal-arrival regressions, storage/observability tests, deterministic benchmarks, fresh amd64 image checks, ARM64 build validation, dependency consistency, exact-main CI, exact-SHA Railway deployment and production log/cadence verification.

## Releases

Detailed release notes are under [`docs/releases/`](docs/releases/) and the repository [`CHANGELOG.md`](CHANGELOG.md).

No release is considered complete until its exact tested `main` commit is deployed and production health, version/commit, logs and monitoring cadence are verified.
