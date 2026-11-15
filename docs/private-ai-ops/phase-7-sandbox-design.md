# Phase 7 controlled engineering investigation — accepted checkpoint

Phase 7 is complete for the existing Private AI Operations umbrella release. The exact tested implementation checkpoint is `5468d273f71648d353cf46337fd3f7dcf2b4e2fd`. Phase 8 has not started.

## Controlled gateway

The private service owns durable `ai_ops_tool_operations` records in the existing orchestration store. Every operation is backend-defined and carries validated arguments, parent job/finding references where applicable, exact `source_commit`, immutable input hash, sandbox identifier, worker lease, bounded/redacted result metadata, timestamps, status, output hash and artifact hash.

The gateway does not expose arbitrary shell or model-generated command strings. It supports deliberate source inspection, approved replay/test execution, isolated candidate-test and candidate-patch creation, diff/hash capture and bounded result retrieval. Push, merge, force-push, deployment, Railway mutation, GitHub secret mutation, Telegram actions, raw secret reads, environment dumps and arbitrary network requests are not exposed operations.

Completed operation results are idempotently reusable. Duplicate workers cannot execute the same operation concurrently. Expired uncertain in-flight work is marked interrupted/failed rather than silently rerun. Candidate success never changes a production finding to `RESOLVED`.

## Exact source and candidate isolation

Engineering work is created from an exact pinned source commit. Path traversal, absolute-path escape and symlink escape are rejected. Candidate tests and patches are written only to disposable isolated snapshots/workspaces. The canonical repository checkout, `main` and the umbrella branch are not edited by candidate operations.

Candidate provenance persists the base commit, changed files/diff, artifact hash and related replay/test references. No candidate operation can push, merge or deploy.

## Container isolation

The accepted runner uses a credential-free Docker container for security-sensitive test/replay execution. The backend constructs the complete approved command and applies:

- `--network none`;
- read-only source mount;
- `--read-only` container filesystem with bounded writable scratch where required;
- `--cap-drop ALL`;
- `--security-opt no-new-privileges`;
- unprivileged uid/gid;
- `--pids-limit 64`;
- `--memory 768m`;
- `--cpus 1`;
- no `/data/ai_ops` mount;
- no `/run/secrets` mount;
- bounded timeout and output capture.

The host launcher keeps appropriate CPU/file-size limits. Memory/process isolation for Docker is applied by Docker itself rather than by a host `RLIMIT_AS` inherited by the Docker CLI.

## Replay blocker and fix

The first real pinned Error Museum canary, GitHub Actions run `36412626489`, failed at `op:replay`. Bounded redacted diagnostics added in later commits exposed the actual failure in run `36414194131`:

`runtime/cgo: pthread_create failed: Resource temporarily unavailable`

The replay target `tests/test_error_museum_v47.py` had not started. The Docker CLI is a Go program and had inherited the sandbox launcher `RLIMIT_AS=768 MiB`; its runtime aborted before it could create the isolated test container.

Commit `d27c0d7a552307660852c5c2fe76a2478b481db2` fixed the resource-limit scope. Docker retains container memory/CPU/PID isolation through Docker controls; non-Docker isolation retains its address-space limit. Regression coverage verifies this distinction.

The first successful fixed replay rerun was `36414767889`.

## Safe replay diagnostics

Failed operations expose only a bounded redacted diagnostic containing the operation id/type/status, exit code, approved target, exact source commit, timeout state, output-truncated flag, result hash, and bounded redacted stdout/stderr summaries. Secret-like material and Authorization data remain redacted. The diagnostic JSON itself is bounded and remains valid after redaction.

## Final acceptance evidence

Final exact-checkpoint sandbox/security run `36416809098` passed on `5468d273f71648d353cf46337fd3f7dcf2b4e2fd` with:

- real pinned Error Museum replay PASS;
- exact source read/search/commit operations PASS;
- approved targeted candidate test PASS;
- 17 prohibited-operation rejections plus symlink/path containment checks;
- outbound network isolation PASS;
- secret/environment isolation PASS;
- bounded large-output behavior PASS;
- intentional timeout + orphan-container cleanup PASS;
- completed-operation reuse / idempotency PASS;
- candidate regression-test and candidate-patch demonstrations PASS;
- canonical source unchanged;
- no push, merge or deployment from the candidate path;
- sanitized DR export/restore PASS.

Final test gates on the same code checkpoint:

- Private AI Ops: **139 passed**;
- full repository: **822 passed, 1 warning**;
- dependency integrity: PASS;
- required Phase 7 sandbox/security workflow: `36416809098` PASS;
- Private AI Ops checkpoint/deploy workflow: `36416809040` PASS;
- full PR tests: `36416814014` PASS.

## Schema 7 and durable checkpoint

Schema 7 adds durable controlled-tool operation records while preserving Phase 6 jobs/reviews/feedback/provider state. Migration tests passed. The exact tested checkpoint was deployed only to the dedicated `Plane-Alerts-AI-Ops` service as Railway deployment `79a16cc9-697e-497a-85d8-d175362431b2`.

The Railway `/ready` healthcheck passed after migration. Startup re-read the Phase 1 stability marker and the two persisted Phase 6 canary reviews from the dedicated `/data/ai_ops` volume, proving the existing data survived the schema 6 → 7 checkpoint deployment.

The Phase 7 DR canary restored sanitized operation/candidate provenance with SHA-256 `3d9774cac8c3bf9faf0c6ef12fab1b64356cbb8269507ed8c9b1f212d1a8f27b`.

## Runtime boundaries after acceptance

The dedicated Railway service remains a durable owner-private coordination/checkpoint service. It is **not** an unrestricted engineering host. Security-sensitive engineering execution remains in the isolated GitHub/Docker runner.

The production hourly AI scheduler remains disabled. Real providers remain disabled in the Phase 7 checkpoint service. Paid runtime AI was not introduced. Existing Prediction Lab Release and Weekly Community Release task ownership is unchanged.

The main Plane Alerts service was not redeployed by Phase 7. A fresh 720-sample cadence after the checkpoint remained healthy: p50 5000.2 ms, p95 5001.1 ms, p99 5001.5 ms, maximum 5024.8 ms, zero intervals above 10 seconds.

One separate limitation was observed on the unchanged main service: auxiliary `notification_telemetry` SQLite persistence intermittently timed out / hit a nested-transaction error while the monitor and notification cycles continued. This is outside the Phase 7 AI Ops code path and is not claimed fixed by this checkpoint.

## Stop boundary

Phase 7 acceptance is complete. Do not merge draft PR #143, do not deploy the final umbrella release, do not enable the hourly AI scheduler, and do not start Phase 8, the private Operations GUI, or Supervisor Chat.
