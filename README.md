# Plane Alerts

Plane Alerts is a Telegram-based aircraft spotting alert system. It combines live ADS-B observations with deterministic trajectory, closest-point-of-approach (CPA), ETA, route-history and terminal-area logic so alerts are based on whether an aircraft is actually expected to pass the observer, not proximity alone.

AI is not part of the live qualification path. It does not decide trajectory, CPA, ETA, confidence, pass/no-pass, aircraft filtering, turn prediction, cancellation or notification timing.

**Current code version: Plane Alerts v4.6.0**

Telegram: **[@planebotnotifierbot](https://t.me/planebotnotifierbot)**

## v4.6 — Prediction Intelligence & Instant Interaction

v4.6 improves how Plane Alerts describes uncertainty without replacing the proven production CPA geometry.

### Confidence and position uncertainty

Each prediction has an evidence-based user-facing confidence state:

- High
- Medium
- Low
- Uncertain

The deterministic confidence layer considers observation freshness and count, update cadence, heading and groundspeed stability, turn evidence, acceleration, distance trend, prediction horizon, missing fields and estimated positional uncertainty.

Freshness is speed-dependent and bounded. A fast jet can become too uncertain sooner than a slow aircraft because the same observation age implies a larger possible position error. Confidence diagnostics retain the reasons for degradation rather than exposing fake probability precision.

### Turn-aware shadow prediction

The established production predictor still controls alerts. v4.6 also runs bounded linear and turn-aware candidates in shadow mode for outcome comparison.

A turn candidate requires multiple fresh observations with consistent direction and rate. One heading spike, stale history or a short noisy sequence cannot establish a turn, and candidate curvature decays rather than being extrapolated indefinitely.

Prediction Lab can compare production and shadow CPA/ETA behavior without allowing the candidate model to qualify, cancel or time an alert.

### Route-history evidence

Historical routes are supporting evidence, not physical truth.

v4.6 adds:

- age decay so older history carries less influence
- bounded background clustering of similar paths
- weaker authority for small samples
- weaker authority when route clusters disagree
- live-geometry authority when history diverges from current observations
- conservative terminal-area uncertainty without treating destination=IST as automatic suppression

Clustering stays outside the five-second monitoring critical path.

### ETA and cancellation safeguards

Plane Alerts retains the existing provider-teleport rejection, heading-spike protection, cancellation confirmation latch and v4.4 direct-presence recovery.

A cancelled encounter can recover from fresh independent physical observations inside the configured radius. Stale or repeated samples cannot resurrect it. v4.6 does not hide bad ETA behavior behind aggressive smoothing.

### Faster Telegram interactions

Callback-based menus acknowledge Telegram callback queries before avoidable database or forecast work where safe. `/next60` no longer rebuilds forecast data before acknowledging the button press.

A bounded profile-state cache avoids repeated reads of already persisted session state. Callback IDs are deduplicated so repeated delivery does not duplicate work, while distinct rapid clicks remain independent.

Interaction telemetry separates Plane Alerts handler latency from Telegram/network latency and records callback acknowledgement, handler completion and message completion distributions with p50/p95/p99/worst values.

## Alert-critical architecture

```text
ADS-B ingestion
  -> canonical fresh observation
  -> deterministic motion history
  -> production trajectory / CPA / ETA
  -> confidence + position uncertainty
  -> terminal / route supporting evidence
  -> qualification / cancellation lifecycle
  -> Telegram alert

                         -> turn-aware candidate (shadow only)
                         -> Prediction Lab outcome evaluation
```

Runtime AI is outside this path.

## ADS-B providers and local receiver support

Plane Alerts can combine public ADS-B providers with an optional local readsb/dump1090-compatible receiver. Local data is preferred when it is healthy and fresh, while bounded public-provider participation preserves fallback and wider coverage.

Supported local receiver families include:

- readsb
- dump1090
- dump1090-fa
- ultrafeeder / tar1090 aircraft JSON

Example configuration:

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

## Prediction Lab

Prediction Lab records forecasts before outcomes are known and later compares them with observed behavior. It is used to investigate CPA error, ETA stability, false or late cancellations, qualify/cancel oscillation, route-history mistakes, terminal-arrival behavior and shadow predictor performance.

Missing ADS-B coverage is unresolved rather than counted as a hit or miss. Longer-range 30–60 minute expectations remain shadow-only until enough trustworthy outcomes exist.

## Photography intelligence

Plane Alerts also provides deterministic spotting guidance for camera settings, framing, sun position, atmospheric conditions, upper-air conditions, contrail probability and shooting-window timing. Photography and weather enrichment do not control alert qualification.

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

Pull-request CI validates the full test suite plus focused replay/provider/interaction suites and deterministic performance benchmarks:

```text
python -m compileall -q app vercel_runtime worker.py
pytest -q
pytest -q tests/test_error_museum_v42.py tests/test_v44_monitor_reliability_replay.py tests/test_eta_stability_hotfix.py
pytest -q tests/test_v45_provider_resilience.py tests/test_v45_local_provider_area_recovery.py
pytest -q tests/test_interaction_v46.py
python scripts/benchmark_v42.py
python scripts/benchmark_v45_provider_resilience.py
python scripts/benchmark_v46_prediction.py
python scripts/benchmark_v46_interaction.py
pip check
```

Regression coverage preserves the v4.4/v4.5 Error Museum, cancellation recovery, terminal-arrival and provider-resilience tests while adding stable/noisy/stale-turn, speed-dependent freshness, position uncertainty, route decay/clustering and Telegram callback latency cases.

## Deployment

Production runs on Railway with MongoDB persistence. Deployment occurs only after the exact `main` commit passes CI. The deployed build reports its exact commit through runtime metadata; deployment SHAs are not hard-coded in source.

The main service runs Telegram, shared ADS-B polling, deterministic prediction, route history, photography intelligence and Next60. A separate AGY service performs post-outcome investigation and may suggest hypotheses, but it does not control trajectory or notification decisions. Railway AI is not used.

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
