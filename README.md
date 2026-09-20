# Plane Alerts

Plane Alerts is a Telegram-based aircraft spotting alert system. It combines live ADS-B observations with deterministic trajectory, closest-point-of-approach (CPA), ETA, confidence, route-history and terminal-area logic so alerts are based on whether an aircraft is actually expected to pass the observer, not proximity alone.

AI is not part of the live qualification path. It does not decide trajectory, CPA, ETA, confidence, pass/no-pass, runway use, terminal state, cancellation or notification timing.

**Current code version: Plane Alerts v4.7.0**

Telegram: **[@planebotnotifierbot](https://t.me/planebotnotifierbot)**

## v4.7 — Airport, Runway & Terminal Intelligence

v4.7 extends the existing v4.2/v4.6 prediction stack instead of introducing a competing predictor.

### Terminal intelligence

Plane Alerts now records deterministic terminal-area evidence for:

- sustained vector changes
- downwind/base transitions
- final intercept and established final
- probable holding behavior
- go-arounds and missed approaches

A single noisy ADS-B heading is not enough to establish a turn. Terminal evidence uses bounded observation history and the existing v4.6 sustained-turn checks.

Airport context produces an explicit uncertainty penalty for diagnostics. Fresh live geometry remains authoritative: being near an airport or having that airport as the route destination is never an automatic suppression rule.

### Runway geometry and inference

Runway geometry lives in a dedicated data layer rather than being scattered through prediction code. The initial maintained production runway set includes Istanbul Airport (LTFM/IST), while the classifier accepts generic single, parallel and crossing-runway layouts.

Likely runway direction is inferred from recent observed traffic only when multiple aircraft support the same direction. The inference retains supporting-aircraft count, recency and confidence; one aircraft cannot create an active-runway conclusion. Configuration changes require fresh multi-aircraft evidence.

Recent airport movement clusters are bounded, deduplicated per aircraft and time-decayed. Historical route paths can also be associated with runway families as supporting evidence. Neither source overrides fresh motion.

### Shadow validation and live safety

New runway/base/final/holding suppression hypotheses are **shadow-only in v4.7.0**. They are recorded in Prediction Lab with their model identifier and do not control live qualification or cancellation.

One narrow fail-safe is authoritative: if an aircraft was being held only because a landing turn was expected, strong observed go-around or missed-approach evidence can invalidate that old expectation and return control to the fresh live trajectory. The confirmed cancellation latch is not bypassed.

This protects both sides of the historical IST problem: normal arrivals can be measured against runway-aware shadow expectations while genuine overhead/transit or go-around passes remain alertable.

### Weather and contrails

Weather remains optional context and is not a hard runway selector. Provider failure must not block ADS-B ingestion, CPA or alerting.

Google Contrails remains separate photography/environment enrichment. It does not influence trajectory, airport classification, runway inference, CPA, ETA, qualification, cancellation or alert timing.

### Known limitations

Runway-aware suppression remains shadow-only until enough replay and production outcomes demonstrate improvement without missed genuine passes. Airports without maintained runway geometry still receive generic terminal context, but runway-specific inference remains uncertain. If maintained runway data becomes incomplete, runway-specific inference is disabled rather than blocking monitoring. Published ATC procedures and official live runway assignments are not treated as physical truth.

## Alert-critical architecture

```text
ADS-B ingestion
  -> canonical fresh observation
  -> deterministic motion history
  -> production trajectory / CPA / ETA
  -> v4.6 confidence + position uncertainty
  -> existing terminal / route guard
  -> qualification / cancellation lifecycle
  -> Telegram alert

ADS-B merged snapshots
  -> bounded v4.7 terminal history
  -> runway evidence + recent movement clusters
  -> base/final/holding/go-around classification
  -> airport candidate decision (shadow)
  -> Prediction Lab / AGY evidence
```

Runtime AI is outside this path.

## ADS-B providers and local receiver support

Plane Alerts can combine public ADS-B providers with an optional local readsb/dump1090-compatible receiver. Local data is preferred when it is healthy and fresh, while bounded public-provider participation preserves fallback and wider coverage.

Supported local receiver families include readsb, dump1090, dump1090-fa and ultrafeeder/tar1090 aircraft JSON.

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

Prediction Lab records forecasts before outcomes are known and later compares them with observed behavior. v4.7 adds structured terminal diagnostics including airport, terminal state, runway candidate/confidence/support, recent movement cluster, holding/go-around state, runway-aware historical support, live CPA and the shadow decision reason.

Missing ADS-B coverage is unresolved rather than counted as a hit or miss. Longer-range 30–60 minute expectations remain shadow-only until enough trustworthy outcomes exist.

## Photography intelligence

Plane Alerts also provides deterministic spotting guidance for camera settings, framing, sun position, atmospheric conditions, upper-air conditions, contrail probability and shooting-window timing. Photography, weather and contrail enrichment do not control alert qualification.

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

Pull-request CI runs compile/static validation, the complete pytest suite, preserved Error Museum/provider/interaction regressions, v4.7 airport replays and deterministic performance benchmarks.

```text
python -m compileall -q app vercel_runtime worker.py
pytest -q
pytest -q tests/test_error_museum_v42.py tests/test_v44_monitor_reliability_replay.py tests/test_eta_stability_hotfix.py
pytest -q tests/test_v45_provider_resilience.py tests/test_v45_local_provider_area_recovery.py
pytest -q tests/test_interaction_v46.py
pytest -q tests/test_airport_terminal_v47.py tests/test_runway_data_v47.py tests/test_route_guard_v47.py tests/test_error_museum_v47.py
python scripts/benchmark_v42.py
python scripts/benchmark_v45_provider_resilience.py
python scripts/benchmark_v46_prediction.py
python scripts/benchmark_v46_interaction.py
python scripts/benchmark_v47_terminal.py
pip check
```

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
