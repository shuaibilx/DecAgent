# Benchmark exposure review

Reviewed: 2026-10-22 (Asia/Shanghai)

## Evidence and scope

The user confirmed that only two EnterpriseOps tasks entered project testing:

- CSM: `task_20260106_161859_265_00364ece_0e836ce8`
- ITSM: `task_20260115_201951_323_391f1eff_659b9dae`

The user has not viewed the other EnterpriseOps task prompts, target states, reference actions, or scoring material outside the project. The repository audit found the remaining 204 EnterpriseOps tasks only as IDs in inventory, manifest, or dry-run records; no evidence shows those task contents were sent to the Agent. The user also confirmed that the 80 official τ-Bench test tasks were not viewed outside the project. Project τ development used train examples.

## Assignment for a future cohort

- Mark the two exact EnterpriseOps cases above `development_exposed`.
- Mark the other 204 EnterpriseOps cases `not_exposed` at the exact-case level.
- Mark the 80 official τ-Bench test cases `not_exposed`; keep τ train cases for development and smoke only.
- Do not propagate exposure based on the unreviewed `prompt-surface-heuristics-1` workflow-family mapping.
- Keep every official task in the benchmark denominator. `not_exposed` records project task-level exposure only; it does not establish an independent holdout or rule out model pretraining contamination.

## Status at initial review

The review did not rewrite the frozen v8 manifest or its trial ledger. At that point, the reviewed assignment had not yet been frozen in a new manifest. No live evaluation batch was started by the review.

## Frozen cohort update (2026-10-22)

The reviewed assignment is now frozen in `comparison_trial_manifest_v9.json` with digest `f984ec74c9776efef0b6ba808ff035198500f1997537e01776dc2a895173a604`. The v9 cohort preserves all 286 full and 24 smoke cases, marks only the two confirmed EnterpriseOps cases as `development_exposed`, assigns the other EnterpriseOps and τ official test cases `not_exposed`, and marks τ train smoke cases as development data. This is a pre-registration only: no trial ledger, model call, tool call, or official score was created. v8 and its unknown route states remain unchanged and cannot be dispatched by the live runner.
