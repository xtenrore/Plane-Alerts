# Plane Alerts

Plane Alerts is a Telegram-based aircraft spotting alert system. It combines live ADS-B observations with deterministic trajectory, closest-point-of-approach (CPA), ETA, route-history and terminal-arrival logic so alerts are based on whether an aircraft is actually expected to pass the observer, not proximity alone.

AI is not part of the live qualification path. It does not decide trajectory, CPA, ETA, pass/no-pass, aircraft filtering, cancellation or notification timing.

**Current code version: Plane Alerts v4.5.0**

Telegram: **[@planebotnotifierbot](https://t.me/planebotnotifierbot)**

## v4.5 — Local ADS-B and provider resilience

v4.5 keeps the v4.4 prediction behavior and moves aircraft ingestion toward a local-first, multi-provider architecture. A local receiver is optional: a Railway deployment with no local receiver configured continues to use the existing public ADS-B feeds.

### Provider architecture

```text
Optional local receiver       Public ADS-B providers
readsb / dump1090             adsb.lol / adsb.fi
 dump1090-fa / ultrafeeder    airplanes.live / adsb.one / OpenSky
          \                         /
           -> canonical observations
           -> freshness + provenance
           -> deterministic ICAO merge
           -> existing trajectory / CPA / qualification / alerts
```

A configured, healthy and fresh local receiver is preferred for position data. One rotating public source still participates in the bounded shared-region refresh so Plane Alerts retains fallback, outside-local-coverage data and safe metadata enrichment. A failed local receiver cannot block the five-second monitoring path.

Provider switching does not change aircraft identity: the live prediction state continues to use ICAO24 as the aircraft key. Multiple providers reporting the same ICAO24 are merged deterministically instead of allowing the last completed HTTP request to overwrite earlier data.

### Canonical observations and provenance

Normalized observations can retain:

- ICAO24 and callsign
- observed and received timestamps
- provider/source identity
- latitude and longitude
- altitude, groundspeed, track and vertical rate
- position age/source freshness
- aircraft type where supplied
- field-level provenance after a safe multi-provider merge

Missing values stay missing. Plane Alerts does not invent an aircraft observation timestamp when a provider does not supply a usable position time or position age. Motion data from sources separated too far in time is not combined into a synthetic state.

### Provider health and failover

Each provider tracks concrete evidence rather than one opaque score, including recent successes/failures, latency, timeouts, malformed responses, stale-position rate, rate-limit events, aircraft count, consecutive failures, circuit state and cooldown time.

Repeated failures open a bounded circuit breaker. During cooldown Plane Alerts skips that source rather than continuously hammering it. When cooldown expires, the provider enters a recovery probe state; a successful request closes the circuit automatically. HTTP 429 responses also enter bounded cooldown and honor a usable `Retry-After` value.

The existing critical-path provider deadline remains in place. Timeouts now feed the provider circuit state, so repeated OpenSky or other provider stalls are isolated instead of retried indefinitely every cycle.

### OpenSky geography

OpenSky bounding boxes use latitude-aware longitude scaling. Regions that cross the ±180° date line are split into legal requests, and near-pole searches use bounded global-longitude coverage rather than generating invalid coordinates.

Regression coverage includes the equator, mid-latitudes, Istanbul latitude, high latitudes, both sides of the date line and near-pole behavior.

## Optional local ADS-B configuration

Plane Alerts supports the commonly exposed aircraft JSON output from:

- readsb
- dump1090
- dump1090-fa
- ultrafeeder / tar1090 deployments

Set either the receiver base URL or the full `aircraft.json` URL:

```env
LOCAL_ADSB_URL=http://receiver.local
LOCAL_ADSB_RECEIVER_TYPE=auto
LOCAL_ADSB_TIMEOUT_SECONDS=0.8
LOCAL_ADSB_MAX_POSITION_AGE_SECONDS=12
LOCAL_ADSB_AUTH_HEADER=
```

`LOCAL_ADSB_RECEIVER_TYPE` may be `auto`, `readsb`, `dump1090`, `dump1090-fa` or `ultrafeeder`. If the configured URL is a base URL, Plane Alerts checks the common `data/aircraft.json`, `tar1090/data/aircraft.json` and `aircraft.json` locations within the same bounded request path.

Do not place credentials in `LOCAL_ADSB_URL`. If a private gateway requires authentication, `LOCAL_ADSB_AUTH_HEADER` can contain the complete Authorization header value. Provider diagnostics never emit that secret or the configured local URL.

Leave `LOCAL_ADSB_URL` blank for public-only operation. No local ADS-B hardware is required to run Plane Alerts.

### Diagnostics

Structured provider logs show which providers participated in a refresh and which source supplied merged aircraft positions. Provider status includes circuit/cooldown state, latency, recent failures, stale-position rate and rate-limit state. Merge rejection logs identify stale or conflicting source positions without exposing local receiver credentials or URLs.

The v4.5 local receiver integration is covered by realistic readsb/dump1090-style fixtures and simulated disconnect, malformed-data, recovery and disagreement tests. Physical SDR hardware validation is still environment-specific and is not claimed by the automated test suite.

## Reliability foundations retained from v4.4

The alert-critical path remains:

```text
ADS-B ingestion -> freshness validation -> trajectory -> CPA -> qualification -> alert
```

Route-history persistence, provider learning, photography, weather, Prediction Lab and auditing remain lower-priority optional work. Shared regional polling, continuity snapshots, bounded histories, notification deduplication, cancellation/requalification guards and the IST terminal-arrival regression suite are preserved.

The prediction model identifier remains `4.4-observation-confirmations`; v4.5 changes ingestion/provider resilience rather than redesigning prediction behavior.

## Profiles and aircraft filtering

`/profiles` manages persistent alert profiles. Each profile can hold an independent location, radius, aircraft selection and advanced filtering rules. Profiles can be created, activated, edited, renamed, duplicated and deleted.

Advanced rules follow deterministic inheritance:

```text
Profile defaults
  -> Category override
  -> Aircraft-specific override
```

Selecting an aircraft only expresses user interest; it never bypasses trajectory, CPA, route, arrival or confidence checks.

## Prediction Lab

Prediction Lab records forecasts and later compares them with observed outcomes to investigate ETA stability, CPA error, false or late cancellations, qualify/cancel oscillation, route-history mistakes, terminal-arrival behavior and Next60 quality.

Missing ADS-B coverage is not counted as a hit or a miss. Longer-range 30–60 minute expectations remain shadow-only until enough trustworthy outcomes exist.

## Photography intelligence

Plane Alerts includes deterministic spotting guidance for camera settings, framing, sun position, atmospheric conditions, upper-air conditions, contrail probability and shooting-window timing. Photography and weather enrichment do not control live alert qualification.

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

The private `/agy` console is owner-only and is not part of normal user configuration.

## Testing and release gates

Pull-request CI runs:

```text
python -m compileall -q app vercel_runtime worker.py
pytest -q
python scripts/benchmark_v42.py
python scripts/benchmark_v45_provider_resilience.py
pip check
```

v4.5 adds regression coverage for canonical timestamps, local receiver parsing/disable/failure/recovery, provider timeout and circuit recovery, rate-limit cooldown, deterministic merge order, stale/fresh disagreement, malformed coordinates, date-line and high-latitude OpenSky geography, stable ICAO identity across source switching and bounded concurrent provider work. Existing v4.4 replay/Error Museum and IST/genuine-pass tests remain part of the full suite.

## Deployment

Production runs on Railway with MongoDB persistence. Railway deployment is triggered only after the `main` test workflow succeeds. The deploy workflow checks out that exact tested SHA and embeds the same SHA into the uploaded build, so health/release reporting can identify the running commit.

The main service runs Telegram, shared ADS-B polling, deterministic prediction, route history, photography intelligence and Next60. A separate AGY service performs post-outcome investigation and does not control live trajectory or notification decisions. The Railway AI agent is not used.

## Running locally

Plane Alerts uses Python 3.11 and MongoDB.

```bash
git clone https://github.com/xtenrore/Plane-Alerts.git
cd Plane-Alerts
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Repository layout

```text
app/
  aircraft/          ADS-B providers, canonical observations and filters
  bot/               Telegram commands, profile flows and messages
  intelligence/      trajectory, route history and terminal-arrival logic
  photography/       deterministic camera guidance
  worker/            shared polling, lifecycle and cadence guards
scripts/             deployment, verification and benchmarks
tests/               unit, regression, replay and receiver fixtures
docs/error_museum/   preserved production failure cases
docs/releases/       release notes
.github/workflows/   CI and Railway workflows
```

## Design principle

**Deterministic code decides what is physically happening. AI may investigate or explain evidence, but it does not control the live alert decision.**

ADS-B data can be stale, delayed, duplicated, incomplete or temporarily unavailable. Plane Alerts records uncertainty explicitly instead of inventing certainty.
