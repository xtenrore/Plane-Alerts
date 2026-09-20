# Plane Alerts

Plane Alerts is a Telegram-based aircraft-spotting alert system. It combines live ADS-B observations with deterministic trajectory, closest-point-of-approach (CPA), ETA, route-history and terminal-arrival logic so alerts are based on whether an aircraft is actually expected to pass the observer, not proximity alone.

AI is not part of the live qualification path. It does not decide trajectory, CPA, ETA, pass/no-pass, aircraft filtering, cancellation or notification timing.

**Current code version: Plane Alerts v4.4.0**

Telegram: **[@planebotnotifierbot](https://t.me/planebotnotifierbot)**

## v4.4 — Reliability, observation truth and release integrity

v4.4 is primarily a reliability release. The main goal is to protect the five-second monitoring path and reduce false or late alerts without hiding genuine close passes.

### Alert-critical path

The intended priority order is:

```text
HIGH PRIORITY
ADS-B -> trajectory -> CPA -> qualification -> Telegram alert

LOW PRIORITY
route-history persistence/learning -> provider learning -> photography -> weather -> Prediction Lab -> auditing
```

Historical enrichment is supporting evidence. Live alerts do not wait for optional cold route-history reads when a safe fallback exists.

### Route-history isolation

Cold or expired flight-number history is served from bounded memory when available and refreshed in the background. A true cold miss temporarily falls back to no historical veto rather than blocking ADS-B processing, trajectory calculation, CPA or notification.

The route-history path uses bounded single-flight refreshes, cache limits, TTLs, concurrency limits and I/O timeouts. Route-history writes are also handled through a fixed worker queue with deduplication and stale-work dropping. Historical learning can recover from a dropped optional sample on a later observation.

### Terminal-arrival and genuine-pass handling

The terminal-arrival ensemble remains designed around the recurring false-alert case where an aircraft arriving at IST briefly points toward the observer before making its normal arrival turn. Destination, airport-convergence, route-history and multiple deterministic motion hypotheses may hold or cancel a projected alert when the evidence supports it.

At the same time, fresh physical observations remain authoritative. A genuinely observed close pass is not permanently hidden by old route expectations. Direct-presence recovery requires independent fresh observations rather than one stale or repeated sample.

Cancellation state is latched after repeated evidence to prevent qualify/cancel/requalify oscillation. Predictive evidence alone cannot immediately resurrect a confirmed cancellation, but a fresh physical entry into the configured radius releases the latch.

### Notification correctness

A Plane Alerts encounter normally reuses one Telegram message. v4.4 therefore records lifecycle delivery events explicitly:

- `first_notification`
- `message_update`
- `cancellation_update`
- `passed_update`
- `retry`
- `failed_delivery`

`notified_at` now represents the first successful logical alert and is not refreshed by ordinary message edits. Alert counts and lead-time analysis therefore do not treat a camera-ready edit, cancellation edit or passed update as a brand-new notification.

Notification telemetry and photo snapshots are persisted after Telegram delivery through bounded background work. A failed cancellation delivery leaves the encounter active so the cancellation can be retried instead of silently closing the alert.

### Five-second cadence protection

The monitoring path includes:

- shared regional ADS-B polling instead of one provider request per user
- provider refresh deadlines and continuity snapshots
- batched active-user configuration reads
- batched approach-state reads
- compiled profile filtering from already loaded configuration
- provider learning moved off the live path
- route-history reads and writes moved off the live path
- bounded optional enrichment work
- scheduler tolerance that avoids turning normal sub-second jitter into a roughly ten-second evaluation gap

Cached ADS-B continuity preserves the real position age. Reusing a snapshot never pretends stale data is fresh.

### Release identity

`app/version.py` is the canonical Plane Alerts version source. Health, readiness, worker status and release reporting expose the same version and deployed commit.

Railway deployment is triggered only after the `main` test workflow succeeds. The deploy workflow checks out that exact tested SHA and embeds the same SHA into the uploaded build, so a CLI deployment can still identify the code that is actually running.

## Profiles and aircraft filtering

`/profiles` manages persistent alert profiles. Each profile can hold an independent location, radius, aircraft selection and advanced filtering rules. Profiles can be created, activated, edited, renamed, duplicated and deleted.

The aircraft selector is data-driven and supports category browsing and type search. `All Aircraft` is a logical mode, so newly introduced or unknown aircraft can still qualify when the user chooses to monitor everything.

Advanced rules follow deterministic inheritance:

```text
Profile defaults
  -> Category override
  -> Aircraft-specific override
```

Supported rule fields include enabled/disabled state, altitude limits, airline/operator allow and block lists, and optional radius overrides. Selecting an aircraft only expresses user interest; it never bypasses trajectory, CPA, route, arrival or confidence checks.

## Alert qualification

Plane Alerts can consider:

- aircraft and observer position
- heading and groundspeed
- altitude and vertical rate
- position age and observation gaps
- recent distance trend
- turn rate and curvature
- projected CPA and time to CPA
- flight-number route history
- destination/airport convergence when available
- multiple plausible terminal-arrival futures
- fresh observed physical presence

A straight-line vector is not treated as sufficient evidence around terminal arrivals when stronger contradictory evidence exists.

## Prediction Lab

Prediction Lab records forecasts and later compares them with observed outcomes to investigate ETA stability, CPA error, false or late cancellations, qualify/cancel oscillation, route-history mistakes, terminal-arrival behavior and Next60 quality.

Missing ADS-B coverage is not counted as a hit or a miss. Outcome scoring requires suitable observed evidence. Longer-range 30–60 minute expectations remain shadow-only until enough trustworthy outcomes exist.

`/next60` and `/forecast` display expected aircraft in 0–15, 15–30 and 30–60 minute windows. Short-range live geometry remains authoritative.

## Photography intelligence

Plane Alerts includes deterministic spotting guidance for camera settings, framing, sun position, atmospheric conditions, upper-air conditions, contrail probability and shooting-window timing. Photography and weather enrichment must not delay the alert-critical path, and deterministic fallbacks remain available when optional services fail.

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
pip check
```

The final v4.4 reliability branch passed **291 tests**. Coverage includes fresh-observation confirmation, repeated-observation protection, production guard composition in a fresh interpreter, cancellation latching and recovery, failed cancellation retry, stale ADS-B behavior, IST terminal-turn suppression, genuine close-pass recovery, notification update classification, cold route-history isolation, profiles, Prediction Lab outcome handling and existing Error Museum regressions.

The final terminal-arrival benchmark evaluated 2,250 observer/path combinations for 250 users at about **1,800 evaluations per second**, with shared bounded motion-path reuse. Changes are not deployed merely because a branch exists; production deployment is gated on a successful `main` test workflow.

## Deployment

Production runs on Railway with MongoDB persistence. The main service runs Telegram, shared ADS-B polling, deterministic prediction, route history, photography intelligence and Next60. A separate AGY service performs post-outcome investigation and does not control live trajectory or notification decisions.

The Railway AI agent is not used for deployment.

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
  aircraft/          ADS-B providers, aircraft registry and deterministic filters
  bot/               Telegram commands, profile flows and messages
  intelligence/      trajectory, route history and terminal-arrival logic
  photography/       deterministic camera guidance
  worker/            shared polling, lifecycle and cadence guards
scripts/             deployment, verification and audit helpers
tests/               unit, regression and replay tests
docs/error_museum/   preserved production failure cases
docs/releases/       release notes
.github/workflows/   CI and Railway workflows
```

## Design principle

**Deterministic code decides what is physically happening. AI may investigate or explain evidence, but it does not control the live alert decision.**

ADS-B data can be stale, delayed, duplicated, incomplete or temporarily unavailable. Plane Alerts records uncertainty explicitly instead of inventing certainty.
