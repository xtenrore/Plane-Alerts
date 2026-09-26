# Solved Prediction Lab cases

This directory contains durable Prediction Lab cases whose evidence-backed fix has completed the full release lifecycle.

A case may enter `solved/YYYY-MM-DD/` only when:

1. the Investigator/Fixer completed the case and preserved it in `reviewed`;
2. a release candidate explicitly references that `case_id` and reviewed evidence path;
3. the exact tested fix commit was merged;
4. that exact commit was deployed to Railway successfully; and
5. mandatory production verification passed for the affected behavior.

Merge alone is never sufficient.

A solved case must preserve the original `case_id`, constituent `event_id` values, investigation evidence and conclusion. It must also record the resolving commit SHA, deployed version, production-verification timestamp/result, and regression/Error Museum references when applicable.

Prediction Lab evidence must not be deleted or cleared merely because a fix was merged or released. If deployment or verification fails, keep the case in `reviewed` or explicitly unresolved. Runtime spool retention does not apply to this durable repository history.
