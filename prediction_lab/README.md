# Prediction Lab evidence branch

Plane Alerts stores high-volume prediction audit and shadow-evaluation evidence as files instead of continuously writing those records to MongoDB.

Repository layout:

- `raw/YYYY-MM-DD/` — immutable synchronized evidence events.
- `unchecked/YYYY-MM-DD/` — durable cases created by the Collector task.
- `reviewed/YYYY-MM-DD/` — Investigator/Fixer conclusions and evidence references. Reviewed means investigated; it does not automatically mean solved.
- `solved/YYYY-MM-DD/` — durable cases whose exact candidate fix was merged, successfully deployed, and production-verified. Solved records retain the original case/evidence plus resolution metadata.
- `error_museum/` — promoted deterministic replay/regression cases.
- `archive/mongo-import/` — verified export of historical Prediction Lab Mongo collections.
- `state/` — idempotent task checkpoints.
- `schemas/` — repository evidence schemas.

## Case lifecycle and retention

The durable case lifecycle is:

`unchecked -> reviewed -> solved`

A code merge, pull-request merge, release commit, or version bump must never clear, delete, reset, or bulk-prune Prediction Lab repository evidence. A merge by itself does not make a case solved.

The Investigator/Fixer transitions completed investigations from `unchecked` to `reviewed` while preserving the case ID, constituent event IDs, source evidence, conclusion, and regression/Error Museum references. The Release task may transition only the exact reviewed cases linked to a release candidate into `solved` after the exact merged commit has deployed successfully and mandatory production verification has passed.

If deployment or production verification fails, the case stays reviewed/unresolved. Inconclusive, expected-behavior, duplicate, blocked, or no-bug cases remain durable historical evidence with an explicit disposition rather than being silently deleted.

The continuously changing evidence lives on the dedicated `prediction-lab-data` branch. Production deployment is gated only from successful tests on `main`; commits to the data branch must never deploy Railway.

Runtime evidence is first written atomically to `/data/prediction_lab` (or `PREDICTION_LAB_ROOT` for self-hosting/tests). A bounded GitHub Actions job copies a batch to the data branch. A Railway-volume raw file may be acknowledged/pruned only after safe synchronization according to the runtime retention policy. Runtime spool cleanup must never delete the durable repository case history in `unchecked`, `reviewed`, `solved`, or `error_museum`.

No repository evidence may contain credentials, direct Telegram user identifiers, admin names, or exact private observer coordinates. Missing later ADS-B coverage remains inconclusive. The `30–60` minute `/next60` history forecasts remain shadow-only.
