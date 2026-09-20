# Plane Alerts

Plane Alerts is a Telegram-based aircraft spotting alert system. It combines live ADS-B observations with deterministic trajectory, closest-point-of-approach (CPA), ETA, confidence, route-history and terminal-area logic so alerts are based on whether an aircraft is actually expected to pass the observer, not proximity alone.

AI is not part of the live qualification path. It does not decide trajectory, CPA, ETA, confidence, pass/no-pass, runway use, terminal state, cancellation or notification timing.

**Current code version: Plane Alerts v4.7.3**

Telegram: **[@planebotnotifierbot](https://t.me/planebotnotifierbot)**

## v4.7 — Airport, Runway & Terminal Intelligence

v4.7 extends the existing v4.2/v4.6 prediction stack instead of introducing a competing predictor.

### Terminal intelligence

Plane Alerts records deterministic terminal-area evidence for:

- sustained vector changes
- downwind/base transitions
- final intercept and established final
- probable holding behavior
- go-arounds and missed approaches

A single noisy ADS-B heading is not enough to establish a turn. Terminal evidence uses bounded observation history and the existing v4.6 sustained-turn checks.

Airport context produces an explicit uncertainty penalty for diagnostics. Fresh live geometry remains authoritative: being near an airport or having that airport as the route destination is never an automatic suppression rule.

v4.7.2 adds one narrow authoritative **initial qualification hold** for the recurring terminal-arrival false-alert class. Before the first Telegram notification only, a candidate may be held when fresh observations jointly show a strong low/slow/descending airport arrival and the temporary straight-line observer CPA conflicts with that terminal evidence. Destination metadata such as `ISL` or `IST` is supporting evidence only and cannot suppress an alert by itself. The initial hold requires fresh sufficient evidence and releases for go-around or missed approach, a trajectory change that disproves the expected turn, a landing path that can physically enter the observer radius, or fresh physical entry into the configured radius. The landing path ends at the far runway end; the former 18 km airborne extension is shadow context only. v4.7.3 evaluates this guard until the first successful Telegram delivery. Candidate qualification, missing evidence and failed sends cannot permanently disable it; existing delivered messages use the established cancellation lifecycle.

### Worldwide airport and runway data

v4.7.1 added a local worldwide aviation-reference database built from a commit-pinned OurAirports snapshot. The complete airport catalogue is compiled during the production image build into `data/aviation/compiled/global_airports.sqlite3`; runtime monitoring performs local read-only SQLite lookups and does not call OurAirports or MongoDB for static airport/runway reference data.

The global snapshot includes large, medium and small airports, heliports, seaplane bases, local-code-only facilities and closed facilities represented by the source. Closed facilities remain in the reference database but are excluded from normal nearby-airport inference. Runway rows are preserved even when the source lacks endpoint geometry; Plane Alerts only uses runway-relative prediction when the required coordinates/headings are present.

Airport lookups use a bounded spatial-cell index and a small runtime cache instead of loading the worldwide catalogue into memory or scanning every airport in the five-second loop.

Maintained official overrides take precedence over the global source. LTFM/IST uses the verified operational runway set maintained by Plane Alerts, so a generic upstream record cannot silently reintroduce stale or planned runway geometry. v4.7.2 also verifies the compiled LTBA/ISL identity and active runway geometry as a release gate because Atatürk arrivals are a primary Error Museum case.

### Runway geometry and inference

Runway geometry lives in a dedicated data layer rather than being scattered through prediction code. The classifier accepts generic single, parallel and crossing-runway layouts worldwide.

Likely runway direction is inferred from recent observed traffic only when multiple aircraft support the same direction. The inference retains supporting-aircraft count, recency and confidence; one aircraft cannot create an active-runway conclusion. Configuration changes require fresh multi-aircraft evidence.

Recent airport movement clusters are bounded, deduplicated per aircraft and time-decayed. Historical route paths can also be associated with runway families as supporting evidence. Neither source overrides fresh motion.

### Shadow validation and live safety

Broad runway/base/final/holding suppression hypotheses remain **shadow-only**. They are recorded in Prediction Lab and do not directly control live qualification or cancellation.

The v4.7.2 initial terminal-arrival hold is deliberately narrower: it can delay only the first notification when several fresh physical arrival signals agree. It is not a destination-airport veto and it ends only after actual per-user message delivery. Strong go-around/missed-approach evidence can release an older landing-turn hold. Airport-distance growth, a single climb or a hypothetical runway extension cannot by themselves override that hold; the existing bounded expected-turn lifecycle remains in control. Fresh physical radius entry always wins.

This protects both sides of the historical Istanbul problem: normal arrivals can avoid premature straight-line false alerts while genuine overhead/transit, changed-trajectory or go-around passes remain alertable.

### Weather and contrails

Weather remains optional context and is not a hard runway selector. Provider failure must not block ADS-B ingestion, CPA or alerting.

Google Contrails remains separate photography/environment enrichment. It does not influence trajectory, airport classification, runway inference, CPA, ETA, qualification, cancellation or alert timing.

### Known limitations

Only the narrow initial terminal-arrival hold, corrected in v4.7.3, is authoritative; broader runway/history hypotheses remain shadow evidence until additional replay and production outcomes demonstrate improvement without missed genuine passes. The worldwide reference catalogue is community-maintained and is not treated as official operational truth; maintained Plane Alerts overrides can replace records for airports where stronger sources are available. Some airports or runway rows do not have complete endpoint coordinates/headings, so runway-specific inference remains uncertain there. Published procedures and inferred runway configuration never override fresh physical observations.

## Alert-critical architecture

```text
ADS-B ingestion
  -> canonical fresh observation
  -> deterministic motion history
  -> production trajectory / CPA / ETA
  -> v4.6 confidence + position uncertainty
  -> existing terminal / route guard
  -> v4.7.3 terminal-arrival guard until successful notification delivery
  -> qualification / cancellation lifecycle
  -> Telegram alert

Pinned OurAirports snapshot (build time only)
  -> local SQLite + spatial-cell index
  -> on-demand airport/runway geometry
  -> bounded v4.7 terminal history
  -> runway evidence + recent movement clusters
  -> base/final/holding/go-around classification
  -> broad airport candidate decision (shadow)
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

Prediction Lab records forecasts before outcomes are known and later compares them with observed behavior. v4.7 adds structured terminal diagnostics including airport, terminal state, runway candidate/confidence/support, recent movement cluster, holding/go-around state, runway-aware historical support, live CPA and the shadow decision reason. v4.7.3 records initial-hold candidates, effective holds, actual notification visibility, bounded landing-path CPA, destination match, evidence age, airport-distance trend and heading error. Throttled `terminal_qualification` logs make bypasses inspectable without logging observer coordinates.

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

Pull-request CI runs compile/static validation, builds and verifies the pinned worldwide airport database, runs the complete pytest suite, preserves Error Museum/provider/interaction regressions, and enforces deterministic performance benchmarks.

```text
python -m compileall -q app vercel_runtime worker.py
python scripts/build_airport_database.py
python scripts/verify_airport_database.py
pytest -q
pytest -q tests/test_error_museum_v42.py tests/test_v44_monitor_reliability_replay.py tests/test_eta_stability_hotfix.py
pytest -q tests/test_v45_provider_resilience.py tests/test_v45_local_provider_area_recovery.py
pytest -q tests/test_interaction_v46.py
pytest -q tests/test_airport_terminal_v47.py tests/test_runway_data_v47.py tests/test_route_guard_v47.py tests/test_error_museum_v47.py
pytest -q tests/test_global_airport_data_v471.py
pytest -q tests/test_terminal_arrival_hold_v472.py
python scripts/benchmark_v42.py
python scripts/benchmark_v45_provider_resilience.py
python scripts/benchmark_v46_prediction.py
python scripts/benchmark_v46_interaction.py
python scripts/benchmark_v47_terminal.py
python scripts/benchmark_v471_airport_lookup.py
python scripts/benchmark_v472_terminal_hold.py
pip check
```

## Deployment

Production runs on Railway with MongoDB persistence for live/user/history state. Static worldwide airport/runway reference data is kept outside MongoDB in the local image database. Deployment occurs only after the exact `main` commit passes CI. The deployed build reports its exact commit through runtime metadata; deployment SHAs are not hard-coded in source.

The main service runs Telegram, shared ADS-B polling, deterministic prediction, route history, photography intelligence and Next60. A separate AGY service performs post-outcome investigation and may suggest hypotheses, but it does not control trajectory or notification decisions. Railway AI is not used.

## Running locally

Plane Alerts uses Python 3.11 and MongoDB. Build the pinned airport reference database once before starting the application:

```bash
git clone https://github.com/xtenrore/Plane-Alerts.git
cd Plane-Alerts
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/build_airport_database.py
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
