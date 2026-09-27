# Plane Alerts Private Operations — Phase 0 Architecture Decision

Status: **Accepted for Phase 0 design lock**  
Baseline date: **2026-09-27**  
Scope: **inventory and design only**. This record does not enable provider routing, create a scheduler, change alert behavior, deploy code, mutate Railway configuration, or modify production data.

## 1. Baseline source and release state

- Canonical repository: `xtenrore/Plane-Alerts` (public).
- Baseline `main`: `639464f5a11f5b6690e41a6b80e1c3d9e9d7ee6b`.
- Canonical application version at that commit: `5.8.7`.
- Railway runtime logs also identify the running application as Plane Alerts `5.8.7`.
- Latest community/self-host stable GitHub release observed during inventory: `v5.5.3`.
- The production/community version difference is expected under the current Railway-vs-Community release policy and must not be represented as community validation of `5.8.7`.
- Existing open engineering PRs were preserved. Phase 0 did not modify, close, rebase, merge, or replace them.
- The exact production commit SHA was not independently exposed by the Railway connector during this inventory. Runtime version was verified, but commit equality must remain an explicit verification field rather than being inferred from deployment timing.

## 2. Railway topology and resource decision

The current Railway project has two relevant services:

1. The live Plane Alerts service, which owns Telegram and the five-second aircraft-monitoring path.
2. A separate FreeLLMAPI service, already isolated from the live monitor.

The live service currently has a persistent Prediction Lab volume mounted at `/data/prediction_lab` with a 500 MB capacity. During the Phase 0 observation window, the live service used roughly 0.80 GB memory on average and the Prediction Lab volume had recently reached roughly 0.45 GB usage before later dropping. The aircraft monitoring loop was maintaining approximately five-second cadence.

### Decision: separate Railway service

Private Operations will be implemented as a **separate Railway service/process boundary**, not as background AI work inside the live Plane Alerts process.

Reasons:

- the live monitor is alert-critical and must not share failure/resource pressure with optional inference work;
- current memory usage leaves limited headroom for model orchestration, HTTP clients, worktrees and artifacts;
- the current Prediction Lab volume has already operated close enough to capacity that Private Operations state must not compete with it;
- a separate service provides independent restart, resource, health and rollout boundaries;
- the repository already demonstrates that a separate supporting service is operationally possible.

### Decision: dedicated persistent volume

The Private Operations service will receive its **own persistent Railway volume**. It must not store its durable database on:

- the live Plane Alerts Prediction Lab volume;
- the FreeLLMAPI service volume;
- Railway ephemeral filesystem storage.

The exact mount path will be chosen in Phase 1 when the service/volume is provisioned. The logical durable root will contain an orchestration database plus bounded artifacts and temporary data.

## 3. Prediction Lab lifecycle

The current repository already contains the Prediction Lab lifecycle directories including `raw`, `unchecked`, `reviewed`, `solved`, `error_museum`, `archive`, `schemas`, and `state`.

The `prediction-lab-data` branch is active and was receiving current evidence-sync commits during this Phase 0 inventory.

### Decision

Private Operations will **integrate with, reference and preserve** the existing Prediction Lab lifecycle. It will not replace the Prediction Lab database/branch, erase existing evidence, or invent a parallel solved/error-history contract. Cross-system links will use stable case/finding identifiers and source commit/evidence references.

## 4. Current application storage and configuration

Current application code supports both MongoDB and SQLite. The live Railway service does not expose a `DATABASE_BACKEND` variable in the inspected variable-name inventory, while the current parser treats an omitted backend as MongoDB; therefore the current hosted path selects MongoDB under the observed configuration contract. Community/native installs can use SQLite.

Prediction Lab high-volume evidence is already file-backed with migration support away from legacy Mongo collections.

### Decision: Private Operations database

Private Operations will use a **dedicated SQLite database in WAL mode on its own persistent volume** unless Phase 1 testing demonstrates a concrete safety problem that requires another durable store. It will not put high-volume orchestration/event history into the existing application MongoDB by default.

The schema namespace is locked around the roadmap entities:

- `ai_ops_jobs`
- `ai_ops_steps`
- `ai_ops_attempts`
- `ai_ops_tool_operations`
- `ai_ops_findings`
- `ai_ops_reviews`
- `ai_ops_provider_health`
- `ai_ops_usage`
- `ai_ops_events`
- `ai_ops_chat_sessions`
- `ai_ops_chat_messages`
- `ai_ops_feedback`
- `ai_ops_sync_state`
- `ai_ops_restore_history`

Phase 1 may introduce these incrementally, but it must not create a conflicting state model.

## 5. Initial bounded retention targets

These are Phase 0 operational targets, not permanent hard-coded constants. Phase 1 must make them configurable and enforce byte/count bounds as well as time bounds.

| Data class | Initial target |
| --- | --- |
| canonical findings, final review summaries, final task disposition, stable evidence references, user feedback | durable; compact rather than silently delete |
| raw model responses / prompt packages | 7 days, then compact/delete after terminal disposition and verified durable summary |
| retry payloads and verbose tool stdout/stderr copies | 14 days maximum; retain compact result/hash |
| noisy low-value operational events | 14 days |
| provider health/usage fine-grained history | 30 days raw; retain compact daily summaries longer |
| temporary worktrees / scratch artifacts | remove after terminal task; abandoned scratch target <=24 hours |
| future Supervisor full chat messages | initial 90-day active-history target; continuity summaries may be retained longer |

Capacity controls must keep meaningful free headroom. Optional artifact creation must be backpressured before the volume becomes critically full; canonical findings/checkpoints have priority over raw model/debug material.

## 6. Provider and credential inventory

Only secret **names/presence** were inspected. No credential values were printed, copied into this record, or sent to a model.

### Current live application wiring

- Gemini: `GEMINI_API_KEY`, `GEMINI_API_KEY_2` are parsed/wired.
- Groq: `GROQ_KEY`, `GROQ_KEY_2` are parsed/wired, with legacy `GROQ_API_KEY` compatibility in application code.
- OpenSky credential slots remain aircraft-provider credentials and are outside the Private Operations LLM router.
- Mistral Private Operations slots described by the roadmap are **not wired into the current live application configuration**.
- Cloudflare Workers AI Private Operations account/token pairs described by the roadmap are **not wired into the current live application configuration**.
- OpenRouter is used by the separate bounded maintenance workflow when configured, but it is not a normalized live-application provider pool in the current `app/config.py`/`app/ai_keys.py` path.
- The current `app/ai_keys.py` health state is in-memory and limited to Gemini/Groq; it is not the durable provider/key health model required by the Private Operations roadmap.

The roadmap's reported five-key Gemini/Groq/Mistral pools must therefore be treated as **owner-reported intended inventory, not verified production wiring**. Phase 3 must discover/configure the real slots without duplicating credentials or assuming separate quota pools.

### Quota/account independence

Phase 0 did **not** establish that multiple credentials have independent account/project quota. Current production wiring exposes names but not ownership metadata. Cloudflare account separation is also unverified because the Cloudflare slots are not currently wired into the inspected live service configuration.

Until proven by safe metadata/probes in the provider-adapter phase, routing must assume that repeated quota responses can represent a shared provider/account limit and must not burn through every key as a quota-bypass mechanism.

## 7. Provider/model capability pre-screen

Phase 0 reviewed the currently documented provider capabilities and the repository's configured model names. No model is locked as the production Supervisor model in this phase.

- The repository's historical/default Groq model setting must be revalidated before reuse; model availability/deprecations change and Phase 3/9 must query or validate current approved models.
- Gemini remains a plausible structured-output/strong-worker family, but actual configured-key health/free allowance must be measured through the future adapter.
- Mistral is a candidate for independent review/deep work once a real configured slot exists.
- Cloudflare Workers AI remains the preferred reserved Supervisor family from the roadmap, but no live Plane Alerts Cloudflare credential pair was available through the current configuration inventory to perform the required Plane Alerts benchmark now.
- OpenRouter free routing is useful for emergency diversity, but a generic free router may change the underlying model. Session-affinity/quality-sensitive Supervisor use should prefer an explicitly approved free model/route where available.

Official capability/model sources to re-check at implementation time:

- Groq deprecations: https://console.groq.com/docs/deprecations
- Gemini models: https://ai.google.dev/gemini-api/docs/models
- Mistral models: https://docs.mistral.ai/models
- Cloudflare Workers AI: https://developers.cloudflare.com/workers-ai/
- OpenRouter free router: https://openrouter.ai/openrouter/free

### Probe result / limitation

No direct credential-value probe was issued from this ChatGPT session because the connected Railway/GitHub interfaces deliberately do not reveal provider secret values and several roadmap providers are not yet wired into the application. The repository already contains a bounded read-only maintenance smoke path for currently configured free maintenance providers; Phase 0 preserved it rather than adding a duplicate probe mechanism.

This is a deliberate `UNVERIFIED` state, not a claim that all planned credentials work. Phase 3 must perform per-slot free/bounded probes through the normalized adapters before a slot becomes healthy/eligible.

No paid model call was made during Phase 0.

## 8. Existing schedules and overlap

Current active ChatGPT scheduled responsibilities observed during Phase 0:

- Prediction Lab Release — hourly;
- Weekly Community Release — weekly;
- Plane Spotting Slice — weekly.

The repository also has a scheduled bounded `free-maintenance-agent.yml` workflow that runs every two hours. It reads unchecked Prediction Lab cases, can prepare an isolated candidate branch/PR when enabled and justified, and does not merge/deploy production. Its read-only smoke path was recently hardened on the current baseline commit.

### Decision

No new Private Operations scheduler is enabled in Phase 0. Early implementation remains shadow/read-only and must not duplicate the side effects of the hourly release task, weekly community task, or the existing bounded maintenance workflow. Any later handover requires explicit parity evidence, rollback and documented ownership.

## 9. Health/status and existing admin UI

Current application endpoints already include:

- `/health`
- `/ready`
- `/stats`

They expose useful live monitor timing, storage/database state, bot mode, version/commit fields and bounded operational diagnostics.

An existing `/admin` dashboard/API also exists. It is not a safe drop-in backend for the future Private Operations GUI: some authenticated admin routes intentionally expose user identifiers and exact stored profile coordinates. Private Operations must use a separate sanitized API/view model and must never emit exact private observer coordinates into model prompts, logs or general operations events.

Phase 0 also observed that some production log records contain user-identifying values. Private Operations logging/redaction must be stricter and should not copy raw application logs wholesale into its durable event store.

## 10. Release/workflow baseline

Current repository workflows include application tests, Railway deployment, community installer/release paths, Prediction Lab synchronization, secret synchronization and the bounded maintenance workflow.

The Railway and community release channels remain separate. Private Operations releases are owner-private Railway work and must not trigger the full community platform/installer matrix unless a later change actually affects community/self-host behavior.

## 11. Disaster-recovery repository decision

Because the canonical Plane Alerts repository is public, private operational backup data must never be committed there.

### Decision

Use a **dedicated private GitHub repository** for sanitized Private Operations disaster-recovery state. Intended owner/repository name:

`xtenrore/Plane-Alerts-Private-State`

Phase 0 verified that this repository does not currently exist. It is **not created in Phase 0**; Phase 2 owns creation/configuration of the private backup destination and remote verification. If the name becomes unavailable or policy changes, Phase 2 may choose another private repository but must preserve the separation from the public Plane Alerts repository.

GitHub will be a disaster-recovery/export layer, never the live Private Operations database.

## 12. Phase 0 safety verification

Confirmed for this phase:

- no alert-critical code changed;
- no Plane Alerts production data changed;
- no Railway variables/secrets changed;
- no Railway service was redeployed or restarted by this work;
- no scheduled task was created, enabled, disabled or changed;
- no functional provider routing was enabled;
- no credential value was exposed in this record;
- no paid model call was made;
- existing open engineering work was preserved;
- no community installer/platform claim was added.

## 13. Design-lock decisions for Phase 1

Phase 1 must begin from these decisions:

1. Provision/build a **separate Private Operations Railway service**.
2. Give it a **dedicated persistent volume**.
3. Use a dedicated **SQLite WAL orchestration store** unless Phase 1 tests disprove the choice.
4. Keep all provider/model behavior fake/test-only in Phase 1; real adapters remain Phase 3.
5. Preserve the current Prediction Lab branch/lifecycle and cross-reference it rather than replace it.
6. Implement durable jobs/steps/attempts, leases, idempotency, recovery, bounded queue and health before real AI calls.
7. Keep the existing release/community/scheduled responsibilities unchanged.
8. Treat every provider credential slot as unverified until its future adapter performs a safe bounded probe.
9. Do not infer the deployed commit when Railway cannot prove it; carry an explicit unknown/verified state.
10. Keep all Private Operations source inert unless explicitly enabled for the owner deployment.

## 14. Phase 0 completion

Phase 0 is complete when this decision record and the accompanying implementation-state file are reviewed on the Phase 0 branch. No Phase 1 implementation is included in this branch.
