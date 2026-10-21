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

## Historical boundary and status

This review does not rewrite the frozen v8 manifest or its trial ledger. v8 remains `unreviewed_unassigned` as originally recorded. The reviewed assignment applies to a future manifest, which has not yet been frozen; no new live evaluation batch was started by this review.
