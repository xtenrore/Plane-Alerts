# Plane Alerts

Plane Alerts is a Telegram-based aircraft spotting alert system. It combines live ADS-B observations with deterministic trajectory, closest-point-of-approach (CPA), ETA, confidence, route-history and terminal-area logic so alerts are based on whether an aircraft is actually expected to pass the observer, not proximity alone.

AI is not part of the live qualification path. It does not decide trajectory, CPA, ETA, confidence, pass/no-pass, runway use, terminal state, cancellation or notification timing.

**Current code version: Plane Alerts v5.3.0**  
**Current prediction version: `5.1-3d-proximity`**

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

## v5.3 multi-location and scale

v5.3 reduces duplicated computation when many observers share the same regional ADS-B snapshot. Observer-independent aircraft motion is projected once and reused through bounded process-local caches, including the authoritative v4.3 midpoint-integrated motion path. The established production wrapper order remains `shared base -> v4.3 midpoint -> v4.4 direct presence -> v4.6 confidence -> critical timing`.

A bounded spatial index filters geographically impossible user/aircraft pairs before per-user prediction. The grid is only an acceleration structure: exact spherical great-circle membership against the existing `radius + 120 km` monitoring envelope remains authoritative. The hot path evaluates that same spherical boundary with a precomputed unit-vector dot-product threshold instead of repeated inverse-trigonometric Haversine calls. Every surviving candidate still receives its own horizontal CPA, 3D relevance, ETA, confidence, route/terminal, qualification, cancellation and lifecycle evaluation.

Motion caches are limited to 1,024 entries and contain no user coordinates or lifecycle state. Per-user candidate work is also explicitly bounded and surfaced in scale diagnostics. Existing shared provider snapshots and enrichment caches are reused, so v5.3 does not add another ADS-B polling layer or multiply provider calls. The physical prediction version remains `5.1-3d-proximity`.

## v5.2 shadow models and automatic evaluation

v5.2 makes prediction development data-driven without changing the live physical predictor. The authoritative prediction version remains `5.1-3d-proximity`; existing v4.6 linear and turn-aware candidate models continue to run as shadow-only evidence and cannot qualify, cancel, time or send alerts.

Prediction Lab snapshots now record application release version and shadow feature-flag state. When a later outcome has explicit scoreable ground truth, Plane Alerts can evaluate the production control and shadow candidates side by side for CPA error, ETA error, false-positive/false-negative behavior, alert lead time and confidence calibration. Results are grouped by release and model ID so changes can be compared over time.

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

v5.1.2 fixes the production wrapper-chain compatibility bug discovered during v5.1.1 verification. The installed v4.6 confidence layer now accepts and forwards the v5.1 `altitude_relevance` option and preserves unknown observer elevation, so the intended `5.1-3d-proximity` model can execute through the monitor/critical-timing layer.

v5.1.3 completes that repair after v5.1.2 production verification exposed the older v4.4 direct-presence and v4.3 midpoint wrappers beneath v4.6. Both now accept and forward the current v5.1 signature. The release gate recreates the full installed chain — core v5.1 trajectory -> v4.3 midpoint -> v4.4 direct presence -> v4.6 confidence -> critical timing — including unknown observer elevation and both enabled/disabled altitude relevance. The physical prediction version and all CPA/ETA, terminal, qualification and alert-timing thresholds remain unchanged.

## v5.0 observability and explainability

v5.0 adds bounded read-only operator diagnostics and structured runtime metrics without changing the physical predictor. `planealerts diagnostics` explains recent per-aircraft Prediction Lab decisions, while `planealerts metrics` summarizes provider, monitor, storage, queue and notification health. Provider secrets and exact observer coordinates are not persisted in these views.

AGY Mongo reads use bounded timeouts, a cooldown circuit and last-known-good redacted context. A degraded Atlas connection must not make the AGY loop wait on repeated long socket timeouts, and `CHATGPT_HANDOFF_JSON` remains independent of Mongo availability.

## Self-hosting

Plane Alerts supports Docker Compose with MongoDB persistence. See `.env.example`, `docker-compose.yml`, `Dockerfile`, `LICENSE` and the release notes under `docs/releases/` for deployment and migration details.

Before deployment, operators can run:

```bash
planealerts doctor --offline
```

For live configuration/storage checks:

```bash
planealerts doctor
```
