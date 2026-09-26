# Prediction Lab evidence branch

Plane Alerts v5.5 stores high-volume prediction audit and shadow-evaluation evidence as files instead of continuously writing those records to MongoDB.

Repository layout:

- `raw/YYYY-MM-DD/` — immutable synchronized evidence events.
- `unchecked/YYYY-MM-DD/` — durable cases created by the Collector task.
- `reviewed/YYYY-MM-DD/` — Investigator/Fixer conclusions and evidence references. Reviewed means investigated; it does not automatically mean fixed.
- `solved/YYYY-MM-DD/` — reviewed cases whose exact fix was merged, deployed, and production-verified. Solved records retain the complete case/review history and add release-resolution metadata.
- `error_museum/` — promoted deterministic replay/regression cases.
- `archive/mongo-import/` — verified v5.5 export of historical Prediction Lab Mongo collections.
- `state/` — idempotent task checkpoints.
- `schemas/` — repository evidence schemas.

The continuously changing evidence lives on the dedicated `prediction-lab-data` branch. Production deployment is gated only from successful tests on `main`; commits to the data branch must never deploy Railway.

Prediction Lab repository evidence is durable history. A code merge, release, or Railway deployment must never bulk-clear `raw`, `unchecked`, `reviewed`, `solved`, or `error_museum`. A merge alone does not make a case solved. Only the exact candidate-linked reviewed case may move to `solved` after the merged commit has deployed successfully and its required production verification has passed. Failed or unverified deployments leave the case in `reviewed`; inconclusive, no-bug, expected-behavior, duplicate, and blocked conclusions also remain durable reviewed evidence.

Runtime evidence is first written atomically to `/data/prediction_lab` (or `PREDICTION_LAB_ROOT` for self-hosting/tests). A bounded GitHub Actions job copies a batch to the data branch. Runtime spool copies may be acknowledged/pruned only after their exact bytes have been verified on `prediction-lab-data`; that bounded runtime cleanup is separate from repository case retention.

No repository evidence may contain credentials, direct Telegram user identifiers, admin names, or exact private observer coordinates. Missing later ADS-B coverage remains inconclusive. `30–60` minute `/next60` history forecasts remain shadow-only.
