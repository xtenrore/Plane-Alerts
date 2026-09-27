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

The workflow accepts `OPENROUTER_API_KEY`, legacy `OPENROUTER_API`, `FREELLMAPI_BASE_URL`, and `FREELLMAPI_API_KEY` directly from GitHub Actions secrets. A non-empty GitHub value takes precedence over a same-named Railway variable. Remaining allow-listed provider settings may be loaded from Railway with the repository's existing Railway token. Secret values are masked and the temporary Railway variable dump is deleted immediately.

If a key is stored as a GitHub Environment secret rather than a repository Actions secret, the job must be assigned to that GitHub Environment before Actions will expose it. Do not duplicate a working environment-scoped secret into source files or ordinary repository variables.

## FreeLLMAPI router state

The optional FreeLLMAPI gateway is infrastructure for maintenance inference only. It must not be imported into the Plane Alerts five-second monitoring path. Its SQLite/provider-key state belongs on a dedicated persistent volume mounted at `/app/server/data`, separate from `/data/prediction_lab`. Keep its encryption key stable across restarts and upgrades. Provider credentials placed in the router remain separate from Plane Alerts runtime configuration.

The gateway should stay unexposed until authentication/bootstrap and persistent storage are confirmed. Once a unified FreeLLMAPI API key is available, GitHub Actions needs only the router base URL and that unified key; upstream provider keys can remain encrypted in the router instead of being copied into Plane Alerts.

For long-term use, prefer maintenance-only provider credentials or the private FreeLLMAPI router so maintenance calls do not share quota with optional user-facing enrichment. Multiple free credentials may improve resilience, but they must not be rotated or multiplied to evade provider quotas or terms.

## Safety model

The agent is advisory and bounded. Deterministic Plane Alerts code, replay evidence, regression tests, required GitHub CI, and production verification remain the release authority. Missing or stale ADS-B evidence is treated as inconclusive. AI output must never become the runtime authority for trajectory, CPA, ETA, pass/no-pass, qualification, cancellation, or alert timing.

Only one maintenance-agent PR may be open at a time. This prevents free-model mistakes or a large unchecked backlog from creating an unbounded PR queue. A future goal-loop controller must preserve the same bounded-work rule: one finite iteration, explicit evidence, bounded state, and a hard stop rather than an unbounded autonomous loop.
