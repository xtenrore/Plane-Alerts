# Plane Alerts v6.0.0 — Private AI Operations Phase 0

## Status

Phase 0 — Baseline, Inventory & Design Lock: **COMPLETE**

Snapshot date: 2026-09-27

This record is the reviewed architecture decision record required before any durable AI Operations job engine or live provider routing is implemented. It records only safe metadata and design decisions. It contains no credential values, private coordinates, raw user data, or provider request payloads.

## Scope

Phase 0 establishes the real production baseline and locks the first implementation boundaries for Plane Alerts Private AI Operations.

Phase 0 does **not** implement:

- the durable job engine;
- AI provider adapters;
- hourly AI auditing;
- AI findings or review workers;
- the private operations GUI;
- Supervisor Chat;
- GitHub/Dropbox backup execution;
- any merge/deploy control;
- any runtime authority over trajectory, CPA, ETA, pass/no-pass, qualification, cancellation, CAMERA READY, PHOTO NOW, or alert timing.

The deterministic Plane Alerts runtime remains authoritative.

## Verified source and production baseline

| Item | Verified Phase 0 state |
| --- | --- |
| Repository | `xtenrore/Plane-Alerts` |
| Baseline branch | `main` |
| Baseline commit | `639464f5a11f5b6690e41a6b80e1c3d9e9d7ee6b` |
| Baseline code version | `5.8.7` |
| Prediction version | `5.3-3d-proximity-age-aware` |
| Railway production version | `5.8.7` |
| Railway production commit | `639464f5a11f5b6690e41a6b80e1c3d9e9d7ee6b` |
| Production readiness | healthy; `/ready` returned HTTP 200 during Phase 0 verification |
| Live monitor cadence | approximately 5 seconds; recent p50 start interval about 5000 ms |
| Latest community stable release observed | `v5.5.3` |
| Phase 0 branch | `release/v6.0.0-phase0` |

The production `/ready` response reported the exact baseline version and commit with `release_match=true`. The v6.0.0 work is intentionally isolated from existing unfinished pull requests, including open Next 60 / Prediction Lab diagnostic work.

## Existing release and workflow boundaries

The current repository already separates Railway production deployment from the longer weekly community/self-host release path.

Observed workflows include:

- normal application tests;
- Railway deployment of the exact tested current `main` commit;
- Prediction Lab synchronization;
- community installer validation;
- community release publication;
- optional bounded free-maintenance inference.

The v6 private AI Operations work must preserve that separation. It must not turn ordinary Railway AI Ops releases into full Windows ARM64, Linux ARM64, Raspberry Pi, installer, updater, rollback, or release-asset matrices.

## Scheduled-task coexistence

Phase 0 inspected the current non-paused schedule set.

Active responsibilities observed:

- **Prediction Lab Release** — hourly Railway release/verification responsibility;
- **Weekly Community Release** — weekly community/self-host validation and publication;
- **Plane Spotting Slice** — weekly planning only.

Older Prediction Lab collector/investigator/auditor tasks remain paused/disabled.

Decision:

- do not re-enable old tasks;
- do not duplicate their production side effects;
- early AI Ops phases remain isolated and non-authoritative;
- the existing hourly Prediction Lab Release and weekly Community Release keep their current ownership until a later explicit handover phase proves parity.

## Current Railway topology

### Live Plane Alerts service

The production Plane Alerts runtime is a single Railway service with:

- one replica;
- Railway `/ready` health checking;
- the live five-second monitor;
- Telegram runtime;
- MongoDB application persistence;
- one persistent volume mounted at `/data/prediction_lab`.

The existing persistent volume capacity observed during Phase 0 is 500 MB. Over the previous 24 hours the service disk metric peaked at approximately 0.45 GB. That is too close to the current capacity to make this volume the default home for a new durable AI Ops database, artifacts, WAL files, or future chat state.

### Existing FreeLLMAPI maintenance gateway

A second Railway service named `Plane-Alerts-FreeLLMAPI` already exists.

Verified characteristics:

- separate persistent volume mounted at `/app/server/data`;
- private-network service;
- image-based FreeLLMAPI deployment;
- intended for optional repository-side maintenance inference;
- no upstream provider keys were configured at the time of Phase 0 verification (`0 keys` in its health logs).

Decision:

**Do not repurpose this service as the Plane Alerts AI Ops orchestrator or state database.**

It remains an optional inference gateway. AI Ops task ownership, checkpoints, findings, permissions, and durable state must belong to Plane Alerts AI Ops itself.

## Storage inventory

Current storage responsibilities are intentionally split:

- **MongoDB** — durable application data such as users, Telegram identity, locations, profiles, preferences, and bounded application configuration/state;
- **self-host SQLite** — supported local application backend;
- **`/data/prediction_lab` volume** — high-volume Prediction Lab evidence, route-history SQLite, notification lifecycle telemetry, and related bounded operational state;
- **FreeLLMAPI volume** — that gateway's own encrypted provider/router state.

AI Ops must not add high-volume operational event spam to MongoDB merely because MongoDB already exists.

## Provider credential/configuration inventory

Only secret **names/presence**, never values, were inspected.

The current Plane Alerts production service exposes the existing runtime set:

- `GEMINI_API_KEY`
- `GEMINI_API_KEY_2`
- `GROQ_KEY`
- `GROQ_KEY_2`
- five OpenSky credential slots used for ADS-B, not AI

The current runtime parser and `app/ai_keys.py` match this limited state: two Gemini slots and two Groq slots, with a legacy Groq compatibility variable in source.

The wider v6 roadmap expects future AI Ops support for additional Gemini/Groq slots plus Mistral, Cloudflare Workers AI, OpenRouter, and a disabled/manual Cohere trial slot. Those additional credentials are **not** currently part of the live Plane Alerts service configuration observed in Phase 0.

No provider secret migration is performed in v6.0.0. The later provider-adapter phase must support the exact configured secret names that exist at implementation time without exposing values.

## Provider health probes

No real LLM inference health probe was run in Phase 0.

Reason:

- the target expanded AI Ops credential inventory is not currently present in the Plane Alerts runtime;
- the existing FreeLLMAPI gateway currently has zero upstream provider keys;
- Phase 0 must not mutate secrets or introduce paid usage merely to satisfy an inventory check.

This is an explicit verified `NOT_RUN`, not a claimed success.

Future probes must be:

- minimum-cost/free;
- bounded;
- non-streaming unless needed;
- credential-slot aware;
- redacted;
- incapable of selecting a paid route while paid usage is disabled.

## Current provider capability pre-screen

This is a documentation/capability pre-screen only. It is **not** the Plane Alerts Supervisor benchmark and does not select a production Supervisor model.

### Gemini

As of the Phase 0 review, Google documents Gemini 3.5 Flash as a current GA Gemini API model and documents function calling. Gemini remains a candidate for deep investigation and complex Supervisor fallback, subject to the user's actual free allowance and a later Plane Alerts-specific benchmark.

References:

- https://ai.google.dev/gemini-api/docs/changelog
- https://ai.google.dev/gemini-api/docs/function-calling

### Groq

Groq documents strict structured outputs for selected current models including GPT-OSS and Qwen 3.8. The same documentation states that streaming and tool use are not supported simultaneously with Structured Outputs in that strict mode. That constraint must be benchmarked rather than hidden by the adapter.

References:

- https://console.groq.com/docs/structured-outputs
- https://console.groq.com/docs/api-reference

### Mistral

Mistral documents Free-mode API access with limited usage/rate limits. Current model documentation includes models with function calling and structured outputs. Mistral remains a candidate for independent review and engineering tasks, but no production model is hardcoded in Phase 0.

References:

- https://docs.mistral.ai/getting-started/quickstarts/studio/activate-and-generate-api-key
- https://docs.mistral.ai/getting-started/models/compare
- https://docs.mistral.ai/getting-started/quickstarts/developer/build-an-agent

### Cloudflare Workers AI

Cloudflare documents Workers AI on Free and Paid plans, a free allocation of 10,000 Neurons per day, JSON mode, and function calling. Some resource-intensive models require the Paid plan while many models remain available on Workers Free.

Cloudflare remains the preferred family to benchmark for reserved Supervisor capacity, but Phase 0 does not select a model.

References:

- https://developers.cloudflare.com/workers-ai/
- https://developers.cloudflare.com/workers-ai/platform/pricing/
- https://developers.cloudflare.com/workers-ai/features/json-mode/
- https://developers.cloudflare.com/workers-ai/features/function-calling/
- https://developers.cloudflare.com/changelog/post/2026-07-28-models-require-workers-paid/

### OpenRouter

OpenRouter currently exposes an `openrouter/free` router that selects from free models and filters by requested capabilities such as tool calling and structured outputs. Because the selected underlying model can vary, Plane Alerts will treat it as an emergency diversity/fallback route, not a source of stable model identity.

Reference:

- https://openrouter.ai/openrouter/free

### Cohere

`COHERE_TRIAL_KEY` remains excluded from unattended production work by the v6 roadmap. Phase 0 performs no Cohere call.

## ADR-001 — Deployment isolation

**Decision: build Private AI Operations as a separate private Railway service/process, using the Plane Alerts repository/release commit, with its own persistent volume.**

Reasons:

1. AI timeouts and provider failures must never compete with the five-second monitor for process liveness.
2. The existing `/data/prediction_lab` volume has already operated close to capacity.
3. The current FreeLLMAPI service is a provider gateway, not a durable Plane Alerts task orchestrator.
4. Separate restart/resource policy gives a clearer failure boundary.
5. Future GUI/Supervisor work can remain private without changing the live Telegram service surface.

Phase 1 target:

- new private AI Ops service;
- no public domain;
- private Railway networking where service-to-service access is needed;
- dedicated persistent volume;
- proposed mount: `/data/ai_ops`;
- exact capacity confirmed at provisioning time;
- AI Ops disabled/inert if its durable store is unavailable;
- live Plane Alerts remains fully functional if AI Ops is absent.

Phase 0 does not create the service or volume. Provisioning belongs to Phase 1.

## ADR-002 — Durable state engine

**Decision: use SQLite in WAL mode for the initial AI Ops orchestration database on the dedicated AI Ops volume.**

Proposed Phase 1 path:

`/data/ai_ops/ai_ops.sqlite3`

The design must use transactions, foreign keys, busy timeout, WAL checkpointing, bounded cleanup, schema migrations, and corruption/startup validation.

No AI Ops database is created in Phase 0.

## Phase 1 schema contract

Phase 1 should implement only the state-machine foundation required before real AI is connected.

Initial entities:

### `ai_ops_meta`

- schema version;
- migration version;
- created/updated timestamps;
- service instance metadata that contains no secrets.

### `ai_ops_jobs`

- stable job ID;
- job type;
- status;
- priority;
- created/updated timestamps;
- next eligible time;
- idempotency key;
- safe source/version references;
- terminal reason.

### `ai_ops_steps`

- stable step ID;
- job ID;
- step type;
- status;
- sequence/order;
- input artifact hash references;
- output artifact hash references;
- attempt count;
- lease owner;
- lease expiry;
- error class;
- retry eligibility;
- created/started/completed timestamps.

### `ai_ops_attempts`

- attempt ID;
- job/step IDs;
- attempt number;
- fake-provider identifier for Phase 1 tests;
- start/end timestamps;
- result class;
- error class;
- validated-output hash reference where applicable.

### `ai_ops_tool_operations`

Phase 1 may create the table/contract for exactly-once operation IDs even though side-effecting engineering tools are not enabled until a later phase.

- operation ID;
- job/step ID;
- operation type;
- idempotency key;
- status;
- result hash/reference;
- start/end timestamps.

### `ai_ops_scheduler_state`

- scheduler key;
- last success/checkpoint;
- next due time;
- leader lease owner;
- leader lease expiry;
- updated timestamp.

### `ai_ops_events`

- monotonic event ID/cursor;
- event type;
- job/step reference;
- safe compact payload;
- timestamp.

Provider health, findings, reviews, chat, GitHub sync, Dropbox backup, and feedback tables are intentionally deferred to the phases that own those features.

## State-machine contract

Phase 1 statuses should remain explicit and resumable.

Jobs/steps:

- `PENDING`
- `READY`
- `RUNNING`
- `RETRYING`
- `COMPLETE`
- `FAILED`
- `CANCELLED`

On startup:

1. validate durable directory;
2. open database safely;
3. verify schema/migrations;
4. detect expired leases;
5. return interrupted resumable steps to an eligible state;
6. preserve completed steps;
7. never repeat a completed idempotent side effect;
8. expose health only after durable state is usable.

The Phase 1 scheduler is infrastructure-only. The hourly trajectory audit scheduler does not begin until its later roadmap phase.

## Retention and capacity targets

Initial targets are deliberately conservative and must remain configurable.

- non-terminal jobs/steps: retain until terminal;
- terminal job/step/attempt metadata: 90 days;
- low-value event stream: 30 days and at most 100,000 retained events;
- temporary Phase 1 artifacts: 24 hours;
- canonical Prediction Lab/Error Museum evidence: never deleted by AI Ops retention;
- raw provider responses: not created in Phase 1;
- Supervisor chat: not created in Phase 1.

Volume guardrails:

- soft backpressure threshold: 60% used;
- hard claim/persistence safety threshold: 80% used;
- remaining capacity reserved for SQLite WAL, checkpoints, recovery, and bounded transient files.

The Phase 1 implementation must measure actual filesystem capacity rather than assuming a fixed Railway volume size.

## ADR-003 — Disaster-recovery repository

**Decision: reserve a dedicated private GitHub repository named `xtenrore/Plane-Alerts-Private-State` for future sanitized AI Ops disaster-recovery exports.**

Phase 0 verified that this repository name is not currently provisioned.

Rules:

- do not place private AI Ops operational state in the public `xtenrore/Plane-Alerts` repository;
- do not treat GitHub as the live database;
- create/provision the private repository only when the Phase 2 DR sync implementation is built;
- sync only sanitized state until encrypted full snapshots are introduced by their later phase;
- never commit credential values, raw Authorization headers, exact private coordinates, or unredacted private logs.

## ADR-004 — Provider selection and benchmark rule

No Supervisor model/provider is selected in Phase 0.

Before production Supervisor Chat:

- run the Plane Alerts-specific benchmark from the roadmap;
- test tool correctness, grounding, state accuracy, command safety, source understanding, structured output, continuity, latency, and free-allocation sustainability;
- verify the current provider/model is actually available to the configured account;
- keep paid usage disabled;
- prefer session affinity over rotating models every message.

Public/provider documentation is only a capability filter. It is not a substitute for the Plane Alerts benchmark.

## Security and privacy lock

Every later phase inherits these Phase 0 decisions:

- no secret values in the AI Ops DB;
- no API keys in logs, GUI payloads, GitHub exports, model prompts unless the provider request itself requires its own authentication outside prompt content;
- no `ADMIN_PASSWORD`, `MONGO_URI`, Railway token, Telegram token, Dropbox refresh token, or Authorization header sent to an LLM;
- no exact private observer coordinates unless a later task genuinely requires location and an explicit minimization/redaction design exists;
- runtime AI never decides physical alert-critical outputs;
- no Railway AI Agent;
- no retired external-agent integration;
- paid usage is fail-closed and disabled until explicitly approved.

## Phase 0 verification result

Completed:

- repository/main/version inspected;
- open PRs inspected and preserved;
- Railway service topology inspected;
- persistent-volume mounts inspected;
- production version/commit/readiness verified;
- monitor cadence checked;
- Prediction Lab storage/synchronization architecture inspected;
- database backends inspected;
- current configuration/provider parsers inspected;
- active/paused scheduled responsibilities inspected;
- health/readiness implementation inspected;
- release/deploy workflows inspected;
- current provider capabilities pre-screened using current provider documentation;
- separate-service versus in-process decision made from actual topology/storage pressure;
- Phase 1 schema/state/recovery/retention contract defined;
- private GitHub DR destination chosen;
- no secret values stored in this record;
- no production mutation performed;
- no paid model call performed;
- no live LLM inference call performed.

Not performed by design:

- no new Railway service;
- no new Railway volume;
- no AI Ops database;
- no job engine;
- no provider adapter;
- no credential migration;
- no scheduler replacement;
- no production deployment;
- no community stable release.

## Phase 1 handoff

The next phase is **Phase 1 — Durable AI Ops Storage & Job Engine**.

It may begin only from the verified v6.0.0 Phase 0 design lock and must implement:

- the dedicated AI Ops persistent store;
- migrations;
- jobs/steps/attempts;
- leases;
- idempotency;
- restart recovery;
- scheduler state;
- bounded queue;
- health endpoint;
- graceful shutdown;
- fake provider/testing only.

It must **not** connect the full live provider router or hourly AI audit.

This file intentionally stops here.
