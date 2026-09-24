# Plane Alerts Prediction Lab — v4 background and current v5.5 storage

Prediction Lab was introduced in Plane Alerts v4.0 as a shadow-learning and evidence system. The core safety boundary is unchanged: Prediction Lab can observe, replay and evaluate production behavior, but it does not control live trajectory, CPA, ETA, qualification, cancellation or alert timing.

For the current file-backed storage/scheduled-task architecture, see [`prediction-lab-pipeline.md`](prediction-lab-pipeline.md).

## User Next 60 Minutes

`/next60` and `/forecast` combine two evidence sources:

- live production approach state when deterministic CPA/ETA already exists;
- bounded normal `flight_route_samples` timing history for longer-range shadow expectations.

Live geometry overrides a historical entry for the same flight. The 30–60 minute history remains shadow-only and is shown as a timing estimate rather than false exact precision. Missing later ADS-B coverage is inconclusive.

## High-risk sentinel network

The production service rotates free/public ADS-B queries across eight fixed European observation regions: Istanbul, London, Frankfurt, Paris, Amsterdam, Madrid, Rome and Vienna. Sentinel acquisition is independent of user alerts and does not create notifications.

v5.5 also evaluates configured locations belonging to users who currently have delegated admin access. Those coordinates are used only in memory for acquisition. Repository evidence stores a salted private-region identifier instead of admin identity or private observer coordinates.

Sentinel and adversarial-turn evidence is now written to the v5.5 persistent Prediction Lab file spool rather than new `prediction_sentinel_routes` / `prediction_lab_audit` Mongo records.

## Adversarial observer cases

Prediction Lab can inspect observed routes for meaningful turns and construct replay cases representing locations where a naive straight-line predictor could initially appear observer-bound before the route turns away. Proven failures should be promoted into deterministic Error Museum regression cases before a production fix is accepted.

## Evidence resolution

Later observations may resolve prior expectations with actual timing and closest distance. If coverage is absent or too stale to establish what happened, the outcome remains inconclusive and is excluded from hit/miss accuracy scoring.

## v5.5 storage migration

Historical records from these legacy high-volume collections are exported into `prediction_lab/archive/mongo-import/` with source-count and SHA-256 verification before retirement:

- `prediction_lab_audit`
- `prediction_shadow_evaluations`
- `prediction_sentinel_routes`

Normal Plane Alerts application data remains in MongoDB. The dedicated `prediction-lab-data` branch receives bounded synchronized evidence; commits to that branch cannot deploy production.

## Safety boundaries

- Prediction Lab output cannot create or cancel production alerts.
- Runtime AI does not decide trajectory, CPA, ETA, pass/no-pass, qualification, cancellation or alert timing.
- Sentinel collection uses only supported free/public ADS-B providers.
- Repository evidence excludes credentials, direct user identifiers and unnecessary private observer coordinates.
- Missing ADS-B coverage is inconclusive.
- Any evidence-backed fix requires independent verification and replay/regression coverage before release.
