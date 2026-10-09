PLANE ALERTS — PRIVATE AI OPERATIONS, HOURLY AUDIT, SUPERVISOR CHAT & GUI
COMPLETE PHASED IMPLEMENTATION ROADMAP
=========================================================================

STATUS
------
Planning specification only.

This document is intentionally detailed. It is meant to be read by ChatGPT/future maintainers before implementing the private Plane Alerts AI Operations system.

IMPORTANT: Implement this entire document in one release.

Every phase below is an independently testable and independently releasable unit. If a phase proves too large for one safe release, split it into smaller sequential releases. Never combine later phases merely to save time.

This system is PRIVATE OWNER INFRASTRUCTURE for the hosted Plane Alerts deployment. It is NOT a community/self-host feature at this time.

Before every implementation phase, ChatGPT MUST read the current Plane Alerts standing instruction files, inspect the actual repository and production state, inspect the current active/paused scheduled tasks, and follow the newer Railway-vs-community release policy.

Do not assume version numbers written in older prompts or files are still current. Determine the actual current three-part semantic version from the repository and live Railway deployment before starting each release.

AGY / Antigravity / retired external-agent integrations are permanently retired. They must never be restored, mounted, referenced, re-created, or used by this system.

Do not use Railway AI Agent.

=========================================================================
1. PURPOSE
=========================================================================

The goal is to build a private Plane Alerts AI Operations system that:

1. runs an hourly prediction/trajectory audit independently of the five-second live aircraft monitoring loop;
2. deterministically collects and compresses interesting trajectory/alert evidence before using AI;
3. uses multiple AI providers for triage, independent review, difficult-case escalation and engineering investigation;
4. survives individual API-key failures, provider failures, rate limits, malformed responses, process crashes, Railway restarts and deployments;
5. can resume an interrupted task from durable checkpoints instead of restarting everything;
6. uses the user's existing Groq, Mistral, Gemini, Cloudflare Workers AI and OpenRouter credentials correctly;
7. does not treat multiple keys as permission to evade provider quotas, account-level limits or terms;
8. provides a professional private GUI showing workers, tasks, findings, provider health, tool activity, failures, failovers and GitHub synchronization;
9. provides a separate user-facing Supervisor AI chat so the owner can ask what happened, inspect evidence, assign work, request another opinion, stop/retry work, run safe tools and eventually approve controlled actions;
10. keeps Supervisor conversation/task continuity even if its model/provider changes;
11. stores durable state outside ephemeral Railway filesystems;
12. synchronizes recoverable private state to GitHub so the AI Operations system can be rebuilt or migrated away from Railway;
13. keeps secrets and exact private locations out of Git history, GUI logs, model prompts where unnecessary, release assets and community output;
14. remains useful when every AI provider is unavailable;
15. never allows runtime AI to decide trajectory, CPA, ETA, pass/no-pass, qualification, cancellation or alert timing.

The AI system is an auditing, engineering and operational intelligence layer.

The deterministic Plane Alerts flight/prediction system remains authoritative.

=========================================================================
2. NON-NEGOTIABLE PLANE ALERTS BOUNDARIES
=========================================================================

The following rules apply to every phase.

A. ALERT-CRITICAL LOGIC REMAINS DETERMINISTIC

AI MUST NOT decide:

- trajectory;
- CPA;
- ETA/TTC;
- pass/no-pass;
- alert qualification;
- alert cancellation;
- alert timing;
- CAMERA READY timing;
- PHOTO NOW timing.

AI may:

- inspect already produced telemetry;
- classify anomalies;
- compare prediction versus later evidence;
- identify suspicious patterns;
- explain evidence;
- propose hypotheses;
- request deterministic replay;
- inspect source code through controlled tools;
- generate candidate patches in an isolated engineering branch/sandbox;
- propose regression tests;
- create structured findings;
- create release candidates only after deterministic tests support them.

B. PROTECT THE FIVE-SECOND MONITORING PATH

All AI Operations work is low-priority optional work.

It must use:

- bounded queues;
- bounded concurrency;
- timeouts;
- circuit breakers;
- storage retention;
- batching;
- backpressure;
- isolated execution where practical.

A slow or failed AI provider must never delay live ADS-B ingestion, freshness checking, trajectory calculations, qualification or Telegram alerts.

C. MISSING ADS-B COVERAGE IS INCONCLUSIVE

Never classify missing coverage as a successful prediction or a prediction miss.

D. NO PAID RUNTIME DEPENDENCY WITHOUT EXPLICIT APPROVAL

Use only the free allowances/models/routes available to the owner unless the owner explicitly approves paid usage later.

The system must have a hard "paid usage disabled" policy flag.

E. DO NOT TURN PRIVATE AI OPS INTO A COMMUNITY REQUIREMENT

At this time:

- do not add AI Operations API keys to normal self-host installer prompts;
- do not make the community release depend on AI Operations;
- do not require AI Operations for physical alert correctness;
- do not expose the private admin GUI publicly by default;
- do not advertise private AI Ops as a normal self-host feature;
- community installers must remain functional without these credentials;
- if source code for AI Ops exists in the public repository, it must remain disabled/inert unless explicitly enabled in the owner's deployment.

F. PRESERVE CURRENT PREDICTION LAB HISTORY

Do not clear or replace existing durable Prediction Lab evidence.

If the current repository still uses:

- prediction-lab-data;
- raw;
- unchecked;
- reviewed;
- solved;
- Error Museum;
- release_candidate.json;
- state checkpoints;

the new system must integrate with the actual current lifecycle rather than inventing a conflicting one.

Inspect the repository before implementation because filenames/formats may have changed.

=========================================================================
3. HIGH-LEVEL ARCHITECTURE
=========================================================================

Target architecture:

                         PLANE ALERTS
                              |
                  production telemetry/events
                              |
                              v
                DETERMINISTIC AUDIT FILTER
                              |
                   encounter/evidence builder
                              |
                              v
                   DURABLE TASK ORCHESTRATOR
                              |
                    persistent checkpoints
                              |
               +--------------+--------------+
               |              |              |
               v              v              v
          GROQ POOL      MISTRAL POOL    GEMINI POOL
          5 slots         5 slots         5 slots
               |              |              |
               +--------------+--------------+
                              |
                    provider/key router
                              |
                  +-----------+-----------+
                  |                       |
                  v                       v
          CLOUDFLARE WORKERS AI       OPENROUTER
             2 credential pairs       emergency route
                  |                       |
                  +-----------+-----------+
                              |
                       validated AI work
                              |
                 independent review/escalation
                              |
                     sandbox/tool gateway
                              |
                 replay/tests/repository read
                              |
                              v
                  structured durable findings
                              |
             +----------------+----------------+
             |                                 |
             v                                 v
       PRIVATE OPERATIONS GUI             SUPERVISOR CHAT
             |                                 |
             +----------------+----------------+
                              |
                       PRIVATE CONTROL API
                              |
                              v
                      GITHUB DR SYNC
                  + persistent local state


Core rule:

NO API KEY, MODEL, PROVIDER, PROCESS OR WORKER OWNS TASK PROGRESS.

Task progress belongs only to the durable Plane Alerts AI Operations state.

Any compatible healthy provider may continue the next unfinished idempotent step.

=========================================================================
4. CURRENT SECRET INVENTORY AND EXACT ROLE MAPPING
=========================================================================

The user reports the following GitHub Secrets already exist.

ONLY SECRET NAMES may appear in source, logs, status pages and this document.
Never print secret values.

-----------------------------------------------------------------------
4.1 AI WORKER PROVIDERS
-----------------------------------------------------------------------

GEMINI
- GEMINI_API_KEY
- GEMINI_API_KEY_2
- GEMINI_API_KEY_3
- GEMINI_API_KEY_4
- GEMINI_API_KEY_5

Meaning:
Five configured Gemini credential slots.

For this Plane Alerts deployment, the five Gemini API keys are five independent quota pools.
Track quota, cooldown, failures and usage separately for every Gemini key slot.
When one Gemini key is exhausted or unavailable, continue the unfinished idempotent step with another healthy Gemini key before failing over to another provider.

Use a native Gemini adapter.
Do not blindly send Gemini through a Groq/Mistral/OpenAI client merely because the high-level request schema looks similar.

As of 2026-09-27, Google documentation says Gemini API requests authenticate with Gemini API credentials and the REST API supports x-goog-api-key. Google has also been migrating API keys toward authorization keys. Implementation must health-check the actual configured keys rather than assuming old key behavior remains valid.

-----------------------------------------------------------------------

GROQ
- GROQ_KEY
- GROQ_KEY_2
- GROQ_KEY_3
- GROQ_KEY_4
- GROQ_KEY_5

Meaning:
Five configured Groq credential slots.

Use a Groq adapter.
Groq is OpenAI-compatible for supported APIs, currently using the Groq OpenAI-compatible base URL.

Do not rename existing secrets just because official documentation examples use GROQ_API_KEY.
The adapter maps the repository's actual secret names to provider slots.

For this Plane Alerts deployment, the five Groq API keys are five independent quota pools.
Track quota, cooldown, failures and usage separately for every Groq key slot.
When one Groq key is exhausted or unavailable, continue with another healthy Groq key before failing over to another provider.
Each key's own provider/model rate limits must still be respected.

-----------------------------------------------------------------------

MISTRAL
- MISTRAL_API
- MISTRAL_API_2
- MISTRAL_API_3
- MISTRAL_API_4
- MISTRAL_API_5

Meaning:
Five configured Mistral credential slots.

Use a Mistral adapter.
Mistral has its own SDK/API semantics and supports chat/tool calling.
Do not treat Mistral as merely "another Groq key".

Do not rename these existing secrets until the separate structured-provider-configuration roadmap release actually migrates credentials safely.

-----------------------------------------------------------------------

CLOUDFLARE WORKERS AI — SLOT 1
- CLOUDFLARE_ACCOUNT_ID
- CLOUDFLARE_API_TOKEN

CLOUDFLARE WORKERS AI — SLOT 2
- CLOUDFLARE_ACCOUNT_ID_2
- CLOUDFLARE_API_TOKEN_2

IMPORTANT:
Each Cloudflare Workers AI slot consists of ONE Account ID + ONE API token pair.

The Account ID is not an API key.
The API token alone is not enough to identify the Workers AI account endpoint.
Never cross-pair ACCOUNT_ID_1 with API_TOKEN_2 or vice versa.

Use a dedicated Cloudflare Workers AI adapter.

Current official Workers AI REST usage is account-scoped and authenticates with an Authorization Bearer token.
Workers AI model names use the Cloudflare model catalog.
Do not hardcode a model forever; validate current model availability/capability before release.

The two Cloudflare credential pairs are independent provider slots for this deployment.
Track their health, quota/usage and cooldown separately.

The two Cloudflare slots are primarily reserved for Supervisor/control-plane use so hourly worker traffic does not consume all chat capacity.

They may be used for worker fallback only if Supervisor reserve policy allows it.

-----------------------------------------------------------------------

OPENROUTER
- OPENROUTER_API

Role:
Emergency provider/model diversity and optional Supervisor fallback.

Use OpenRouter only with explicitly allow-listed free routes/models while paid usage is disabled.

Do not silently select a paid model.
Do not assume a model remains free forever.
The router must have a current allow-list/configuration and fail closed if a route is not approved.

OpenRouter is OpenAI-compatible at its own base URL and supports model/provider fallbacks, but Plane Alerts should still record the actual selected route/model where returned/known.

-----------------------------------------------------------------------

COHERE
- COHERE_TRIAL_KEY

Role:
NOT an unattended production dependency.

The current key is a trial/evaluation key.
Keep it disabled for normal hourly production work.

Allowed uses:
- development benchmark;
- manual evaluation;
- optional one-off model comparison when explicitly enabled.

It must never become necessary for the hourly audit or Supervisor availability.

-----------------------------------------------------------------------
4.2 NON-AI SECRETS — DO NOT MIX THESE INTO THE AI ROUTER
-----------------------------------------------------------------------

ADMIN_PASSWORD
Role:
Private admin GUI authentication/control-plane access.

Do not send it to any model.
Do not log it.
Do not expose it in status output.

BACK4APP_API
Role:
Existing non-AI project/service integration.
Do not use it as an LLM provider unless the actual current repository explicitly proves otherwise.

GOOGLE_CONTRAILS_API_KEY
Role:
Contrail/environment feature only.
Never use as a Gemini LLM credential.

MONGO_URI
Role:
Existing MongoDB connection.
Never pass to AI.
Never display.
Do not put high-volume AI Ops event spam into Mongo merely because Mongo exists.

NF_API_TOKEN
Role:
Unknown from this planning context.
DO NOT guess.
Before using it, inspect the actual repository/parser/docs and identify what it belongs to.

OPENSKY_1
OPENSKY_2
OPENSKY_3
OPENSKY_4
OPENSKY_5
Role:
OpenSky/ADS-B provider credentials according to current project parser.

They are NOT AI keys.
Do not send them to the AI router.
Do not use them to evade OpenSky quotas.
Read the actual OpenSky parser before touching them.

RAILWAY_API_TOKEN
Role:
Railway administration/deployment tooling.
Never give directly to an AI model.
Only controlled tool gateway code may use it for explicitly allowed operations.

TELEGRAM_BOT_TOKEN
Role:
Telegram bot runtime.
Never give to AI.
Never display in GUI/logs/GitHub backup.

=========================================================================
5. FUTURE STRUCTURED CREDENTIAL MIGRATION COMPATIBILITY
=========================================================================

The standing architecture roadmap already contains a future release for replacing numbered provider environment variables with structured provider credential configuration.

This AI Operations project must NOT prematurely perform that migration unless its proper roadmap phase is currently being implemented.

Until then:

- support the exact current secret names listed above;
- create an internal normalized ProviderCredentialSlot abstraction;
- keep the internal router independent of environment-variable naming;
- allow a future config loader to populate the same slots from a canonical JSON structure;
- never silently load the same credential twice from both legacy and future config;
- never expose credential values;
- never use multiple credentials to bypass provider/account quotas.

Internal abstraction example:

ProviderCredentialSlot:
- provider
- slot_id
- secret_name_reference
- account_id_secret_name_reference where required
- enabled
- health_state
- last_success
- last_failure
- cooldown_until
- quota_scope_hint
- usage counters
- capability flags

No secret value is stored in the database.

=========================================================================
6. PROVIDER AND KEY HEALTH MODEL
=========================================================================

Track TWO health layers:

A. KEY/CREDENTIAL SLOT HEALTH
- HEALTHY
- BUSY
- DEGRADED
- RATE_LIMITED
- COOLDOWN
- AUTH_FAILED
- DISABLED
- PROBING

B. PROVIDER HEALTH
- HEALTHY
- DEGRADED
- RATE_LIMITED
- OUTAGE
- DISABLED

Why both are required:

A 401 on one key may be key-specific.
A sequence of matching quota 429 responses on multiple keys may indicate account/project/provider-level exhaustion.

Do not burn through all five keys when evidence suggests a shared provider-level limit.

Provider health records must include safe metadata only:
- provider;
- credential slot number;
- model;
- last success;
- last error class;
- cooldown;
- latency EMA;
- successful calls;
- failed calls;
- input/output token counts where provider reports them;
- parse failures;
- tool-call failures.

Never store:
- API key text;
- Authorization headers;
- secret-bearing raw requests.

=========================================================================
7. FAILURE AND FAILOVER POLICY
=========================================================================

Typical handling:

TIMEOUT
- mark attempt failed;
- retry current idempotent unit with another compatible healthy credential/provider;
- do not restart the whole audit.

CONNECTION RESET
- same as timeout.

HTTP 429 / provider quota
- read Retry-After/reset information where available;
- classify key-level versus provider-level behavior;
- place the affected scope in cooldown;
- fail over to another provider;
- do not brute-force every credential as a quota-bypass technique.

HTTP 401/403
- classify as authentication/permission failure;
- disable that credential slot;
- surface an admin warning;
- do not repeatedly retry.

HTTP 5xx
- mark provider/key degraded;
- use circuit breaker;
- fail over.

MALFORMED JSON / SCHEMA FAILURE
- one bounded repair attempt when safe;
- otherwise retry the step on another model/provider;
- never accept invalid structured output.

MODEL UNAVAILABLE
- choose another approved model/provider.

CONTEXT TOO LARGE
- deterministically compact/split the evidence;
- never silently truncate critical evidence.

ALL PROVIDERS UNAVAILABLE
- persist PENDING_AI;
- continue collecting deterministic evidence;
- retry later;
- lose no case.

Circuit breaker example:

3 relevant failures in a short window
-> OPEN circuit
-> stop sending normal requests
-> wait configured cooldown
-> HALF OPEN
-> one small health probe
-> success: CLOSED
-> failure: OPEN again

No infinite retry loops.

=========================================================================
8. PROVIDER ROLE POOLS
=========================================================================

Do not randomly choose a model for every request.

Use role-based pools.

-----------------------------------------------------------------------
8.1 ROUTINE TRIAGE
-----------------------------------------------------------------------

Default preference:
1. Groq fast approved model
2. alternate Groq compatible model when failure is model-specific rather than provider quota
3. Mistral fast approved model
4. Gemini Flash/Flash-Lite class model
5. approved Cloudflare worker model if Supervisor reserve permits
6. OpenRouter approved free fallback
7. persistent queue

Purpose:
Fast structured classification of compact hourly evidence batches.

-----------------------------------------------------------------------
8.2 INDEPENDENT REVIEW
-----------------------------------------------------------------------

Default preference:
1. Mistral
2. Gemini
3. Groq using a meaningfully different model
4. Cloudflare
5. OpenRouter

Critical rule:
The reviewer should normally NOT be the same provider/model that performed the first triage.

Changing only the API key is not an independent review.

The second reviewer should receive the evidence without being told the first model's conclusion where practical.

This reduces anchoring.

-----------------------------------------------------------------------
8.3 DEEP INVESTIGATION
-----------------------------------------------------------------------

Default preference:
1. Gemini strong/current approved model
2. Mistral strong/coding model
3. Groq stronger approved model
4. approved Cloudflare strong model
5. OpenRouter free approved model
6. queue for later

Deep investigation may receive:
- longer trajectory history;
- relevant telemetry;
- provider/freshness state;
- prediction snapshots;
- alert lifecycle;
- route/destination evidence;
- relevant source excerpts;
- prior similar Error Museum cases;
- replay output;
- test output.

It still cannot directly alter production.

-----------------------------------------------------------------------
8.4 SUPERVISOR CHAT
-----------------------------------------------------------------------

Supervisor capacity is deliberately separated from normal workers.

Preferred reserved provider family:
Cloudflare Workers AI Slot 1
-> Cloudflare Workers AI Slot 2
-> reserved strong fallback selected by benchmark
-> OpenRouter approved free fallback
-> local/read-only degraded mode later if implemented

Do NOT permanently hardcode a Cloudflare model before benchmarking it on Plane Alerts Supervisor tasks.

At implementation time, inspect the current Workers AI catalog and benchmark suitable tool-capable models.

The Supervisor should use SESSION AFFINITY:
- choose one suitable model when a chat starts;
- keep it for that session;
- do not rotate every message;
- switch only for failure/quota/health/explicit user request;
- when switching, restore a continuity packet plus current backend state.

Complex questions may delegate to a strong worker even when the Supervisor itself is a cheaper model.

This avoids requiring the chat model to personally perform every deep engineering analysis.

=========================================================================
9. SUPERVISOR MODEL BENCHMARK
=========================================================================

Before enabling the user-facing Supervisor as production-quality, create a Plane Alerts-specific benchmark.

Candidate models/providers are tested on realistic tasks such as:

- "What happened in the last audit?"
- "Which agents disagreed on finding X and why?"
- "Show evidence for this cancellation."
- "Do not guess when evidence is missing."
- "Run the existing replay test."
- "Find the relevant source file."
- "Do not modify production."
- "Ask another provider for an independent review."
- "Continue an interrupted investigation."
- "Show provider health without leaking credentials."
- "Explain a failed test accurately."
- "Create a candidate patch only in an isolated engineering branch."
- "Which task is currently using Gemini?"
- "Was this finding useful based on eventual evidence?"
- "Stop this task without stopping the live aircraft monitor."

Score:
- tool-call correctness — critical;
- hallucination/grounding — critical;
- task-state accuracy — critical;
- command safety — critical;
- source-code understanding — high;
- multi-step planning — high;
- structured-output reliability — high;
- long-context ability — high;
- conversation quality — medium;
- latency — medium;
- free allowance/sustainability — high.

Do not choose the Supervisor solely from public benchmark scores.

Choose based on Plane Alerts-specific tests.

=========================================================================
10. DURABLE TASK ORCHESTRATOR
=========================================================================

Every job is a durable state machine.

Example:

audit-2026-09-27-14
- prepare_window: COMPLETE
- group_encounters: COMPLETE
- triage_batch_001: COMPLETE
- triage_batch_002: IN_PROGRESS
- independent_review_184: PENDING
- deep_investigation_184: PENDING
- publish_finding_184: PENDING

Suggested tables/entities:

ai_ops_jobs
ai_ops_steps
ai_ops_attempts
ai_ops_tool_operations
ai_ops_findings
ai_ops_reviews
ai_ops_provider_health
ai_ops_usage
ai_ops_events
ai_ops_chat_sessions
ai_ops_chat_messages
ai_ops_feedback
ai_ops_sync_state
ai_ops_restore_history

Every step has:
- stable task_id;
- stable step_id;
- status;
- input artifact hashes;
- output artifact hashes;
- attempt number;
- provider/model;
- credential slot number, not secret;
- timestamps;
- lease owner;
- lease expiry;
- error classification;
- retry eligibility.

=========================================================================
11. MID-TASK CONTINUATION
=========================================================================

LLM hidden computation cannot be transferred mid-token from one provider to another.

Therefore continuation must happen at APPLICATION CHECKPOINTS.

Do not ask one model to perform a giant uncheckpointed task.

Break work into durable units.

Example:

A. normalize evidence;
B. triage cases 1-10;
C. triage cases 11-20;
D. independent review case 7;
E. run replay;
F. inspect replay result;
G. search relevant source;
H. propose candidate test;
I. generate structured finding.

After every complete unit:
- validate output;
- store it;
- hash it;
- mark COMPLETE.

If an API fails during a unit:
- discard incomplete/unvalidated partial output;
- keep completed earlier units;
- retry only the current unit with another compatible provider.

For streamed structured outputs:
- commit only complete validated records;
- do not trust arbitrary half-generated JSON.

=========================================================================
12. EXACTLY-ONCE / IDEMPOTENT TOOL OPERATIONS
=========================================================================

Agentic work can run commands, so side effects need operation IDs.

Example:

op-1001 clone_or_fetch_repo COMPLETE
op-1002 checkout_exact_commit COMPLETE
op-1003 run_replay COMPLETE
op-1004 inspect_result IN_PROGRESS
op-1005 create_candidate_test PENDING
op-1006 create_patch PENDING

Before a side-effecting action:
- check operation_id;
- if already COMPLETE, return stored result;
- otherwise execute and record.

If a worker dies after running a command but before producing prose, the next model reads the durable tool result instead of repeating the command blindly.

Use leases for workers.

Example:
- worker-A claims case for 5 minutes;
- worker-A heartbeats;
- worker-A crashes;
- lease expires;
- worker-B resumes from last completed operation.

=========================================================================
13. HOURLY AUDIT PIPELINE
=========================================================================

The hourly AI audit must NOT mean "send one hour of raw logs to an LLM".

Target:

PRODUCTION TELEMETRY
-> deterministic time-window reader
-> deterministic anomaly filter
-> group by real aircraft encounter
-> compact evidence packets
-> batch
-> AI triage only when needed
-> independent review only when needed
-> deep investigation only when needed

Relevant event families include actual current equivalents of:

- TRAJECTORY_CHANGED;
- APPROACH_QUALIFIED;
- APPROACH_CANCELLED;
- APPROACH_HELD;
- route veto changes;
- destination resolution;
- CPA prediction changes;
- ETA/TTC changes;
- CAMERA READY / PHOTO NOW timing evidence where relevant;
- PASS_OBSERVED / closest observed pass;
- provider changes;
- stale samples;
- rejected/outlier samples;
- storage/evidence gaps;
- Next 60 shadow forecast/outcome events.

Deterministic anomaly pre-filter examples:

- CPA changes by a configured meaningful amount;
- inside-radius projected CPA becomes outside-radius;
- APPROACHING -> NOT_APPROACHING;
- alert cancellation;
- cancellation followed by rapid re-qualification;
- repeated qualify/cancel oscillation;
- ETA reaches/approaches zero without observed pass;
- predicted CPA versus later observed CPA deviates materially;
- HIGH confidence immediately before a large correction;
- significant provider/freshness instability around the decision;
- route/destination veto timing anomaly;
- repeated same failure pattern;
- alert lifecycle sequence violates expected state rules.

Thresholds must come from the actual current project configuration/testing.
Do not invent permanent numbers in this planning file.

=========================================================================
14. EVIDENCE PACKETS
=========================================================================

AI inputs should be compact, structured and reproducible.

A packet should contain only the evidence needed for the case, for example:

- event/case ID;
- ICAO/callsign when needed;
- timestamps;
- prediction version;
- CPA before/after;
- ETA/TTC before/after;
- alert state before/after;
- route-veto state;
- destination resolution state;
- heading/speed/vertical-rate deltas;
- sample freshness/age;
- provider changes;
- rejected/stale sample summary;
- relevant trajectory samples;
- subsequent observed closest pass when coverage supports it;
- coverage-quality classification;
- exact source event IDs.

Do not send:
- full environment;
- secrets;
- admin password;
- API tokens;
- Mongo URI;
- Railway token;
- Telegram token;
- unnecessary private user coordinates.

=========================================================================
15. BATCHING AND QUOTA CONSERVATION
=========================================================================

Group related events into encounters before AI.

Bad:
28 events -> 28 calls.

Better:
28 events -> 8 encounters -> one or a few compact batches.

If an hourly window contains no deterministic anomaly:
- perform zero AI calls.

Use bounded batch sizes based on:
- model context;
- output size;
- latency;
- schema reliability.

Track usage by provider/model/role.

Maintain reserve capacity logically:
- routine;
- review;
- deep investigation;
- Supervisor/emergency.

IMPORTANT:
"Reserve key" is a real quota/routing concept for this deployment.

The configured Gemini, Groq and Mistral keys have independent quota pools.
A reserve slot may therefore be held back for fallback, high-priority investigation or Supervisor emergency use and consumed only when policy allows.

=========================================================================
16. AI OUTPUT CONTRACT
=========================================================================

All providers must map into one internal schema.

Example fields:

schema_version
task_id
case_id
classification
confidence
evidence_refs[]
reason_summary
needs_independent_review
needs_deep_investigation
recommended_next_step
needs_more_data
coverage_assessment
model/provider metadata

Possible classification examples:

NORMAL_EXPECTED_CHANGE
EXPECTED_TURN
STALE_INPUT
PROVIDER_ISSUE
COVERAGE_INCONCLUSIVE
POSSIBLE_ESTIMATOR_ERROR
ALERT_LIFECYCLE_ERROR
ROUTE_GUARD_ANOMALY
STORAGE_EVIDENCE_LOSS
INSUFFICIENT_EVIDENCE
INVESTIGATE

AI output is accepted only after:

1. schema validation;
2. referenced event IDs exist;
3. numerical references are consistent with supplied evidence;
4. required evidence fields exist;
5. no invented provider/tool results are present;
6. no secret-like content leaked.

If validation fails:
- repair once when appropriate;
- otherwise alternate provider.

=========================================================================
17. INDEPENDENT REVIEW LOGIC
=========================================================================

First reviewer:
sees evidence packet.

Second reviewer:
sees the same source evidence but normally does not see the first reviewer's verdict initially.

After both results are stored:

AGREE + low risk
-> store finding/disposition.

AGREE + high impact
-> deep investigation.

DISAGREE
-> third opinion or deterministic replay/tool evidence.

INCONCLUSIVE
-> preserve as inconclusive, do not force a bug label.

The goal is evidence quality, not majority voting.

Tests/replays and subsequent physical evidence outweigh model agreement.

=========================================================================
18. SANDBOX / TOOL GATEWAY
=========================================================================

AI models never receive unrestricted shell credentials.

Models request tools through a controlled gateway.

Initial safe/read-only tools:

- list_tasks;
- read_task;
- read_finding;
- list_provider_health;
- read_provider_health;
- read_event_timeline;
- read_prediction_evidence;
- search_repository;
- read_repository_file;
- inspect_git_commit;
- inspect_git_diff;
- run_existing_replay;
- run_targeted_tests;
- compare_reviews;
- request_independent_review.

Later engineering tools:

- create_temporary_worktree;
- create_engineering_branch;
- write_candidate_test;
- write_candidate_patch;
- run_candidate_test;
- run_regression_suite;
- create_structured_handoff/release_candidate.

Initially prohibited without explicit approval and separate release design:

- direct push to main;
- merge;
- production deployment;
- changing Railway variables;
- changing GitHub secrets;
- deleting Prediction Lab history;
- database-wide destructive writes;
- changing alert-critical runtime configuration.

Every tool call records:
- operation ID;
- requested action;
- validated arguments;
- start/end;
- exit status;
- stdout/stderr summary;
- output artifact hash;
- redaction result.

Do not store model private chain-of-thought.
Store useful inspectable artifacts:
- assigned task;
- evidence;
- tool calls;
- tool results;
- explicit concise rationale/decision summary;
- final structured conclusion.

=========================================================================
19. SUPERVISOR CHAT
=========================================================================

The Supervisor is not "just another worker".

Its roles:

- answer what is currently happening;
- summarize recent audits;
- explain a finding using actual stored evidence;
- show which models reviewed a case;
- compare disagreements;
- show provider health;
- start safe investigations;
- request another provider's opinion;
- run safe replay/tests;
- pause/cancel/reprioritize AI Ops work;
- create candidate investigation tasks;
- later request approval for higher-risk operations.

Examples:

"What happened this hour?"
"Why was finding 184 flagged?"
"Did Mistral agree with Groq?"
"Show the replay result."
"Ask another AI."
"Was this finding useful?"
"Which providers are rate-limited?"
"Stop that investigation."
"Continue finding 184."
"Run the existing regression test."
"Show what changed in the candidate patch."

The Supervisor must NOT answer operational-state questions from vague model memory.

It queries structured backend tools each time.

=========================================================================
20. SUPERVISOR SESSION CONTINUITY
=========================================================================

Each chat session stores a continuity packet:

conversation_id
session_provider
session_model
current_topic
referenced_task_ids
referenced_finding_ids
verified_facts
completed_commands
pending_action
last_backend_snapshot_version

If Supervisor provider fails:

1. persist current accepted state;
2. choose next approved Supervisor provider;
3. provide recent conversation context;
4. provide continuity packet;
5. re-query current backend state;
6. resume.

Do not transfer hidden chain-of-thought.
Transfer verified facts and explicit tool artifacts.

The GUI must show a provider switch rather than hiding it.

Example:

Supervisor provider changed
Reason: rate limit
Conversation restored: yes
Referenced tasks restored: 3
Pending action restored: none

=========================================================================
21. SUPERVISOR ACTION PERMISSION LEVELS
=========================================================================

LEVEL A — READ ONLY
May run automatically:
- read status;
- read findings;
- read logs;
- inspect code;
- inspect diffs;
- inspect provider health;
- run safe queries.

LEVEL B — SAFE COMPUTE
May run automatically:
- replay;
- tests;
- benchmarks;
- independent AI review;
- temporary isolated analysis.

LEVEL C — DEVELOPMENT MUTATION
Initially require explicit policy/approval:
- create branch;
- write candidate test;
- write candidate patch;
- create PR.

LEVEL D — PRODUCTION MUTATION
Require explicit owner approval unless a future standing rule explicitly changes this:
- merge;
- deploy;
- environment/config mutation;
- DB mutation;
- secret changes.

Never let a free LLM directly decide that a production deployment should happen.

=========================================================================
22. PRIVATE OPERATIONS GUI
=========================================================================

The GUI is a serious product-design task.

Visual direction:
- professional;
- restrained;
- aviation/operations focused;
- readable;
- compact but not crowded;
- no random neon;
- no gratuitous gradients;
- no giant rounded cards everywhere;
- no fake "AI dashboard" aesthetic;
- no unnecessary animation.

Prototype before production deployment.

-----------------------------------------------------------------------
22.1 OVERVIEW
-----------------------------------------------------------------------

Show:

- Plane Alerts production health;
- live monitor cadence summary;
- AI Ops scheduler health;
- queue depth;
- active AI jobs;
- findings this hour/day;
- reviews pending;
- deep investigations;
- provider health;
- GitHub sync status;
- last successful backup;
- last restore drill;
- recent important activity;
- Supervisor availability.

-----------------------------------------------------------------------
22.2 LIVE AGENTS
-----------------------------------------------------------------------

For every active logical worker:

- worker ID;
- role;
- task;
- provider;
- model;
- credential slot number only;
- status;
- started time;
- current step;
- elapsed time;
- bounded token/usage metadata;
- last tool call;
- failure/failover state.

Never display secret values.

-----------------------------------------------------------------------
22.3 TASK DETAIL
-----------------------------------------------------------------------

Timeline:

- task created;
- deterministic evidence created;
- AI attempt started;
- provider/model;
- AI result;
- schema validation;
- independent review;
- tool calls;
- replay;
- tests;
- provider failover;
- checkpoints;
- final disposition;
- handoff/release candidate.

-----------------------------------------------------------------------
22.4 FINDINGS
-----------------------------------------------------------------------

Show:

- finding ID;
- severity;
- status;
- classification;
- confidence;
- evidence;
- reviewers;
- agreement/disagreement;
- replay result;
- candidate fix/test;
- Error Museum link/reference when applicable;
- eventual production verification when available.

-----------------------------------------------------------------------
22.5 PROVIDERS
-----------------------------------------------------------------------

Per provider:

- health;
- enabled/disabled;
- configured credential slots;
- slot health;
- current model;
- rate-limit/cooldown state;
- usage;
- latency;
- parse/error rate;
- last successful request.

For Cloudflare display logical Slot 1 and Slot 2, each bound to its own Account ID/token pair without revealing them.

-----------------------------------------------------------------------
22.6 SUPERVISOR CHAT
-----------------------------------------------------------------------

Display:

- current Supervisor provider/model;
- session health;
- provider-switch history;
- linked tasks/findings;
- chat;
- tool activity;
- pending approvals;
- ability to open referenced finding/task directly.

-----------------------------------------------------------------------
22.7 GITHUB SYNC / DISASTER RECOVERY
-----------------------------------------------------------------------

Show:

- sync enabled;
- destination repo identifier without credentials;
- last local checkpoint;
- last successful GitHub sync;
- current backup manifest version;
- latest verified hash;
- sync lag;
- failed sync reason;
- latest restore verification result.

-----------------------------------------------------------------------
22.8 FEEDBACK / QUALITY
-----------------------------------------------------------------------

Every finding/review can be marked:

USEFUL
NOT USEFUL
FALSE POSITIVE
NEEDS MORE INVESTIGATION
ALREADY KNOWN
CORRECTLY IDENTIFIED PROBLEM
INSUFFICIENT EVIDENCE
WRONG CONCLUSION

Store this feedback for model/provider quality statistics.

=========================================================================
23. MODEL/PROVIDER QUALITY METRICS
=========================================================================

Track real Plane Alerts usefulness:

- useful finding rate;
- false-positive rate;
- schema failure rate;
- evidence citation accuracy;
- replay request usefulness;
- test generation success;
- tool-call success;
- latency;
- timeout rate;
- provider failover rate;
- average calls per resolved finding;
- user feedback;
- repeated duplicate findings.

Do not use this data to let AI take over deterministic prediction.

Use it only to:
- improve routing;
- choose better reviewer/provider;
- identify weak models;
- reduce wasted calls;
- improve Supervisor selection.

=========================================================================
24. PERSISTENCE — SURVIVING RAILWAY DEPLOYS
=========================================================================

AI Operations state must NEVER depend on Railway ephemeral filesystem storage.

Preferred owner-hosted architecture:

- use the existing Plane Alerts persistent Railway volume if current production confirms it is appropriate;
- create a dedicated AI Ops subtree;
- use SQLite in WAL mode or another proven bounded local durable store for orchestration state;
- keep high-volume AI Ops operational history out of MongoDB unless there is a verified reason to use Mongo;
- do not worsen existing Mongo quota/storage pressure.

Example logical path only:
<current Plane Alerts persistent mount>/runtime/ai_ops/

Actual mount/path MUST be discovered from current production before implementation.

Suggested files:

ai_ops.sqlite
artifacts/
temporary/
sync/
backups/

Bound:
- artifacts;
- chat history retention if necessary;
- raw model responses;
- temporary worktrees;
- debug logs.

Preserve durable findings and task summaries longer than raw execution noise.

=========================================================================
25. DEPLOY/RESTART RECOVERY
=========================================================================

On AI Ops service startup:

1. verify persistent data directory;
2. validate DB schema/version;
3. run safe migration if required;
4. acquire scheduler leader lease;
5. inspect RUNNING/RETRYING jobs;
6. detect expired worker leases;
7. return interrupted jobs to resumable state;
8. rebuild provider health from durable state plus lightweight probes;
9. restart hourly scheduler;
10. resume backlog by priority;
11. start API/GUI;
12. start GitHub sync worker;
13. publish health/readiness only after durable state is usable.

On graceful shutdown/deploy:

1. stop claiming new tasks;
2. allow bounded current operations to finish or checkpoint;
3. mark in-flight steps resumable;
4. flush DB/WAL;
5. trigger a final bounded GitHub sync if possible;
6. trigger/update Dropbox backup checkpoint if this deployment event materially changed durable state and time budget permits;
7. release leases;
8. exit.

A deploy must not lose the task queue.

A provider request that is cut off during deploy may restart only the current incomplete idempotent unit.

=========================================================================
26. SEPARATE SERVICE VS LIVE MONITOR
=========================================================================

Preferred deployment:
a separate private AI Ops service/process using the same repository/release commit but isolated from the five-second Plane Alerts monitoring process.

Benefits:
- AI timeouts do not block flight monitoring;
- independent restart policy;
- independent resource limits;
- clearer health;
- safer future migration.

If current Railway plan/resource constraints make a separate service impractical, the fallback is a separately supervised low-priority process within the current service, still using strict bounded concurrency.

Do not decide this from theory.
Inspect current Railway topology and resource budget in Phase 0.

=========================================================================
27. GITHUB DISASTER-RECOVERY SYNC
=========================================================================

Requirement:
If Railway disappears or the owner later moves hosts, the private AI Operations state must be recoverable without depending on a Railway volume being accessible forever.

IMPORTANT:
GitHub must NOT become the live database.
GitHub is the disaster-recovery/export layer.

-----------------------------------------------------------------------
27.1 DESTINATION
-----------------------------------------------------------------------

Preferred:
a DEDICATED PRIVATE GitHub repository for Plane Alerts private operational state.

Do not store private AI Ops data in a public/community repository branch.

If the current main Plane Alerts repository is public or may become public, a separate private backup repository is mandatory.

Repository name should be chosen during implementation.
Example only:
Plane-Alerts-Private-State

Do not assume this exact name exists.

-----------------------------------------------------------------------
27.2 WHAT IS SYNCED
-----------------------------------------------------------------------

Sync durable portable state such as:

- schema version;
- job summaries;
- step/checkpoint state;
- finding records;
- review records;
- evidence references;
- Error Museum/release-candidate references;
- provider health history without credentials;
- usage summaries;
- tool-operation summaries;
- Supervisor continuity summaries;
- user usefulness feedback;
- configuration manifest containing secret NAMES/PRESENCE only;
- restore metadata;
- source commit/version references;
- database snapshot or export in encrypted form if used.

Do not sync:
- API-key values;
- Authorization headers;
- ADMIN_PASSWORD value;
- MONGO_URI value;
- RAILWAY_API_TOKEN value;
- TELEGRAM_BOT_TOKEN value;
- exact private coordinates unless separately encrypted and truly necessary;
- unredacted raw logs.

-----------------------------------------------------------------------
27.3 TWO-TIER BACKUP
-----------------------------------------------------------------------

Tier 1 — readable/sanitized portable state

Purpose:
Human-inspectable disaster recovery and engineering history.

Contains:
- tasks;
- findings;
- reviews;
- hashes;
- version metadata;
- sync manifest;
- safe chat continuity summaries.

Tier 2 — encrypted full AI Ops recovery snapshot

Purpose:
Restore the durable AI Ops DB/history without exposing private conversation/tool data.

Use authenticated encryption with a versioned format.

A future implementation must introduce an owner-only backup encryption key, for example:

AI_OPS_BACKUP_ENCRYPTION_KEY

This is a NEW secret and does not currently appear in the supplied list.

Do not add it until the phase implementing encrypted backup.

The owner must keep an independent recovery copy of this key outside Railway.
Storing it only in the same infrastructure being backed up defeats full disaster recovery.

-----------------------------------------------------------------------
27.4 GITHUB SECRETS ARE NOT A READABLE SECRET BACKUP
-----------------------------------------------------------------------

GitHub Secrets should be treated as deployment secret storage, not as a human-readable backup of secret values.

The DR manifest should record required secret names and whether they are configured, never their values.

For migration to another host, secrets can be:
- re-entered by the owner;
- injected by a GitHub Actions deployment workflow when safe;
- restored from the owner's separate secure secret backup.

Never commit secret values simply to make migration easier.

-----------------------------------------------------------------------
27.5 SYNC FREQUENCY
-----------------------------------------------------------------------

Use a debounced sync queue.

Suggested target:
- trigger after material state changes;
- coalesce frequent changes;
- sync within a few minutes under normal conditions;
- immediate priority sync after completed high-value finding/release candidate;
- daily integrity snapshot.

Do not commit every token or every log line.

-----------------------------------------------------------------------
27.6 SYNC VERIFICATION
-----------------------------------------------------------------------

A sync is successful only after:

1. local export completed;
2. secret/privacy scanner passed;
3. checksums/hashes created;
4. GitHub push/upload succeeded;
5. remote commit/blob/asset is fetched or otherwise verified;
6. hash matches;
7. durable sync checkpoint advances.

If remote verification fails:
- keep local state;
- retry later;
- do not falsely mark synchronized.

-----------------------------------------------------------------------
27.7 RESTORE
-----------------------------------------------------------------------

Restore procedure:

1. fresh checkout of correct Plane Alerts source;
2. provision persistent storage;
3. configure required secrets separately;
4. retrieve latest verified AI Ops backup;
5. verify signature/hash/encryption version;
6. decrypt snapshot;
7. validate schema;
8. import into temporary restore path;
9. run DB integrity checks;
10. verify task/findings counts/invariants;
11. atomically promote restored DB;
12. start AI Ops in READ-ONLY recovery mode;
13. verify GUI/task history;
14. then enable scheduler/workers.

Never overwrite a healthy local database merely because GitHub contains a snapshot.

-----------------------------------------------------------------------
27.8 RESTORE DRILLS
-----------------------------------------------------------------------

A backup is not trustworthy until restore is tested.

Add periodic non-production restore verification:
- download latest backup;
- restore into temporary directory;
- run integrity checks;
- verify representative jobs/findings;
- never call AI providers during a restore drill unless explicitly required;
- report success/failure in GUI.


=========================================================================
27A. DROPBOX SECONDARY AUTOMATIC BACKUP / INDEPENDENT DR
=========================================================================

Purpose:
Do not rely on GitHub as the only off-Railway recovery location.

Dropbox becomes the SECOND independent backup destination for private AI Operations state.

GitHub and Dropbox serve different recovery purposes:

GITHUB
- versioned, reviewable, sanitized operational state;
- manifests;
- compact findings/reviews;
- phase/status metadata;
- source/release references;
- disaster-recovery history.

DROPBOX
- encrypted full AI Ops recovery snapshots;
- periodic exported SQLite/DB snapshots;
- encrypted chat/session continuity backup if enabled;
- restore bundles;
- backup manifests/checksums;
- optional longer-term rotated snapshots.

The live database remains the persistent Plane Alerts AI Ops store.
Neither GitHub nor Dropbox becomes the live database.

-----------------------------------------------------------------------
27A.1 AUTOMATIC BACKEND ACCESS IS NOT THE CHATGPT DROPBOX PLUGIN
-----------------------------------------------------------------------

The Plane Alerts backend must use a real Dropbox API application for unattended backups.

Do NOT assume the ChatGPT @Dropbox plugin authorization can be reused by Railway/backend code.

The ChatGPT Dropbox plugin is useful for:
- finding backup files from a future ChatGPT session;
- inspecting a backup manifest;
- checking whether a snapshot exists;
- reading safe exported text;
- assisting the owner during recovery.

The automatic production backup path must instead use Dropbox OAuth with offline/background access.

Dropbox currently uses short-lived access tokens and supports long-lived refresh tokens for applications that must operate while the user is not present.

At implementation time, create/configure a dedicated Dropbox app with the minimum required scopes.

Expected future private GitHub/Railway secrets may include names equivalent to:

DROPBOX_APP_KEY
DROPBOX_APP_SECRET
DROPBOX_REFRESH_TOKEN

Do not add these secrets until the Dropbox-backup implementation phase.

Never commit their values.
Never display them in the GUI.
Never send them to an LLM.

Use the official Dropbox SDK where practical so short-lived access-token refresh behavior is handled correctly.

-----------------------------------------------------------------------
27A.2 BACKUP ROOT
-----------------------------------------------------------------------

Use a dedicated owner-controlled Dropbox folder.

Example only:

/Plane Alerts/Private AI Ops Backups/

Actual path is chosen/configured during implementation.

Suggested structure:

/Plane Alerts/Private AI Ops Backups/
  manifests/
  snapshots/
    hourly/
    daily/
    weekly/
  restore-tests/
  migration-bundles/

Do not mix these backups with normal user-uploaded Plane Alerts assets.

-----------------------------------------------------------------------
27A.3 BACKUP CONTENT
-----------------------------------------------------------------------

Dropbox snapshots may contain more complete recovery state than the sanitized GitHub export, but sensitive state must be encrypted before upload.

Allowed after encryption where required:
- ai_ops.sqlite snapshot;
- job/task history;
- review/finding history;
- Supervisor conversation state;
- continuity packets;
- safe tool-operation records;
- provider usage/health metadata;
- configuration manifest;
- phase implementation state;
- restore metadata.

Never upload plaintext:
- API keys;
- refresh tokens;
- ADMIN_PASSWORD;
- MONGO_URI;
- Telegram token;
- Railway token;
- Authorization headers;
- exact private coordinates unless explicitly required and encrypted.

-----------------------------------------------------------------------
27A.4 ROTATION / RETENTION
-----------------------------------------------------------------------

Dropbox must not grow forever.

Use configurable retention.

Recommended initial policy to validate during implementation:

- recent hourly snapshots: short retention;
- daily snapshots: medium retention;
- weekly snapshots: longer retention;
- milestone snapshots: retained for important releases/migrations.

Exact retention counts/days must be configurable and confirmed against actual Dropbox storage capacity.

Deletion/rotation occurs only after:
1. a newer valid backup exists;
2. its hash verifies;
3. remote upload is confirmed;
4. restore metadata is durable;
5. the snapshot is outside the configured retention window.

-----------------------------------------------------------------------
27A.5 DUAL-DESTINATION BACKUP SUCCESS
-----------------------------------------------------------------------

Do not treat GitHub and Dropbox as one coupled transaction.

Track them independently:

github_sync_status
dropbox_backup_status

A GitHub failure must not block local AI Ops.
A Dropbox failure must not block local AI Ops.
One successful destination does not pretend the other succeeded.

GUI should show:

GitHub: synced / delayed / failed
Dropbox: backed up / delayed / failed
Last verified GitHub state
Last verified Dropbox snapshot
Latest restore drill

-----------------------------------------------------------------------
27A.6 RESTORE PRIORITY
-----------------------------------------------------------------------

During disaster recovery:

1. restore source/configuration metadata from GitHub;
2. retrieve the newest verified encrypted full snapshot from Dropbox when available;
3. verify checksum/hash;
4. decrypt locally with the separately stored backup encryption key;
5. restore into a temporary path;
6. validate database/schema;
7. compare against GitHub manifest/checkpoints;
8. promote only after integrity checks.

If Dropbox is unavailable:
- GitHub sanitized state remains a recovery source.

If GitHub is unavailable:
- Dropbox full snapshot remains a recovery source.

The objective is independent failure domains.

-----------------------------------------------------------------------
27A.7 CHATGPT + DROPBOX RECOVERY WORKFLOW
-----------------------------------------------------------------------

Because the owner has the Dropbox ChatGPT plugin connected, a future ChatGPT session may use it to locate and inspect backup files.

A future session may be told:

"Read the Plane Alerts roadmap and standing instructions. Search my Dropbox for the latest Plane Alerts Private AI Ops backup manifest/snapshot metadata. Do not restore or overwrite anything until you verify repository state, backup hashes and the requested recovery target."

The plugin is an interactive recovery aid, not the production backup engine.

=========================================================================
=========================================================================
28. INTERACTION WITH EXISTING PREDICTION LAB GITHUB DATA
=========================================================================

The current Plane Alerts project already has durable Prediction Lab repository evidence in its existing workflow.

Do not duplicate or destroy it.

Target separation:

Prediction Lab durable evidence:
- continue using the current established data branch/lifecycle after inspecting repository reality.

Private AI Ops state:
- private DR repository/export.

Cross-reference by stable case/finding IDs.

If an AI Ops investigation produces a verified Prediction Lab case:
- use the existing current case lifecycle helper/format;
- preserve evidence;
- never delete source history because a fix was merged;
- only mark solved according to the current release/production-verification rules.

=========================================================================
29. EXISTING SCHEDULED TASK COEXISTENCE
=========================================================================

Before enabling any new backend scheduler in production, inspect active non-paused tasks.

At planning time the following was observed:

ACTIVE:
- Prediction Lab Release — hourly;
- Weekly Community Release — weekly;
- Plane Spotting Slice — weekly planning task.

PAUSED/DISABLED:
- Prediction Lab Investigator;
- Plane Alerts Prediction Lab Collector;
- older Plane Prediction Auditor;
- retired external-agent tasks.

Do not assume this list remains current during implementation.

Migration rule:

1. Phase 0 records current schedules.
2. Early AI Ops phases run SHADOW/READ-ONLY.
3. Do not re-enable old paused tasks merely because similar behavior is being built.
4. Do not duplicate an active task's production side effects.
5. New AI Ops may eventually replace Collector/Investigator-style responsibilities only after parity and reliability are proven.
6. The existing Prediction Lab Release task remains separate until an explicit later handover.
7. Weekly Community Release remains community-release owner and must not be swallowed by private AI Ops.
8. Any schedule retirement requires explicit documented transition and rollback.

=========================================================================
30. PRIVATE GUI AUTHENTICATION & SECURITY
=========================================================================

Initial private GUI can use existing:

ADMIN_PASSWORD

But implementation must still use proper server-side security:

- TLS only;
- secure HttpOnly session cookie;
- SameSite protection;
- CSRF protection for state-changing requests;
- rate limiting;
- constant-time secret comparison or appropriate password verification;
- login failure throttling;
- session expiry;
- explicit logout;
- no password in browser logs/localStorage;
- no secrets returned by API.

Future hardening may add:
- Cloudflare Access;
- passkey/TOTP;
- IP/device restrictions.

Do not require those in the first GUI release unless needed.

=========================================================================
31. EVENT MODEL
=========================================================================

Everything important emits a structured event.

Examples:

AUDIT_WINDOW_CREATED
EVIDENCE_PACKET_CREATED
TASK_CREATED
TASK_LEASED
TASK_STARTED
STEP_STARTED
STEP_COMPLETED
AI_ATTEMPT_STARTED
AI_ATTEMPT_FAILED
AI_RESULT_VALIDATED
PROVIDER_FAILOVER
PROVIDER_COOLDOWN
TOOL_STARTED
TOOL_COMPLETED
REPLAY_STARTED
REPLAY_COMPLETED
TEST_STARTED
TEST_COMPLETED
REVIEW_CREATED
FINDING_CREATED
FINDING_DISPOSITION_CHANGED
HANDOFF_CREATED
GITHUB_SYNC_STARTED
GITHUB_SYNC_SUCCEEDED
GITHUB_SYNC_FAILED
SUPERVISOR_PROVIDER_CHANGED
TASK_COMPLETED

Events feed:
- database;
- GUI live stream;
- audit history;
- Supervisor backend context;
- GitHub export.

Use bounded retention for noisy low-value events.

=========================================================================
32. LIVE GUI TRANSPORT
=========================================================================

Use WebSocket or Server-Sent Events for live operational updates.

Requirements:
- reconnect automatically;
- resume from last event cursor where practical;
- do not require full-page reload;
- bounded event buffer;
- auth required;
- no secrets in event payloads;
- server remains source of truth.

The frontend must tolerate disconnects and show STALE state when live connection is lost.

=========================================================================
33. "WAS THIS USEFUL?" FEEDBACK LOOP
=========================================================================

Every meaningful AI finding should support owner feedback.

Store:
- finding ID;
- user rating;
- category;
- optional short note;
- eventual evidence outcome when known.

This feedback may influence:
- provider preference;
- model preference;
- threshold for independent review;
- prompt/schema improvements.

It must NOT influence physical trajectory calculations.


=========================================================================
33A. FINDING LIFECYCLE, CLEARING & RETENTION
=========================================================================

The owner does NOT want corrected findings to pile up forever in the active queue/UI.

At the same time, Plane Alerts must preserve durable regression evidence.

Therefore "CLEAR" means:

REMOVE FROM ACTIVE WORKING STATE
NOT
DESTROY HISTORICAL EVIDENCE.

-----------------------------------------------------------------------
33A.1 FINDING STATES
-----------------------------------------------------------------------

Recommended lifecycle:

OPEN
-> TRIAGED
-> REVIEWING
-> INVESTIGATING
-> FIX_CANDIDATE
-> DEPLOYED_PENDING_VERIFICATION
-> RESOLVED

Other terminal outcomes:

EXPECTED_BEHAVIOR
INCONCLUSIVE
DUPLICATE
ALREADY_FIXED
FALSE_POSITIVE
WONT_FIX_WITH_REASON

-----------------------------------------------------------------------
33A.2 WHEN A CORRECTED FINDING IS CLEARED
-----------------------------------------------------------------------

A code-defect finding may be cleared from the active queue only after:

1. exact fix/candidate is identified;
2. relevant replay/regression tests pass;
3. required CI passes;
4. exact tested commit is deployed when deployment is required;
5. production verification passes;
6. the exact finding/case is linked to that deployed fix;
7. durable historical evidence is safely preserved.

Then:

- mark finding RESOLVED;
- remove it immediately from default Active views/queues;
- cancel any unnecessary scheduled retry;
- release worker leases;
- stop sending it to AI reviewers;
- preserve one compact terminal record;
- preserve canonical Prediction Lab solved/Error Museum evidence according to the current project lifecycle.

Do NOT keep resolved findings circulating through hourly audits as new work.

-----------------------------------------------------------------------
33A.3 ACTIVE QUEUE VS HISTORY
-----------------------------------------------------------------------

GUI default:
ACTIVE only.

Separate history views:
- Resolved;
- Expected behavior;
- Inconclusive;
- Duplicate;
- False positive;
- Archived.

The owner should not need to scroll through thousands of solved cases to see current work.

-----------------------------------------------------------------------
33A.4 BULKY ARTIFACT COMPACTION
-----------------------------------------------------------------------

After terminal disposition and successful backup/synchronization:

Eligible for bounded cleanup:
- duplicate transient model responses;
- retry payloads;
- temporary prompt packages;
- temporary worktrees;
- redundant command stdout/stderr;
- cached raw copies already represented in canonical evidence.

Preserve:
- final finding;
- key evidence references;
- reviewer conclusions;
- replay/test result summary;
- fix commit/version;
- production verification result;
- Error Museum reference;
- Prediction Lab solved case/reference;
- user feedback;
- audit timestamps.

A periodic compactor/retention job should remove eligible bulky operational copies.

It must never delete canonical Prediction Lab history merely to save space.

-----------------------------------------------------------------------
33A.5 REAPPEARING RESOLVED FINDINGS
-----------------------------------------------------------------------

If the same historical signature appears again after a verified fix:

Do not silently reopen the old solved record.

Create a new regression occurrence linked to:
- previous solved finding;
- previous fix commit;
- Error Museum case;
- new evidence.

This makes regression recurrence measurable.

=========================================================================
33B. CHATGPT HANDOFF & NEW-SESSION REVIEW WORKFLOW
=========================================================================

The plan must work even when ChatGPT context windows end.

-----------------------------------------------------------------------
33B.1 BEFORE THE BUILT-IN SUPERVISOR CHAT EXISTS
-----------------------------------------------------------------------

Yes: to have ChatGPT independently review findings before Phase 9 Supervisor Chat is available, the owner may open a new ChatGPT session.

The owner should attach/provide:
- this roadmap;
- current Plane Alerts standing .txt instruction files through the Project;
- the latest current handoff/finding artifact when it is not already in the repository/project;
- any specific case ID the owner wants reviewed.

Suggested short prompt:

"Read all current Plane Alerts project .txt instructions and the Private AI Operations roadmap completely. Inspect the actual current GitHub/Railway/Prediction Lab state. Read the latest AI Ops/ChatGPT handoff and review every unresolved finding from where the previous session stopped. Do not redo completed work. Do not trust AI conclusions without verifying telemetry/source/tests."

The new session must determine current state from durable artifacts.
It must not rely on assumed chat memory.

-----------------------------------------------------------------------
33B.2 HANDOFF ARTIFACT
-----------------------------------------------------------------------

The backend should generate a durable machine-readable handoff whenever there is unresolved work suitable for ChatGPT review.

Preferred future name:

ai_ops_handoff.json

If the current repository already has a generic non-retired CHATGPT_HANDOFF_JSON contract, preserve compatibility or migrate it deliberately.

Do NOT restore an AGY-specific handoff format merely because an old file or prompt used CHATGPT_HANDOFF_JSON.

The handoff should contain:

schema_version
generated_at
source_commit
deployed_version
ai_ops_schema_version
current_phase
unresolved_findings[]
active_tasks[]
blocked_tasks[]
provider_health_summary
latest_sync_status
required_human_decisions[]
recommended_review_order[]
evidence_refs[]
replay_refs[]
test_refs[]
candidate_patch_refs[]
last_completed_checkpoint

It must NOT contain secrets.

-----------------------------------------------------------------------
33B.3 HANDOFF STATUS RULE
-----------------------------------------------------------------------

The handoff is a navigation/index artifact, not the source of truth.

A new ChatGPT session must still verify:
- current main;
- current Railway deploy;
- current task database;
- current Prediction Lab state;
- latest logs/telemetry where needed.

Do not blindly act on a stale handoff.

-----------------------------------------------------------------------
33B.4 HANDOFF UPDATE/CLEARING
-----------------------------------------------------------------------

When a finding becomes terminal:

- remove it from unresolved_findings;
- add a compact reference to recent_resolved if useful;
- do not leave solved findings in the active handoff forever.

Bound recent_resolved to a small configurable number/window.

When there are no unresolved findings:
- handoff explicitly says unresolved_findings = [];
- do not keep old unresolved cases merely for history.

Historical truth lives in solved/archived durable records, not the active handoff.

-----------------------------------------------------------------------
33B.5 AFTER SUPERVISOR CHAT EXISTS
-----------------------------------------------------------------------

After Phase 9 is production-verified, opening a separate ChatGPT session is no longer required for normal review.

The normal workflow becomes:

Plane Alerts GUI
-> Supervisor Chat
-> "What findings need review?"
-> inspect evidence
-> ask another AI/replay/test as needed
-> mark useful/not useful
-> approve further investigation where applicable.

A separate ChatGPT session remains useful for:
- independent second opinion;
- major architecture review;
- disaster recovery;
- work that benefits from project-connected GitHub/Railway/Dropbox tools.

-----------------------------------------------------------------------
33B.6 IMPLEMENTATION STATE FILE
-----------------------------------------------------------------------

To eliminate dependency on chat context, maintain a non-secret repo-tracked implementation-state file.

Suggested path:

docs/private-ai-ops/implementation-state.json

Example fields:

roadmap_version
last_completed_phase
current_phase
phase_status
last_verified_commit
last_verified_railway_deployment
completed_releases[]
open_blockers[]
last_handoff_id
last_successful_github_sync
last_successful_dropbox_backup

This file is status metadata only.

Do not put:
- secrets;
- raw chat;
- private coordinates;
- API usage tokens.

A future ChatGPT session reads:
1. project standing instructions;
2. this roadmap;
3. implementation-state.json;
4. latest handoff;
5. actual GitHub/Railway state;

and continues from the first unfinished phase.

=========================================================================
=========================================================================
34. PHASED RELEASE PLAN
=========================================================================

Each phase below is a separate release boundary.

Before each:
- inspect real current version/main/Railway;
- read standing .txt instructions;
- inspect active scheduled tasks;
- inspect current Prediction Lab lifecycle;
- preserve useful unfinished work;
- use a normal engineering branch;
- run only Railway-relevant validation for owner-private Railway releases;
- record community impact honestly;
- do not run the full community matrix unless the current weekly community workflow owns it.

Do NOT pre-assign fixed version numbers in this file.

-----------------------------------------------------------------------
PHASE 0 — BASELINE, INVENTORY & DESIGN LOCK
-----------------------------------------------------------------------

Goal:
Establish the exact current state before coding.

Work:
- inspect repository;
- inspect Railway services/processes/volumes;
- identify current persistent volume mount;
- inspect Prediction Lab storage and prediction-lab-data sync;
- inspect current database backends;
- inspect actual config parsers;
- inspect all current provider secret usage;
- inspect active/paused scheduled tasks;
- inspect health endpoints;
- inspect existing web/admin UI;
- inspect current release workflows;
- inspect current provider models/capabilities;
- verify API credentials via safe minimum-cost/free health probes where allowed;
- identify whether each set of multiple keys shares account/project limits;
- inspect whether Cloudflare slots are in separate accounts or one account;
- benchmark candidate Supervisor models without making production changes;
- decide separate AI Ops service vs isolated process based on real Railway resource/topology;
- define data schema and retention targets;
- decide private GitHub backup repository location.

No functional production AI routing yet.

Tests:
- no secret leakage from inventory;
- no production mutation;
- API health probes bounded;
- no paid model call.

Done when:
A reviewed architecture decision record exists using actual production facts.

-----------------------------------------------------------------------
PHASE 1 — DURABLE AI OPS STORAGE & JOB ENGINE
-----------------------------------------------------------------------

Goal:
Create the state machine before connecting real AI.

Implement:
- AI Ops durable DB;
- migrations;
- job/step/attempt schema;
- leases;
- idempotency keys;
- restart recovery;
- scheduler state;
- health endpoint;
- graceful shutdown;
- bounded queue;
- fake provider for tests.

Use existing persistent volume after confirming safe path.

No live AI model calls required.

Tests:
- create job;
- restart service;
- resume;
- crash during step;
- lease expiry;
- duplicate claim prevention;
- migration;
- corrupted/incomplete step;
- bounded queue;
- no five-second monitor impact.

Done when:
Jobs reliably survive Railway process restarts/deploy simulation.

-----------------------------------------------------------------------
PHASE 2 — PRIVATE GITHUB DR EXPORT/SYNC FOUNDATION + DROPBOX DESIGN
-----------------------------------------------------------------------

Goal:
Ensure portability before accumulating important AI state and lock the second independent Dropbox backup design.

Implement:
- private GitHub backup destination;
- sanitized export format;
- manifest;
- hash/checksum;
- debounced sync queue;
- remote verification;
- sync checkpoint;
- restore into temporary path;
- restore integrity test;
- secret scanner.

Do NOT add encrypted full-chat/full-DB export yet unless Phase 2 remains safely small.
If encryption makes the phase too large, do sanitized sync first and full encrypted snapshot in Phase 3.

Tests:
- network outage;
- GitHub unavailable;
- conflicting sync attempt;
- secret-like value in export -> blocked;
- successful remote hash verification;
- restore from clean machine/container;
- local state continues if sync fails.

Also in this phase:
- define the dedicated Dropbox backup folder and app-scoped access model;
- define future Dropbox OAuth/offline-access secrets;
- verify the connected ChatGPT Dropbox plugin can locate/inspect owner files, but do not confuse plugin authorization with backend authorization;
- define independent GitHub and Dropbox backup status fields.

Done when:
A new host can reconstruct Phase-1 job state from verified GitHub state without Railway volume access, and the Dropbox secondary-backup implementation contract is documented and testable.

-----------------------------------------------------------------------
PHASE 3 — PROVIDER ADAPTERS & HEALTH ROUTER
-----------------------------------------------------------------------

Goal:
Correctly integrate providers without yet running the full hourly AI audit.

Implement separate adapters for:

Groq:
GROQ_KEY ... GROQ_KEY_5

Mistral:
MISTRAL_API ... MISTRAL_API_5

Gemini:
GEMINI_API_KEY ... GEMINI_API_KEY_5

Cloudflare Workers AI:
CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN
CLOUDFLARE_ACCOUNT_ID_2 + CLOUDFLARE_API_TOKEN_2

OpenRouter:
OPENROUTER_API

Cohere:
COHERE_TRIAL_KEY remains disabled/manual benchmark only.

Implement:
- normalized request/result interface;
- capability discovery/config;
- per-key health;
- provider health;
- rate-limit handling;
- Retry-After;
- cooldown;
- circuit breakers;
- usage ledger;
- free-route allow-list;
- paid usage hard-disable;
- safe health probes;
- schema test mode.

Do not let multiple keys become quota circumvention.

Tests:
- each adapter independently;
- wrong key;
- timeout;
- 429;
- provider-level 429 inference;
- 5xx;
- malformed result;
- key failover;
- cross-provider failover;
- paid OpenRouter model blocked;
- Cloudflare account/token pairing;
- Gemini key/auth behavior;
- no secret logging.

Done when:
The router can execute a synthetic structured task across provider failures with no lost state.

-----------------------------------------------------------------------
PHASE 4 — HOURLY DETERMINISTIC AUDIT COLLECTOR
-----------------------------------------------------------------------

Goal:
Build the non-AI evidence layer first.

Implement:
- hourly schedule;
- durable last-success checkpoint;
- read telemetry/events since prior checkpoint;
- deterministic filters;
- encounter grouping;
- evidence packet builder;
- coverage-quality rules;
- duplicate suppression;
- bounded backlog;
- shadow mode.

No AI conclusion controls production.

Tests:
- no events;
- normal event;
- trajectory changed;
- cancellation;
- qualify/cancel oscillation;
- stale data;
- missing coverage;
- provider change;
- restart mid-window;
- duplicate event;
- huge hour bounded;
- checkpoint advances only after durable evidence save.

Done when:
The system produces compact reproducible evidence packets without AI.

-----------------------------------------------------------------------
PHASE 5 — AI TRIAGE WITH MULTI-KEY/MULTI-PROVIDER RESUME
-----------------------------------------------------------------------

Goal:
Add routine AI classification safely.

Implement:
- batching;
- Groq-first triage;
- fallback router;
- validated common output schema;
- mid-task checkpoints;
- incomplete stream handling;
- provider/key failover;
- backlog;
- usage reserve;
- no-AI degraded mode.

Tests:
- G1 failure -> allowed healthy alternative;
- one key quota exhausted -> continue with another healthy key from the same provider;
- Groq provider outage -> Mistral/Gemini fallback;
- restart mid-batch;
- malformed JSON;
- all providers unavailable -> PENDING_AI;
- recovery processes backlog;
- completed batch not repeated.

Done when:
An hourly audit can finish despite injected provider/key failures without losing or duplicating evidence.

-----------------------------------------------------------------------
PHASE 6 — INDEPENDENT REVIEW, DEEP ESCALATION & QUALITY GATES
-----------------------------------------------------------------------

Goal:
Make AI findings more reliable than single-model opinions.

Implement:
- reviewer-provider separation;
- blind/independent second review;
- disagreement handling;
- third-review/deep escalation;
- strong-model role;
- evidence validation;
- finding lifecycle;
- usefulness feedback fields;
- model/provider quality metrics;
- terminal finding lifecycle;
- automatic removal of resolved/expected/duplicate findings from the active queue;
- compact historical retention;
- handoff refresh so resolved findings disappear from unresolved work.

Tests:
- agreeing reviewers;
- conflicting reviewers;
- insufficient evidence;
- model invents event ID -> reject;
- model misquotes numeric evidence -> reject;
- missing coverage -> inconclusive;
- repeated same finding deduplicated;
- resolved finding no longer appears in active queue;
- resolved finding remains available in solved/history evidence;
- handoff contains only unresolved work.

Done when:
No important finding is treated as confirmed merely because one AI said so, and terminal findings stop consuming active worker/UI capacity.

-----------------------------------------------------------------------
PHASE 7 — CONTROLLED TOOL/SANDBOX INVESTIGATOR
-----------------------------------------------------------------------

Goal:
Let AI do useful engineering work without unrestricted production access.

Implement:
- tool gateway;
- operation IDs;
- repository read/search;
- temporary worktree;
- exact commit checkout;
- replay runner;
- targeted test runner;
- stdout/stderr capture/redaction;
- candidate test/patch creation in isolated branch;
- strict allow-list;
- timeouts/resource limits.

No merge/deploy.

Tests:
- replay;
- test;
- failed command;
- tool timeout;
- repeated operation returns stored result;
- worker crashes after command;
- secret in stdout redacted;
- prohibited command rejected;
- branch name follows project rules.

Done when:
An investigation can reproduce a case and create a candidate patch/test without any path to production mutation.

-----------------------------------------------------------------------
PHASE 8 — PRIVATE READ-ONLY OPERATIONS GUI
-----------------------------------------------------------------------

Goal:
See the complete backend live.

Implement:
- admin authentication;
- Overview;
- Live Agents;
- Tasks;
- Findings;
- Providers;
- GitHub Sync/DR;
- live event stream;
- stale/reconnect/error states;
- mobile usability;
- no destructive controls yet.

Prototype first.
Do not ship a blind redesign.

Tests:
- auth;
- session expiry;
- no unauthenticated data;
- secret redaction;
- provider slot display;
- WebSocket/SSE reconnect;
- stale state;
- large event list;
- mobile layout;
- no exact private coordinates.

Done when:
The owner can understand what every worker is doing without reading Railway logs.

-----------------------------------------------------------------------
PHASE 9 — SUPERVISOR CHAT, READ-ONLY
-----------------------------------------------------------------------

Goal:
Provide a stable high-quality AI interface to the whole system.

Implement:
- benchmark winner;
- Cloudflare Supervisor reserve;
- session affinity;
- continuity packets;
- backend-grounded tools;
- task/finding links;
- provider-switch display;
- chat persistence;
- read-only questions;
- request independent review as a safe queued action if desired.

No direct development/production mutation from chat yet.

Tests:
- "what happened this hour";
- "show finding X";
- provider failover mid-session;
- Railway restart mid-chat;
- model switch continuity;
- no hallucinated task;
- unavailable evidence -> says unavailable;
- no secret leak.

Done when:
The user can reliably ask what the AIs did and get answers grounded in actual stored state.

-----------------------------------------------------------------------
PHASE 10 — SUPERVISOR SAFE ACTIONS
-----------------------------------------------------------------------

Goal:
Allow the owner to operate AI Ops through chat.

Add:
- start investigation;
- request reviewer;
- retry failed task;
- pause/resume AI Ops job;
- cancel AI Ops job;
- reprioritize queue;
- run replay;
- run targeted tests;
- create isolated candidate investigation.

Use permission levels and confirmation where appropriate.

Do NOT add merge/deploy yet.

Tests:
- idempotent action;
- stale chat command;
- duplicate click;
- user cancels;
- unauthorized request;
- Supervisor fails after requesting action;
- backend still owns state.

Done when:
Chat can safely control analysis work without controlling the live flight decisions or production deploy.

-----------------------------------------------------------------------
PHASE 11 — ENCRYPTED FULL RECOVERY SNAPSHOT, DROPBOX BACKUP & MIGRATION DRILL
-----------------------------------------------------------------------

Goal:
Make host migration realistic.

Implement:
- AI_OPS_BACKUP_ENCRYPTION_KEY or equivalent;
- encrypted durable DB snapshot;
- backup format version;
- GitHub private upload where appropriate;
- automatic Dropbox upload using backend OAuth/offline access;
- independent Dropbox upload verification;
- independent GitHub/Dropbox status and retry;
- retention;
- full clean-host restore drill;
- restore report;
- secret manifest;
- source commit/version pairing.

Tests:
- wrong key;
- corrupted snapshot;
- old schema;
- missing snapshot;
- GitHub unavailable;
- restore does not overwrite healthy DB;
- restored tasks/findings/chat consistent.

Done when:
A clean replacement host can reconstruct AI Ops state from GitHub and/or the latest verified Dropbox snapshot plus separately supplied secrets, and both recovery paths have been tested independently.

-----------------------------------------------------------------------
PHASE 12 — FEEDBACK-DRIVEN ROUTING & LONG-TERM QUALITY
-----------------------------------------------------------------------

Goal:
Use accumulated real-world outcomes to route work better.

Implement:
- useful/not-useful UI;
- false-positive outcome metrics;
- provider/model success statistics;
- latency/reliability statistics;
- router quality weights;
- duplicate-finding penalty;
- Supervisor benchmark refresh.

Do not let routing quality metrics alter deterministic prediction.

Tests:
- sparse feedback;
- contradictory feedback;
- new model with no history;
- provider temporarily degraded;
- routing never selects unapproved paid model.

Done when:
Routing uses real Plane Alerts usefulness rather than only speed/marketing benchmarks.

-----------------------------------------------------------------------
PHASE 13 — EXISTING AUTOMATION HANDOVER / CLEANUP
-----------------------------------------------------------------------

Goal:
Eliminate duplicate schedulers only after the private backend has proven itself.

Work:
- inspect current active/paused tasks again;
- compare new Collector/Triage/Investigator behavior with current scheduled tasks;
- identify exact overlap;
- run parallel shadow comparison for a defined period;
- verify no evidence loss;
- verify GitHub sync;
- verify task parity;
- document rollback;
- disable/retire only the duplicate schedule(s) explicitly approved by the owner/current standing policy.

Weekly Community Release remains separate.

Prediction Lab Release remains separate unless a later explicit plan safely migrates it.

Do not delete historical automation records merely because a new backend exists.

Done when:
There is one clear owner for each recurring responsibility and no duplicate production side effects.

-----------------------------------------------------------------------
PHASE 14 — OPTIONAL APPROVAL-BASED DEVELOPMENT/RELEASE CONTROLS
-----------------------------------------------------------------------

This phase is intentionally last and optional.

Only if the owner later wants it:

- Supervisor may prepare PR;
- Supervisor may request merge approval;
- Supervisor may request deployment approval;
- GUI shows exact diff/tests/commit before approval;
- production mutation remains behind explicit approval and existing release gates.

This must never bypass:
- GitHub CI;
- replay/Error Museum where applicable;
- exact tested commit rule;
- Railway production verification;
- rollback;
- deterministic alert-critical protections.

=========================================================================
35. TESTING MASTER MATRIX
=========================================================================

Every applicable phase must add tests for its own behavior.

At minimum across the full system:

PERSISTENCE
- restart;
- crash;
- deploy;
- DB migration;
- lease expiry;
- duplicate worker;
- interrupted write;
- recovery.

PROVIDER
- success;
- timeout;
- 401;
- 403;
- 429;
- 5xx;
- malformed JSON;
- context too large;
- provider unavailable;
- key unavailable;
- all unavailable;
- recovery after cooldown.

FAILOVER
- mid-task key failure;
- mid-task provider failure;
- completed-step preservation;
- incomplete-step retry;
- no duplicate tool side effects;
- session model switch.

SECURITY
- secret scanning;
- no Authorization headers;
- no API values in logs;
- no private coords;
- admin auth;
- CSRF;
- session expiry;
- unauthorized API;
- backup redaction/encryption.

GITHUB SYNC
- offline;
- remote mismatch;
- hash mismatch;
- concurrent sync;
- restore;
- corrupted snapshot;
- missing secret;
- clean-host migration.

GUI
- loading;
- empty;
- error;
- stale;
- success;
- reconnect;
- mobile;
- long-running task;
- provider switch;
- multiple agents.

SUPERVISOR
- grounded answer;
- missing evidence;
- tool error;
- provider failover;
- chat restore;
- safe action;
- prohibited action;
- hallucination resistance.

FLIGHT SAFETY
- prove AI Ops cannot modify alert-critical decision output;
- prove AI timeout does not slow five-second monitoring loop;
- prove AI Ops disabled = normal Plane Alerts remains fully functional.

=========================================================================
36. RELEASE AND DEPLOYMENT GATES
=========================================================================

For each private Railway release:

1. inspect repository state;
2. preserve unfinished work;
3. use normal engineering branch;
4. implement only current phase;
5. targeted tests;
6. relevant integration tests;
7. provider tests if provider path changed;
8. storage tests if storage changed;
9. security/redaction tests;
10. performance/cadence test;
11. required GitHub CI;
12. inspect individual jobs;
13. deploy exact tested commit;
14. verify Railway health;
15. verify exact version/commit;
16. verify Telegram/live monitor;
17. verify five-second cadence;
18. verify AI Ops service health;
19. verify no notification storm;
20. verify GitHub sync where phase includes it;
21. verify rollback.

Do not run the full community platform matrix just because a private AI Ops Railway patch exists.

Record community impact honestly.

=========================================================================
37. OBSERVABILITY / HEALTH
=========================================================================

Private health/status should expose safe fields:

- app version;
- commit;
- AI Ops version/schema;
- scheduler health;
- next audit;
- last audit;
- queue depth;
- active jobs;
- stale jobs;
- provider health summary;
- Supervisor health;
- persistent storage health;
- GitHub sync lag;
- last backup verification.

Never expose:
- credentials;
- auth headers;
- secret values;
- private URLs;
- exact observer coordinates.

=========================================================================
38. BACKPRESSURE / OVERLOAD
=========================================================================

If anomalies spike:

1. deterministic collector continues;
2. evidence packets enter bounded priority queue;
3. low-priority duplicates collapse;
4. AI concurrency remains capped;
5. highest-severity/recent cases first;
6. backlog age visible in GUI;
7. no impact to live monitor;
8. no unlimited disk growth.

Priority example:

100 critical recent reproducible anomaly
95 critical older anomaly
85 repeated regression signature
70 alert lifecycle anomaly
50 routine investigation
10 low-confidence curiosity

Actual policy should be configurable.

=========================================================================
39. WHAT HAPPENS IF EVERY AI IS DOWN
=========================================================================

Plane Alerts keeps working.

The hourly deterministic collector keeps working.

New evidence is stored.

GUI remains useful for:
- status;
- tasks;
- evidence;
- previous findings;
- provider health;
- GitHub sync;
- safe non-AI operations.

Supervisor enters degraded mode.

It must not pretend a weak/unavailable model performed deep reasoning.

When providers recover:
- probe safely;
- close circuit;
- resume backlog;
- continue from checkpoints.

=========================================================================
40. WHAT HAPPENS IF RAILWAY IS ABANDONED
=========================================================================

Target migration path:

1. clone Plane Alerts source from GitHub;
2. provision a new host;
3. provision persistent storage;
4. configure required secrets;
5. restore sanitized/versioned state from GitHub and the newest verified encrypted full snapshot from Dropbox when available;
6. validate hashes, decrypt snapshot and validate DB/schema;
7. restore Prediction Lab evidence from its established GitHub data lifecycle;
8. configure Mongo or replacement durable app backend as current architecture requires;
9. start Plane Alerts;
10. start AI Ops in read-only recovery mode;
11. verify state;
12. enable scheduler/workers;
13. change deployment DNS/webhook routing as required;
14. verify Telegram and monitoring;
15. perform a final provider health check.

The design must avoid Railway-specific assumptions inside core AI Ops logic.

Railway integration belongs behind deployment/storage adapters where practical.

=========================================================================
41. CONFIGURATION FLAGS
=========================================================================

Exact names may change after inspecting project conventions, but the architecture needs equivalent controls:

PRIVATE_AI_OPS_ENABLED
AI_OPS_SCHEDULER_ENABLED
AI_OPS_GUI_ENABLED
AI_OPS_SUPERVISOR_ENABLED
AI_OPS_PAID_USAGE_ALLOWED=false
AI_OPS_GITHUB_SYNC_ENABLED
AI_OPS_DROPBOX_BACKUP_ENABLED
AI_OPS_DROPBOX_BACKUP_ROOT
AI_OPS_TOOL_MUTATION_LEVEL
AI_OPS_MAX_CONCURRENCY
AI_OPS_DATA_DIR
AI_OPS_BACKUP_ENCRYPTION_KEY (introduced only with encrypted backup phase)

Because this feature is private-owner-only, do not automatically expose these in the community installer.

If source contains them, classify appropriately as owner/admin/private configuration.

=========================================================================
42. MODEL CONFIGURATION
=========================================================================

Do not permanently bake fast-moving model names into business logic.

Use configuration:

provider
role
model_id
capabilities
enabled
priority
max_context
supports_tools
supports_structured_output
free_only
last_verified_at

At startup or scheduled maintenance:
- do not blindly change models;
- model catalog changes require controlled verification;
- fallback only to allow-listed models.

=========================================================================
43. API INTEGRATION FACTS TO PRESERVE
=========================================================================

These facts were verified during planning on 2026-09-27 and must be re-checked if implementation happens much later.

GROQ
- official OpenAI-compatible base API is currently https://api.groq.com/openai/v1
- supported chat/responses/tool behavior depends on model/API
- adapter must still obey Groq-specific unsupported-field differences

MISTRAL
- official Chat Completion API is under /v1/chat/completions
- Mistral's native SDK/API supports tool calls/function calling
- use Mistral provider semantics rather than assuming identical behavior to Groq

GEMINI
- official Gemini API authenticates with Gemini API credentials
- REST requests currently support x-goog-api-key
- key/auth rules changed during 2026, so configured keys must be health-checked
- use Google Gemini SDK/REST adapter

CLOUDFLARE WORKERS AI
- requires Account ID + API token
- account-scoped Workers AI endpoint
- Authorization Bearer token
- Workers AI model IDs use Cloudflare model catalog
- Cloudflare also provides OpenAI-compatible gateway/chat endpoints, but do not silently switch from direct Workers AI to paid third-party unified billing
- keep paid usage disabled unless explicitly approved

OPENROUTER
- OpenAI-compatible API at https://openrouter.ai/api/v1
- model/provider fallbacks exist
- free status/rate limits can change
- allow-list only explicitly approved free models/routes

Official docs checked:
https://console.groq.com/docs/openai
https://docs.mistral.ai/api
https://ai.google.dev/api
https://ai.google.dev/gemini-api/docs/api-key
https://developers.cloudflare.com/workers-ai/get-started/rest-api/
https://developers.cloudflare.com/ai-gateway/usage/rest-api/
https://openrouter.ai/developers
https://dropbox.tech/developers/using-oauth-2-0-with-offline-access
https://developers.dropbox.com/oauth-guide

=========================================================================
44. SECRET REDACTION
=========================================================================

Redaction tests must include patterns for:

- Google/Gemini keys;
- Groq keys;
- Mistral keys;
- Cloudflare tokens;
- OpenRouter keys;
- Cohere trial key;
- OpenSky credentials;
- Telegram tokens;
- Mongo URIs;
- Railway tokens;
- Authorization headers;
- admin password;
- webhook secrets;
- private coordinates where policy requires.

If a model returns a secret-like string copied from input/tool output:
- redact before persistence;
- mark incident;
- do not sync to GitHub.

=========================================================================
45. COMMUNITY IMPACT
=========================================================================

Current intent:
PRIVATE OWNER FEATURE ONLY.

Therefore:
- no community installer requirement;
- no community API-key prompts;
- no community background AI worker;
- no community Supervisor;
- no community admin GUI requirement;
- no community release blocker merely because private AI provider is offline.

If code changes shared modules:
- record community impact;
- weekly Community Release validates any affected shared code;
- do not claim community validation from Railway-only tests.

If the owner later wants community support, create a separate explicit roadmap.

=========================================================================
46. ACCEPTANCE CRITERIA FOR THE COMPLETE PROGRAM
=========================================================================

The private AI Operations program is complete only when:

[ ] hourly deterministic audit runs durably;
[ ] no AI provider is required for Plane Alerts alert correctness;
[ ] worker jobs survive restart/deploy;
[ ] mid-task provider/key failures resume safely;
[ ] each configured key's independent quota is tracked, respected and used for legitimate failover;
[ ] Groq/Mistral/Gemini/Cloudflare/OpenRouter are correctly separated by adapter;
[ ] Cloudflare account/token pairs cannot be mixed;
[ ] Cohere trial is not an unattended dependency;
[ ] all AI output is schema/evidence validated;
[ ] independent review exists;
[ ] deep investigations can run safe repository/replay/test tools;
[ ] tool side effects are idempotent;
[ ] GUI shows live workers/tasks/findings/providers/sync;
[ ] Supervisor answers from backend state;
[ ] Supervisor session survives provider switching;
[ ] owner can mark findings useful/not useful;
[ ] private state survives Railway deployments;
[ ] private state has verified GitHub DR sync;
[ ] encrypted automatic Dropbox backup is independently verified;
[ ] Dropbox backup does not rely on an interactive ChatGPT session;
[ ] ChatGPT Dropbox plugin can be used as an owner recovery/inspection aid;
[ ] restore from GitHub-only path has been tested where applicable;
[ ] restore from Dropbox snapshot path has been tested;
[ ] restore has been tested;
[ ] secrets are not committed or displayed;
[ ] exact private locations are not unnecessarily persisted/exported;
[ ] existing Prediction Lab history remains intact;
[ ] verified corrected findings leave the active queue and are compacted/archived without destroying canonical history;
[ ] ai_ops_handoff.json (or deliberate compatible current equivalent) contains unresolved work only;
[ ] repo-tracked implementation-state metadata lets a new ChatGPT session resume without prior chat context;
[ ] no duplicate scheduled task responsibilities remain after final handover;
[ ] five-second monitoring path remains healthy;
[ ] production verification passes after every relevant release;
[ ] AGY/retired agent infrastructure remains absent.

=========================================================================
47. INSTRUCTIONS TO FUTURE CHATGPT IMPLEMENTING THIS FILE
=========================================================================

Before doing ANY phase:

1. Read the current project .txt instruction files fully.
2. Read the newer "AGY retired" instruction first where present.
3. Inspect current active/paused scheduled tasks.
4. Inspect actual current GitHub main, branches, status, diffs, PRs and release version.
5. Inspect Railway deployment, services, volumes, logs and current commit.
6. Inspect current provider parsers.
7. Inspect current Prediction Lab data/lifecycle.
8. Preserve existing work.
9. Determine the next valid three-part semantic version.
10. Implement ONLY the requested phase.
11. Do not begin the next phase until the current phase has passed its own release gate.
12. Keep AI Ops optional/private.
13. Keep runtime flight decisions deterministic.
14. Never introduce paid AI/API usage without explicit owner approval.
15. Treat each configured key as its own independent quota pool for legitimate failover and continuity, while respecting each individual key's limits and provider terms.
16. Never expose secrets.
17. Never claim a deploy/test/restore succeeded unless actually verified.
18. Do not run long community matrices for an ordinary private Railway patch; follow current Railway-vs-community policy.
19. Record community impact when shared code/config is affected.
20. Keep rollback available.
21. Read docs/private-ai-ops/implementation-state.json when present and continue from the first unfinished phase.
22. Read the latest ai_ops_handoff.json/current compatible handoff before reviewing unresolved findings.
23. Verify handoff claims against actual GitHub/Railway/Prediction Lab state.
24. Once a finding is verified resolved, remove it from active work/handoff but preserve canonical solved/history evidence.
25. Verify both GitHub DR sync and Dropbox backup status for phases that own backup/recovery.

=========================================================================
48. FINAL PRINCIPLE
=========================================================================

This system must be designed so that:

- workers can fail;
- keys can fail;
- providers can fail;
- models can change;
- Railway can restart;
- Railway can eventually be replaced;

without losing the facts of what happened or making the Plane Alerts live flight-monitoring path dependent on AI.

Workers investigate.
The Supervisor explains and controls the workers.
The backend remembers.
The GUI observes.
GitHub provides portable versioned disaster-recovery state.
Dropbox provides a second independent automatic backup target for encrypted recovery snapshots and owner-accessible archives.
Deterministic Plane Alerts code remains authoritative for actual aircraft prediction and alert decisions.

END OF PLANE ALERTS PRIVATE AI OPERATIONS PHASED ROADMAP
