# Changelog

Plane Alerts uses separate production releases. Detailed notes live under `docs/releases/`.

## v5.5.4 — Provider-First Destination Path Guard

- Replaced the stacked route-history, expected-turn and runway/terminal live alert-veto chain with one cached provider-first destination/path qualification layer.
- Uses existing ADSB.lol destination data plus ADSBDB as an independent additional/fallback source, with bounded background lookups that never block the five-second monitor path.
- Added deterministic airport-before-observer, landing-area-before-observer and destination-path conflict checks while preserving fresh physical entry as authoritative.
- Provider outages and genuinely ambiguous conflicts fail open to live trajectory; a disagreement can be resolved only when live terminal geometry clearly supports one nearby destination over a materially farther alternative. Sustained divergence and climbing go-around/diversion evidence release destination suppression.
- Route history and airport/runway inference remain available for Prediction Lab, analytics and diagnostics but are no longer live alert authorities.
- Added exact IST/LTFM, LTBA, genuine-pass, bad-destination, outage, slow-provider, physical-entry and go-around regressions plus a non-blocking destination-path benchmark.
- Separated Railway production CI/deployment from community release publication and the long weekly community platform matrix, so an ordinary Railway patch cannot automatically publish a community-stable tag or run the full ARM64/Raspberry Pi installer matrix.
- Underlying trajectory/3D prediction version remains `5.3-3d-proximity-age-aware`.

## v5.5.3 — Startup Migration Isolation

- Fixed the v5.5.2 Railway readiness failure where historical Prediction Lab migration plus Mongo index/schema maintenance blocked FastAPI startup after MongoDB had already connected.
- Railway now runs that low-priority storage maintenance in a retrying background task so the live monitoring runtime can start without waiting for historical archive work.
- Preserved the existing verified export/count/SHA-256-before-drop migration semantics and normal application MongoDB data.
- Added startup-isolation and maintenance-retry regression coverage; physical prediction behavior remains unchanged at `5.3-3d-proximity-age-aware`.

## v5.5.2 — Railway Volume CLI Compatibility Patch

- Corrected Railway CLI target-selector ordering in the production deployment workflow so the persistent `/data/prediction_lab` volume can be discovered or created before deployment.
- Corrected the same selector ordering for Prediction Lab volume file list, download and acknowledgement/rename operations in the bounded Git sync workflow.
- Added regression coverage for the current Railway `volume` command grammar exposed by the failed v5.5.1 production rollout.
- No Prediction Lab evidence semantics or live physical prediction behavior changed; prediction version remains `5.3-3d-proximity-age-aware`.

## v5.5.1 — File-Backed Prediction Lab & Automated Evidence Pipeline

- Moved high-volume Prediction Lab audit, shadow-evaluation and sentinel evidence from MongoDB to an atomic persistent file spool.
- Added verified export/retirement of `prediction_lab_audit`, `prediction_shadow_evaluations` and `prediction_sentinel_routes` while preserving normal application MongoDB data.
- Added a dedicated `prediction-lab-data` branch and bounded Railway-volume-to-Git synchronization with acknowledgement only after a successful push.
- Added durable case/event IDs plus Collector, Investigator and Release checkpoints for idempotent external scheduled tasks.
- Preserved the eight established high-risk European sentinels and added privacy-safe dynamic sampling around current delegated-admin configured locations.
- Moved `/next60` historical shadow candidates to normal `flight_route_samples`; live deterministic approach state remains authoritative and 30–60 minute forecasts remain shadow-only.
- Added quota-full Atlas recovery narrowly for the verified Prediction Lab migration, plus restart/conflict/privacy/cadence/migration regressions and a persistence benchmark.
- Added persistent `prediction_lab_data` self-host storage and matching backup/rollback documentation.
- Physical prediction version remains `5.3-3d-proximity-age-aware`; trajectory, CPA, ETA, terminal, qualification, cancellation and alert timing are unchanged.

## v5.5.0 — Guided Community Self-Hosting

- Published the guided community self-hosting installer and stable cross-platform installer assets.
- Kept that public tag immutable; it was deliberately separate from the owner's Railway production deployment path.
- Physical prediction version remained `5.3-3d-proximity-age-aware`.

## v5.4.3 — AGY Removal

- Permanently removed the retired AGY/Antigravity Railway service and persistent state volume.
- Removed the `/agy` Telegram command, command-menu entry and handler registration.
- Removed AGY runtime modules, Docker/dependency files, helper scripts, environment settings and AGY-only test suites.
- Preserved useful provider-age and stale-cancellation regressions under neutral prediction test names.
- Physical prediction version remained `5.3-3d-proximity-age-aware`.

## v5.4.2 — General Reliability Audit Fixes

- Fixed first-time setup, last-known-good monitoring configuration, optional admin configuration, Compose readiness, Telegram interaction wiring and several photography/enrichment correctness issues.
- Preserved absolute snapshot age and deterministic active-profile/configuration behavior.
- Added dedicated audit regressions without changing the physical prediction model.

## v5.4.1 — Reliability Hotfix

- Hardened the then-active external-agent quota/recovery and Mongo-context paths.
- Those retired integration paths were subsequently removed completely in v5.4.3.
- Physical prediction remained `5.3-3d-proximity-age-aware`.

## v5.4.0 — User Experience & Presets

- Added six editable profile presets and guided preset/custom profile creation.
- Improved `/profiles`, `/preferences` and setup navigation/recovery behavior.
- Updated Next 60 branding and added UX regressions/performance coverage.
- Physical prediction remained `5.3-3d-proximity-age-aware`.

## v5.3.0 — Multi-Location & Scale

- Added bounded observer-independent aircraft-motion reuse and exact spatial candidate filtering.
- Preserved observer-specific CPA, 3D relevance, confidence, terminal and lifecycle decisions.
- Corrected provider-age handling in the midpoint ETA/entry path and stale cancellation recovery semantics.
- Added multi-user isolation, production-chain and scale regressions.
- Physical prediction version became `5.3-3d-proximity-age-aware`.

## v5.2.1 — Release Documentation Truth Patch

- Corrected ETA-evaluation documentation so scoreable ETA truth requires a matched physical closest-observation timestamp.
- No live prediction behavior changed.

## v5.2.0 — Shadow Models & Automatic Evaluation

- Added deterministic production-control versus shadow-candidate evaluation without allowing shadow models to select live behavior.
- Added release/prediction identity to Prediction Lab evidence and explicit unresolved semantics for cancellations/missing coverage.
- Added release-to-release shadow metrics and conservative promotion evidence.
- Physical prediction remained `5.1-3d-proximity`.

## v5.1.3 — Complete Wrapper-Chain Compatibility Hotfix

- Completed compatibility across the installed v4.3/v4.4/v4.6/v5.1 deterministic predictor wrapper chain.
- Preserved 3D relevance and observed-pass semantics.

## v5.1.2 — Predictor Wrapper Compatibility Hotfix

- Restored v5.1 altitude-relevance arguments through the production v4.6 wrapper chain.
- No thresholds or alert-timing policy changed.

## v5.1.1 — Railway Deploy-Gate Compatibility Patch

- Moved exact-current-main verification into a GitHub-hosted gate before Railway deployment.
- Preserved same-repository successful-main-push trust checks and stale-SHA refusal.

## v5.1.0 — 3D Proximity & Advanced Geometry

- Added deterministic vertical separation and true 3D CPA with conservative horizontal fallback.
- Added observer terrain-elevation enrichment outside the critical path.
- Tightened observed-pass lifecycle semantics and added 3D regression/benchmark coverage.

## v5.0.0 — Observability, Explainability & Operator Insight

- Added operator diagnostics/metrics and bounded provider/storage/timing observability.
- Kept physical alert correctness independent of optional diagnostics.

## v4.9.0 — Project Maturity & Self-Hosting

- Added pinned dependencies, Docker Compose self-hosting, amd64/ARM64 validation, doctor diagnostics, public issue reporting and trusted exact-tested-commit release publishing.

## v4.8.1 — Mongo Storage Startup Compatibility

- Fixed Motor/PyMongo database truth-value evaluation while preserving v4.8 storage isolation.

## v4.8.0 — Storage, Database & Offline Resilience

- Separated alert-critical processing from non-critical MongoDB persistence.
- Added bounded storage queues, degraded operation, storage diagnostics, restart deduplication, migration and safe export/import tooling.

Earlier release details remain available under `docs/releases/` and the repository history.
