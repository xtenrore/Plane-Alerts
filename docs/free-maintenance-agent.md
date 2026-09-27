# Free maintenance agent

Plane Alerts has an optional repository-side maintenance worker for Prediction Lab investigation. It is deliberately separate from the live aircraft-monitoring process.

## What it does

Every two hours, the GitHub Actions workflow reads the newest durable `prediction-lab-data` unchecked case, gathers a bounded amount of relevant source context, and asks a configured free LLM provider for a structured investigation result. When evidence is not strong enough, it can open a deduplicated investigation issue. When the model proposes a small code correction, the worker accepts it only if the patch stays inside the allowlist, includes regression-test changes for application code, compiles, and passes the targeted tests. A successful candidate is opened as a pull request for the normal Plane Alerts CI/replay/release process.

The worker never merges or deploys. It cannot change GitHub workflows, deployment files, dependencies, configuration/secrets, release/version files, documentation, or Prediction Lab history.

## Provider order

The worker uses configured providers in this order and falls through on provider errors:

1. `FREELLMAPI_BASE_URL` + `FREELLMAPI_API_KEY` (`FREELLMAPI_MODEL`, default `auto`)
2. `OPENROUTER_API_KEY` or legacy `OPENROUTER_API` (`OPENROUTER_FREE_MODEL`, default `openrouter/free`)
3. `MAINTENANCE_GROQ_KEY`, then the existing `GROQ_KEY`/`GROQ_API_KEY` as a low-volume bootstrap fallback
4. `MAINTENANCE_GEMINI_KEY`, then the existing `GEMINI_API_KEY` as the final bootstrap fallback

Provider credentials remain in Railway. The workflow uses the repository's existing `RAILWAY_API_TOKEN` secret to read only the allow-listed variable names into the ephemeral GitHub runner, masks every value, and deletes the temporary variable dump immediately.

For long-term use, prefer maintenance-only provider credentials or a private FreeLLMAPI router so maintenance calls do not share quota with optional user-facing enrichment.

## Safety model

The agent is advisory and bounded. Deterministic Plane Alerts code, replay evidence, regression tests, required GitHub CI, and production verification remain the release authority. Missing or stale ADS-B evidence is treated as inconclusive. AI output must never become the runtime authority for trajectory, CPA, ETA, pass/no-pass, qualification, cancellation, or alert timing.

Only one maintenance-agent PR may be open at a time. This prevents free-model mistakes or a large unchecked backlog from creating an unbounded PR queue.
