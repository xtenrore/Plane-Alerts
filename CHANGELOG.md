# Changelog

Plane Alerts uses separate production releases. Detailed notes live under `docs/releases/`.

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
- Trusted post-CI GitHub release publishing creates an immutable `v4.9.0` tag/release from the exact tested `main` commit and canonical release notes.
- Installation, migration and rollback documentation.
- Physical prediction version remains `4.7.3-terminal-delivery-landing-path`.

## v4.8.1 — Mongo storage startup compatibility

- Fixed Motor/PyMongo database truth-value evaluation discovered during v4.8.0 production verification.
- Preserved v4.8 storage isolation and the existing prediction model.

## v4.8.0 — Storage, Database & Offline Resilience

- Separated alert-critical processing from non-critical MongoDB persistence.
- Added bounded storage queues, degraded operation, latency diagnostics, restart deduplication, schema migration and safe export/import tooling.
