# Changelog

Plane Alerts uses separate production releases. Detailed notes live under `docs/releases/`.

## v5.4.2 — General Reliability Audit Fixes

- Fixed first-time setup so monitoring is enabled only after a successful coherent profile save.
- Preserved per-user last-known-good monitoring configuration across partial profile materialization and added cold-start repair from the authoritative active profile.
- Made blank optional `ADMIN_TELEGRAM_ID` valid while keeping invalid nonnumeric values rejected.
- Made sensitive admin APIs fail closed when `ADMIN_PASSWORD` is blank.
- Changed Compose rollout health to `/ready` and aligned self-hosting readiness documentation.
- Explicitly installed the v4.6 Telegram interaction layer in fresh production runtime composition.
- Reconnected nonblocking observer-elevation enrichment to the cached live-user path and bound persistence to the exact saved coordinates.
- Bound preset Save callbacks to one persisted draft session so stale/duplicate buttons cannot create or activate the wrong profile.
- Preserved absolute age for historical photography snapshots and prevented stale snapshots from generating current shooting countdowns.
- Reused the canonical aircraft filter for untargeted `/photo` selection.
- Added effective inherited altitude-rule contradiction validation.
- Updated help, photography and admin product-version displays to the canonical release identity.
- Added dedicated v5.4.2 audit regressions; physical prediction remains `5.3-3d-proximity-age-aware`.

## v5.4.1 — AGY Reliability Hotfix

- Preserved durable AGY quota holds across restart/deploy, enable changes, force tokens, tooling migration and recovery paths.
- Made unknown quota-reset wording remain blocked and kept the required ten-minute guard after verified reset deadlines.
- Isolated AGY Mongo context failures per collection and exposed per-collection cache freshness.
- Corrected AGY profile context to the authoritative `profiles` collection.
- Made findings pagination lossless for sequential consumers.
- Added offline AGY regressions without launching inference or introducing paid-credit usage.
- Physical prediction remains `5.3-3d-proximity-age-aware`.

## v5.4.0 — User Experience & Presets

- Added six editable profile presets: Casual Observer, Aircraft Photographer, Airport-Adjacent, Rare Aircraft Hunter, Military Watcher, and Local SDR Mode.
- Added a guided profile creation choice between Quick Preset and the existing full Custom Setup flow.
- Added preset preview/confirmation for existing profiles while preserving saved coordinates and unrelated preferences.
- Improved `/profiles` navigation with explicit Back, Cancel, Close and Status actions, plus safe recovery from stale/interrupted menus.
- Changed `/preferences` to open the active profile editor instead of dropping directly into the aircraft picker.
- Added actionable validation routes for missing location, radius or aircraft selection.
- Kept Telegram labels/callback payloads compact and within Telegram limits.
- Updated the existing Next 60 Mini App branding to Plane Alerts without changing forecast semantics.
- Added dedicated v5.4 UX regressions, fresh-image runtime wiring validation and a deterministic preset/profile micro-benchmark.
- Physical prediction version remains `5.3-3d-proximity-age-aware`; no trajectory, CPA, ETA, terminal, qualification, cancellation or alert-timing behavior changed.

## v5.3.0 — Multi-Location & Scale

- Added bounded observer-independent aircraft-motion reuse while preserving observer-specific CPA, 3D relevance, confidence, terminal and lifecycle decisions.
- Shared the authoritative v4.3 midpoint-integrated motion path beneath the established v4.4/v4.6/critical-timing safety wrapper chain.
- Added exact spatial candidate filtering so each user evaluates only aircraft within the existing `radius + 120 km` monitoring envelope; the spatial grid is acceleration-only and exact spherical membership remains authoritative.
- Added bounded 1,024-entry LRUs for base motion and midpoint motion, plus an explicit per-user candidate-work cap with operator-visible scale counters.
- Reused the existing shared regional ADS-B provider snapshots and enrichment caches; v5.3 does not add duplicate provider requests or a second feed layer.
- Fixed the midpoint ETA/entry path to account for provider-reported ADS-B position age, resolving the reproducible late-ETA bias identified by AGY seq140.
- Prevented stale in-radius observations from clearing a cancellation latch; fresh direct physical presence can still recover a cancelled encounter.
- Added multi-user isolation/equivalence regressions, a fresh-interpreter production-wrapper composition regression, AGY regressions and a deterministic 500-user/600-aircraft scale benchmark.
- Physical prediction version is `5.3-3d-proximity-age-aware`; no terminal-arrival threshold, qualification threshold, alert-delivery timing policy or runtime-AI authority changed.

## v5.2.1 — Release documentation truth patch

- Corrected the v5.2 ETA-evaluation documentation to match the final production implementation.
- ETA truth now explicitly requires a matched physical closest-observation timestamp; lifecycle-resolution time is not substituted when that timestamp is unavailable.
- Bumped application release identity to `5.2.1`.
- Physical prediction version remains `5.1-3d-proximity`; no live prediction, qualification, cancellation, terminal, provider, storage, Telegram, or shadow-evaluation behavior changed.

## v5.2.0 — Shadow Models & Automatic Evaluation

- Added deterministic production-control versus shadow-candidate evaluation using the existing v4.6 linear and turn-aware candidates.
- Added explicit shadow-only feature flags without adding a model-selection path to live alerts.
- Prediction Lab snapshots now record application release version and shadow feature-flag state.
- Observed in-radius passes are scoreable outcomes; lifecycle cancellations and missing ADS-B coverage remain unresolved until separately validated.
- Added bounded live post-outcome evaluation with 14-day TTL storage and deterministic evaluation IDs.
- Added release-to-release CPA error, ETA error, false-positive/false-negative, cancellation, lead-time and confidence-calibration metric support while preserving missing denominators as unknown.
- Added `planealerts shadow-eval` read-only operator reporting and conservative promotion evidence; automatic promotion is always disabled.
- Added deterministic replay fixtures, v5.2 regressions and a sub-millisecond pure evaluation benchmark.
- Physical prediction version remains `5.1-3d-proximity`; no CPA/ETA, route/terminal, qualification/cancellation or alert-timing policy changed.

## v5.1.3 — Complete v5.1 wrapper-chain compatibility hotfix

- Fixed the additional production `TypeError` discovered after v5.1.2 deployment, where the installed v4.4 direct-presence wrapper rejected the v5.1 `altitude_relevance` option.
- Updated both the v4.4 direct-presence wrapper and v4.3 midpoint wrapper to accept and forward the current v5.1 predictor signature while preserving unknown observer elevation.
- The v4.3 midpoint wrapper now recomputes v5.1 3D relevance on its actual production path and preserves the v5.1 observed-pass requirement instead of restoring horizontal-only or projected-CPA legacy behavior.
- The v4.4 direct-presence guard no longer overrides a trustworthy 3D exclusion or an already-observed completed pass.
- Replaced the shortened compatibility regression with the actual installed chain: core v5.1 trajectory -> v4.3 midpoint -> v4.4 direct presence -> v4.6 confidence -> critical timing.
- Physical prediction version remains `5.1-3d-proximity`; no CPA/ETA thresholds, terminal guards, qualification rules or alert timing policy changed.

## v5.1.2 — Predictor-wrapper production compatibility hotfix

- Fixed the production `TypeError` that prevented v5.1 3D predictions from running through the installed v4.6 confidence and critical-timing wrapper chain.
- The v4.6 predictor wrapper now accepts and forwards `altitude_relevance` and preserves unknown observer elevation instead of coercing it to sea level.
- Added a regression that recreates the production wrapper chain and verifies both enabled and disabled altitude relevance reach the v5.1 predictor correctly.
- Physical prediction version remains `5.1-3d-proximity`; the hotfix restores intended v5.1 behavior rather than introducing a new model.

## v5.1.1 — Railway deploy-gate compatibility patch

- Fixed the trusted post-CI Railway deployment workflow after v5.1.0 exposed that the Railway CLI container does not include `git`.
- Moved current-main SHA verification into a separate standard GitHub-hosted gate job using the authenticated GitHub API/CLI.
- Preserved same-repository successful-main-push trust checks and stale-tested-commit refusal.
- Physical prediction version remains `5.1-3d-proximity`; no trajectory, CPA, ETA, qualification, cancellation or terminal logic changed.

## v5.1.0 — 3D Proximity & Advanced Geometry

- Added deterministic vertical separation and true three-dimensional CPA while retaining horizontal CPA as a separate, always-visible value.
- Added altitude-aware relevance with configurable enable/disable behavior and conservative uncertainty bounds.
- Missing, stale, malformed or discontinuous altitude fails open to the established horizontal predictor rather than suppressing a possible pass.
- Added optional observer terrain elevation backfill through the existing free Open-Meteo integration; network lookup remains outside the five-second alert-critical path.
- Fixed lifecycle classification so an aircraft is finalized as passed only after an actual observed radius entry followed by fresh receding motion; a close projected CPA alone is not ground truth.
- Added v5.1 geometry/lifecycle regressions and a deterministic 3D performance benchmark.
- Physical prediction version is `5.1-3d-proximity`.

## v5.0.0 — Observability, Explainability & Operator Insight

- Added `planealerts diagnostics` with bounded per-aircraft why/why-not explanations from Prediction Lab evidence.
- Added `planealerts metrics` for persisted provider, timing, storage, queue and notification diagnostics.
- Persisted provider request/error/timeout counts, latency percentiles, last success, stale rate and circuit state through the existing monitor heartbeat.
- Added deterministic diagnostics-overhead benchmark and v5.0 regression gates.
- Fixed AGY Atlas read-timeout storms with bounded reads, a cooldown circuit and last-known-good redacted context fallback.
- Preserved independent `CHATGPT_HANDOFF_JSON` delivery when Mongo is degraded.
- Physical prediction version remains `4.7.3-terminal-delivery-landing-path`.

## v4.9.0 — Project Maturity & Self-Hosting

- Fully pinned Python runtime dependency lock.
- MIT license and public issue-reporting template.
- Docker Compose self-hosting with MongoDB persistence, health checks and restart policies.
- amd64/ARM64 Docker validation, including the Raspberry Pi self-hosting path.
- `planealerts doctor` configuration/dependency diagnostics with secret-safe output.
- Fresh-install, Compose, doctor and architecture gates added to CI.
- Trusted post-CI GitHub release publishing creates a `v4.9.0` tag/release from the exact tested `main` commit and canonical release notes.
- Installation, migration and rollback documentation.
- Physical prediction version remains `4.7.3-terminal-delivery-landing-path`.

## v4.8.1 — Mongo storage startup compatibility

- Fixed Motor/PyMongo database truth-value evaluation discovered during v4.8.0 production verification.
- Preserved v4.8 storage isolation and the existing prediction model.

## v4.8.0 — Storage, Database & Offline Resilience

- Separated alert-critical processing from non-critical MongoDB persistence.
- Added bounded storage queues, degraded operation, latency diagnostics, restart deduplication, schema migration and safe export/import tooling.
