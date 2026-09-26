# Prediction Lab evidence pipeline

Plane Alerts v5.5.0 separates live application storage from high-volume prediction evidence. Normal users, profiles, preferences, approach state, provider learning and route-history application data remain in the configured database. Prediction Lab audit snapshots, shadow evaluations and sentinel-route evidence use a persistent file spool instead.

## Production spool and synchronization

The runtime writes small sanitized JSON evidence files atomically under `/data/prediction_lab/raw/YYYY-MM-DD/`. Optional evidence work is bounded and does not run inline with the five-second aircraft-monitoring loop. The historical Mongo collections `prediction_lab_audit`, `prediction_shadow_evaluations` and `prediction_sentinel_routes` are exported to verified NDJSON chunks under `archive/mongo-import/`; source counts and SHA-256 hashes are checked before those legacy collections are retired.

`.github/workflows/prediction-lab-sync.yml` runs independently of deployment. It copies bounded batches to `prediction-lab-data`, validates evidence and rejects credential-like content, and acknowledges runtime spool files only after the exact repository bytes have been committed and pushed successfully. A conflicting push fails closed and is retried later. Acknowledged runtime spool files can follow bounded cleanup rules without deleting their durable repository history.

## Durable case lifecycle

The durable case lifecycle is:

`raw -> unchecked -> reviewed -> solved`

These stages have different meanings and must not be collapsed:

- `raw` is immutable synchronized evidence.
- `unchecked` is a normalized case awaiting investigation.
- `reviewed` is an investigated case with an evidence-backed disposition. Reviewed does not mean fixed.
- `solved` is reserved for a candidate-linked reviewed case whose exact fix commit was merged, successfully deployed, and production-verified. The solved record retains the original case ID, constituent event IDs, investigation/review evidence, and adds the exact fix SHA, deployed version, verification result/time, and relevant Error Museum/regression references.

A code merge alone never solves or clears a case. If deployment or production verification fails, the case remains `reviewed`. Inconclusive, no-bug, expected-behavior, duplicate, or blocked cases also remain durable reviewed evidence rather than being deleted. A new regression after a solved case must receive a distinct case ID; immutable raw evidence must not silently recreate a historical reviewed/solved case in `unchecked`.

No release path may bulk-delete or reset `raw`, `unchecked`, `reviewed`, `solved`, or `error_museum` merely because code was merged, released, or deployed.

## External scheduled tasks

The data branch is deliberately arranged for three idempotent external tasks:

1. **Collector** — inspect new `raw` events, create normalized case files in `unchecked`, deduplicate against later lifecycle stages, and then advance `state/collector.json` in the same durable commit.
2. **Investigator/Fixer** — process each unchecked case once, independently verify telemetry/source evidence, move the case to `reviewed`, add an Error Museum replay when a defect is proven, and create a normal engineering fix branch only when the evidence supports a safe change. Candidate metadata must name the exact case IDs and reviewed evidence paths it intends to resolve. Advance `state/investigator.json` only after the reviewed evidence is committed.
3. **Release/Deployment** — independently verify candidate fix branches, run Railway release gates, merge only proven-safe changes, deploy the exact tested commit, and production-verify it. Only after successful deployment and required production verification may the exact candidate-linked reviewed cases move to `solved`. Advance `state/release.json` only after the release result and corresponding solved-stage data commit succeed.

Case files retain `case_id` and constituent `event_id` values through every lifecycle transition. Re-running a task on an already committed transition is an idempotent no-op when the metadata matches. State checkpoints must never be advanced before their corresponding data/code commits succeed.

## Evidence semantics

Missing or stale ADS-B coverage is explicitly inconclusive. It is never a success or a miss. The eight fixed high-risk sentinels are Istanbul, London, Frankfurt, Paris, Amsterdam, Madrid, Rome and Vienna. Current delegated-admin configured locations are also evaluated in memory, but GitHub evidence stores only salted region identifiers, not admin identity or private observer coordinates. The 30–60 minute portion of `/next60` remains shadow-only; live deterministic CPA always has priority.
