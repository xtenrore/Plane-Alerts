PLANE ALERTS — FINAL ETA / CPA REDESIGN COMPLETION

MISSION

Complete the existing Plane Alerts ETA/CPA redesign into a fully integrated, independently verified, production-ready release candidate.

This is the FINAL COMPLETION PASS of the existing redesign, not a new project, rewrite, or unrelated feature update.

Repository: xtenrore/Plane-Alerts

Existing branch: fix/eta-kinematics-redesign

Last independently reviewed commit: f77057ec97f07e0fde44bb1b83330007946908a8

Previously verified GitHub Actions run: 38122432907, with 717 tests passing.

These are reference checkpoints, NOT assumed current state. Inspect GitHub and the local workspace before proceeding.

Preserve all completed work, finished tests, existing history, and functioning production behavior.

The final result must be a tested release candidate that genuinely improves aircraft prediction accuracy without weakening the existing protections.

⸻

0. RECOVER AND PROTECT THE CURRENT STATE

Before modifying anything:

1. Read instructions.md completely.
2. Read all applicable Plane Alerts .txt project instructions, including the standing engineering, release, installer-maintenance, and Railway-versus-Community policies.
3. Follow the instruction permanently excluding retired external agent integrations from Plane Alerts runtime. Your use as an interactive development tool must not restore retired agent services, dependencies, credentials, schedules, or infrastructure.
4. Inspect the actual Git remote, branch, HEAD, worktrees, git status, staged/unstaged changes, and untracked files.
5. Inspect current main, the ETA redesign branch, recent commits, open PRs, current canonical application version, deployment state, and relevant GitHub Actions.
6. Read the full relevant trajectory, confidence, 3D proximity, route-guard, lifecycle, notification, and Prediction Lab implementations.
7. Preserve all verified work. Never use destructive reset, clean, forced checkout, force push, or overwrite unfinished work.

Continue the existing branch. Do not create a replacement redesign.

If another agent has already completed part of the requested work, verify it and move on. Do not unnecessarily repeat completed engineering.

Do not assume that the historical Codespaces workspace directory name indicates the wrong repository; verify the actual Git remote.

⸻

1. FINAL AUTHORITATIVE PREDICTION INTEGRATION

The analytical Napier CDA/ETA engine has already been introduced and integrated into the v4.3 prediction wrapper for approximately straight, unaccelerated aircraft motion.

Independently verify that integration through the COMPLETE current production wrapper chain, not just a direct function call.

Trace:

ADS-B ingestion → accepted observations → freshness → trajectory history → motion projection → horizontal CPA/ETA → altitude-aware 3D proximity → confidence → destination/path guard → qualification/cancellation → alert lifecycle → Telegram.

Confirm which implementation is actually authoritative at runtime.

Preserve all existing safeguards, especially:

* Position freshness, sample age, and outlier rejection.
* Robust historical speed/heading estimation.
* v5.3 shared midpoint motion.
* All established prediction and compatibility wrappers.
* Continuous versus discretely sampled CPA handling.
* 3D altitude-aware proximity.
* Confidence and uncertainty.
* LTFM/LTBA airport arrival protections.
* Provider-first destination/path authority.
* Confirmation requirements, cancellation hysteresis, and cancelled-alert reactivation protection.
* Observed-pass finalization.
* Five-second monitoring cadence.

IMPORTANT GEOMETRY CONSISTENCY CHECK:

The Napier implementation can replace analytical horizontal CPA/ETA while the existing trajectory path remains discretely sampled.

Investigate whether these values become inconsistent:

* projected_closest_km
* time_to_cpa_s
* radius_entry_s
* projected_closest_slant_km
* projected_closest_3d_km
* time_to_3d_cpa_s
* horizontal_at_3d_cpa_km
* The projected path and confidence/qualification decisions.

These quantities must refer to mathematically consistent physical predictions.

Do not blindly substitute analytical horizontal CPA into an unrelated altitude/time sample.

Choose the smallest correct, deterministic integration that preserves physical meaning and the established safety architecture.

If analytical geometry cannot safely improve a particular case, retain the existing validated method.

⸻

2. REPLACE THE MISLEADING BENCHMARK WITH A REAL PRODUCTION COMPARISON

The existing benchmark reports approximately:

* 51.9% CPA error reduction.
* 54.9% ETA error reduction.

These numbers must NOT be advertised as real-world improvement until independently validated.

The current benchmark largely uses generated tracks and compares the proposed predictor with an unpatched core predictor rather than the full previous production stack.

Correct this.

Required comparison

Use two isolated, immutable code snapshots:

Baseline: the exact production prediction implementation on the original main commit before this redesign, including every installed wrapper, safeguard, and actual runtime configuration.

Candidate: the entire new production predictor with the redesigned ETA/CPA implementation, including the same wrapper chain and all intended changes.

Run both against identical input observations, observer settings, timestamps, altitude assumptions, prediction horizons, and qualification policies.

Do not compare the candidate against a weaker raw mathematical function and call it a production improvement.

Keep baseline and candidate execution isolated so Python imports, monkeypatches, caches, and module initialization cannot contaminate each other.

Historical aircraft evidence

Search the existing Prediction Lab datasets, synchronized historical evidence, Error Museum, route-history observations, and safely accessible historical ADS-B records.

Prioritize genuine historical observations with independent subsequent outcome evidence.

For every scorable aircraft encounter, reconstruct predictions using only information available at the original prediction timestamp.

Prevent future-information leakage.

Measure subsequent observed closest approach and timing using the later observations.

Missing coverage, insufficient sampling, or unobserved CPA must be explicitly INCONCLUSIVE.

Do not invent historical flight records or observed outcomes.

Metrics required

Calculate for both production implementations:

1. Horizontal CPA mean and median absolute error.
2. ETA-to-CPA mean and median absolute error.
3. Radius-entry timing error when independently observable.
4. False-positive qualification rates.
5. Missed confirmed passes.
6. Alert qualification/cancellation oscillation.
7. Prediction stability during turns.
8. Confidence calibration and uncertainty handling.
9. Error breakdown by observation freshness, prediction horizon, and aircraft behavior.
10. Runtime CPU, memory, per-prediction latency, and monitoring-cycle impact.

Include case counts, coverage criteria, excluded/inconclusive case counts, and the measurement methodology.

Use meaningful holdout evidence not selected to favor the new algorithm.

Keep synthetic scenarios as regression tests, not as substitutes for real-world accuracy evidence.

If adequate real observations cannot be recovered, do not invent numerical improvements or declare the final accuracy gate passed. Report the evidence blocker explicitly and leave the candidate unpromoted.

⸻

3. VERIFY AIRPORT ARRIVALS AND TURNING AIRCRAFT

The historical Plane Alerts problem is that aircraft initially appear to approach a user but later turn toward Istanbul airports without actually passing the user.

This must be specifically investigated.

Cover:

* LTFM / Istanbul Airport arrivals.
* LTBA / Atatürk arrivals.
* Aircraft initially heading directly toward the observer.
* Aircraft beginning sustained turns.
* Aircraft leveling out after turns.
* Turns that stop, reverse, or vary in rate.
* Aircraft passing above or near an airport without landing.
* Unknown or incorrect destination metadata.
* Provider changes during an approach.
* Stale ADS-B data during the critical turn.
* Genuine nearby passes that must NOT be suppressed merely because the aircraft is airport-bound.

Verify that accepting turning rates up to 3.6°/s does not introduce false confidence or unstable extrapolations.

A sustained recent turn cannot automatically be projected indefinitely as a constant-rate circular flight.

Extrapolation must remain physically reasonable and uncertainty-aware.

Use genuine historical LTFM/LTBA cases wherever possible, and add deterministic regressions for every confirmed failure.

⸻

4. MATHEMATICAL AND TELEMETRY ROBUSTNESS

Audit the entire changed mathematical path for:

* Incorrect units: knots, metres/second, kilometres/second.
* Observation-age correction applied zero, one, or multiple times incorrectly.
* Stale observations producing overly precise ETAs.
* Missing heading or ground speed.
* Zero speed and near-zero distance.
* Negative, invalid, NaN, or infinite telemetry.
* Bearing wraparound and discontinuities.
* Antimeridian and polar coordinates.
* Near-tangent radius intersections.
* Floating-point boundary errors.
* CPA occurring behind the aircraft.
* Aircraft already inside the radius.
* Receding aircraft.
* Strong acceleration/deceleration.
* Vertical separation and 3D timing.
* Horizon truncation and interpolation edge cases.

Verify that ETA-to-radius-entry and ETA-to-closest-approach are never confused.

Ensure that an unavailable or uncertain prediction remains explicitly uncertain rather than returning misleading precision.

Add reproducible regression tests for every real defect found.

Do not silently change thresholds or weaken acceptance conditions merely to make tests pass.

⸻

5. FINISH THE REAL TELEGRAM INTEGRATION

Verify the actual live alert-rendering path for:

* NEXT SHOT
* CAMERA READY
* PHOTO NOW
* TRAJECTORY CHANGED / CANCELLED
* AIRCRAFT PASSED

Preserve existing message editing, anti-spam behavior, feedback actions, and notification lifecycle IDs.

Ensure that ETA, current distance, projected closest pass, bearing, closure, and optional perimeter-entry information are internally consistent.

Show UTC arrival times only when the estimate is meaningful.

Never describe an uncertain projection as a guaranteed arrival or overhead pass.

Keep messages compact, readable, professionally formatted, and safe for Telegram HTML.

Test missing values, special characters, stale message editing, countdown transitions, and Telegram failures.

Do not redesign unrelated Telegram menus or user interfaces.

⸻

6. SHADOW EVALUATION AND SAFETY PROMOTION

Before treating newly introduced prediction behavior as fully verified:

1. Confirm the old and new predictor outputs can be compared independently.
2. Preserve the full historical replay evidence.
3. Check that the new calculations do not increase verified false alerts.
4. Check that genuine confirmed passes are not lost.
5. Check that confidence and route guards remain effective.
6. Check that uncertainty and missing ADS-B coverage are handled conservatively.
7. Verify cancellation and recovery stability.
8. Confirm that shadow-only candidates cannot control production decisions.

Do not promote a model based only on synthetic tracks, passing unit tests, or aesthetically impressive numerical outputs.

If evidence supports only a subset of the new algorithm, retain only the independently validated authoritative changes. Leave unvalidated alternatives in shadow mode.

⸻

7. PERFORMANCE AND PRODUCTION SAFETY

The aircraft monitor must preserve its five-second cadence.

Verify:

* Normal-cycle timing.
* Heavy-aircraft-density scenarios.
* Multiple users and spotting locations.
* Frequent provider changes.
* Stale or delayed ADS-B data.
* Slow external providers.
* Database degradation.
* Notification bursts.
* Prediction computation overhead.
* Restart and recovery behavior.

Optional enrichment, auditing, and historical analysis must never block the critical monitoring path.

Use bounded tasks, caches, timeouts, and concurrency.

Do not introduce paid AI/API dependencies.

Runtime AI must never decide aircraft trajectory, CPA, ETA, qualification, cancellation, pass/no-pass, or alert timing.

If isolated testing cannot prove live production cadence, identify the remaining rollout verification gate rather than claiming it passed.

⸻

8. TESTING AND INDEPENDENT QUALITY GATES

Reuse valid existing CI results for unchanged exact commits.

After new modifications, execute the relevant targeted tests and historical replay comparisons.

Before recommending the final candidate for production merge, run the full applicable repository release test suite and GitHub Actions against its exact final SHA.

Inspect individual jobs, including:

* Full pytest suite.
* Prediction and geometry tests.
* New ETA benchmark tests.
* Error Museum regressions.
* LTFM/LTBA route-guard regressions.
* Provider and freshness regressions affected by the change.
* Telegram lifecycle/formatting tests.
* 3D proximity tests.
* Performance and cadence benchmarks.
* Architecture/import consistency checks.

Do not weaken tests, edit baseline evidence, fabricate benchmark results, or mark failed gates as passed.

Use independent subagent reviews where beneficial, but require one orchestrator to verify the integrated final result.

Do not let separate agents overwrite each other’s changes.

A successful test suite establishes regression coverage, not automatically real-world prediction accuracy.

⸻

9. SCOPE, RELEASE MANAGEMENT AND PRESERVATION

This remains ONE ETA/CPA REDESIGN COMPLETION UPDATE.

Do not split the remaining work into arbitrary new phases or restart completed work.

Do not perform unrelated changes to:

* Private AI Operations.
* Retired external agent infrastructure.
* Installer architecture.
* Database migration systems.
* Prediction Lab synchronization infrastructure.
* General UI redesigns.
* Provider credential management.
* Unrelated production services.

Fix directly related bugs only when necessary to make this redesign correct.

Review versioning and documentation requirements. Prepare accurate release notes and README changes if the candidate is ready for release; do not claim it is already deployed or published.

Check and record community/self-host impact, but do not run the full Windows ARM64, Linux ARM64, Raspberry Pi, installer, and updater matrices as ordinary Railway-only gates. Those remain owned by the Weekly Community Release process.

Preserve rollback capability and the original production baseline.

Do not merge into main, tag a stable release, deploy to Railway, or publish a community release without my separate authorization.

The objective is a thoroughly verified, merge-ready candidate, not an unapproved production deployment.

⸻

10. REQUIRED FINAL REPORT

When finished, push the verified result to the EXISTING branch:

fix/eta-kinematics-redesign

Then provide:

A. Implementation

* Exact final commit SHA.
* Files changed.
* Root causes fixed.
* How the new predictor integrates into the production wrapper chain.
* Explicit confirmation that existing airport/route/3D/lifecycle safeguards remain intact.

B. Quantitative Evidence

* Baseline SHA and candidate SHA.
* Exact predictor stacks compared.
* Real historical flight/case counts.
* Number of independently scorable outcomes.
* Inconclusive/missing-coverage counts.
* Old-versus-new CPA error.
* Old-versus-new ETA error.
* False positives, confirmed misses, and cancellation oscillation.
* Performance and cadence comparison.
* Any remaining uncertainty.

C. Validation

* Targeted tests performed.
* Full test-suite result.
* Error Museum/replay results.
* Exact GitHub Actions run and status.
* LTFM/LTBA verification.
* Security/privacy and compatibility results.

D. Final Decision

Give one of these outcomes:

READY FOR OWNER APPROVAL: Every applicable safety gate passed, adequate outcome evidence supports the prediction changes, and the branch is ready for controlled production release.

BLOCKED — MORE EVIDENCE REQUIRED: Code and tests are complete, but historical outcome evidence, live cadence verification, or another required acceptance gate remains unresolved.

BLOCKED — DEFECT FOUND: A reproducible correctness or integration problem remains.

Do not conceal blockers or relabel synthetic benchmarks as real-world evidence.

Do not claim deployment, merge, or production verification unless those actions actually occurred.

Finally, preserve all checkpoints, commit and push valid work, give the owner a concise completion report, and STOP.

Do not proceed into unrelated repository-wide bug hunting unless I explicitly request it.

FINAL ACCEPTANCE PRINCIPLE

Plane Alerts is not improved merely because a new mathematical engine produces more precise numbers.

The redesign succeeds only when independent evidence demonstrates that it makes correct aircraft predictions more reliable without weakening existing safeguards, creating false airport alerts, missing confirmed passes, or delaying the critical five-second monitor.

Correctness, physical meaning, measurable reliability, and recoverability take priority over complexity.