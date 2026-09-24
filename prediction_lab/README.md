# Prediction Lab evidence branch

Plane Alerts v5.5 stores high-volume prediction audit and shadow-evaluation evidence as files instead of continuously writing those records to MongoDB.

Repository layout:

- `raw/YYYY-MM-DD/` — immutable synchronized evidence events.
- `unchecked/YYYY-MM-DD/` — durable cases created by the Collector task.
- `reviewed/YYYY-MM-DD/` — Investigator/Fixer conclusions and evidence references.
- `error_museum/` — promoted deterministic replay/regression cases.
- `archive/mongo-import/` — verified v5.5 export of historical Prediction Lab Mongo collections.
- `state/` — idempotent task checkpoints.
- `schemas/` — repository evidence schemas.

The continuously changing evidence lives on the dedicated `prediction-lab-data` branch. Production deployment is gated only from successful tests on `main`; commits to the data branch must never deploy Railway.

Runtime evidence is first written atomically to `/data/prediction_lab` (or `PREDICTION_LAB_ROOT` for self-hosting/tests). A bounded GitHub Actions job copies a batch to the data branch. The Railway file is renamed with `.synced` only after the Git push succeeds; only acknowledged files can later be pruned.

No repository evidence may contain credentials, direct Telegram user identifiers, admin names, or exact private observer coordinates. Missing later ADS-B coverage remains inconclusive. `30–60` minute `/next60` history forecasts remain shadow-only.
