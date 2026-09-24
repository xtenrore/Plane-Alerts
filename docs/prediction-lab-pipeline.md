# Prediction Lab evidence pipeline

Plane Alerts v5.5.0 separates live application storage from high-volume prediction evidence. Normal users, profiles, preferences, approach state, provider learning and route-history application data remain in the configured database. Prediction Lab audit snapshots, shadow evaluations and sentinel-route evidence use a persistent file spool instead.

## Production spool and synchronization

The runtime writes small sanitized JSON evidence files atomically under `/data/prediction_lab/raw/YYYY-MM-DD/`. Optional evidence work is bounded and does not run inline with the five-second aircraft-monitoring loop. The historical Mongo collections `prediction_lab_audit`, `prediction_shadow_evaluations` and `prediction_sentinel_routes` are exported to verified NDJSON chunks under `archive/mongo-import/`; source counts and SHA-256 hashes are checked before those legacy collections are retired.

`.github/workflows/prediction-lab-sync.yml` runs independently of deployment. It copies at most 80 files / 25 MiB per run to `prediction-lab-data`, validates JSON and rejects credential-like content, then marks volume files `.synced` only after a successful Git push. A conflicting push fails closed and is retried later. Synced volume files become eligible for capped cleanup after 24 hours.

## External scheduled tasks

The data branch is deliberately arranged for three idempotent external tasks:

1. **Collector** — inspect new `raw` events, create normalized case files in `unchecked`, and then advance `state/collector.json` in the same durable commit.
2. **Investigator/Fixer** — process each unchecked case once, independently verify telemetry/source evidence, move the case to `reviewed`, add an Error Museum replay when a defect is proven, and create a normal engineering fix branch only when the evidence supports a safe change. Advance `state/investigator.json` only after the reviewed evidence is committed.
3. **Daily Release/Deployment** — independently verify candidate fix branches, run all release gates, merge only proven-safe changes, use the next v5.5.x patch, release/deploy the exact tested commit, production-verify it, then advance `state/release.json`.

Case files retain `case_id` and constituent `event_id` values. Re-running a task on an already committed event is a no-op. State checkpoints must never be advanced before their corresponding data/code commits succeed.

## Evidence semantics

Missing or stale ADS-B coverage is explicitly inconclusive. It is never a success or a miss. The eight fixed high-risk sentinels are Istanbul, London, Frankfurt, Paris, Amsterdam, Madrid, Rome and Vienna. Current delegated-admin configured locations are also evaluated in memory, but GitHub evidence stores only salted region identifiers, not admin identity or private observer coordinates. The 30–60 minute portion of `/next60` remains shadow-only; live deterministic CPA always has priority.
