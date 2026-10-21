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

The reviewed assignment is frozen in `comparison_trial_manifest_v9.json` with digest `f984ec74c9776efef0b6ba808ff035198500f1997537e01776dc2a895173a604`. The v9 cohort preserves all 286 full and 24 smoke cases, marks only the two confirmed EnterpriseOps cases as `development_exposed`, assigns the other EnterpriseOps and τ official test cases `not_exposed`, and marks τ train smoke cases as development data. At the time of this freeze, no v9 trial ledger or live request existed; v8 and its unknown route states remained unchanged.

## Subsequent execution (2026-10-22)

After the v9 freeze, the first-wave phase ran under candidate `126fe5a3dc9edcb3d595d0aa40fa1a5d8713af41`, which passed same-SHA CI run `39639666250`. The run selected the same four fixed cases and eight ReAct/Plan routes. It did not dispatch any of the 80 official τ-Bench test tasks; both τ cases were from train. Results and unresolved usage are recorded in [the v9 report](../../../generated/261022/0133_公开评测首波报告_v9.md) and [machine evidence](public-first-wave-live-20261022-v9-126fe5a.json). Exposure labels and the frozen manifest were not changed by execution.

## Evaluator-authoring access note (2026-10-22)

A later Codex audit command used a recursive rg search over the tau source data/tau2/domains tree with JSON files included. That tree contains tasks.json files for multiple splits. The tool output was truncated, so the exact task records and whether any selected retail/telecom official-test rows appeared in the returned excerpt cannot be established. This was an evaluator-authoring session only: no task content was sent to Qwen, Jev, or a benchmark Agent, and no model or evaluation route was dispatched. The audit will not use task-specific content from that output; future source review must restrict itself to policy files, tool implementation/schema, and explicitly approved development/train material, excluding all task JSON. The user’s answer above remains an accurate statement of the user’s own exposure. The v9 manifest and trial ledger are frozen and remain unchanged; v9 records user/Agent exposure, not this later Codex authoring-session access. Do not describe tau official test as inaccessible to every project-side reviewer; treat any strict reviewer-blind claim as uncertain until this access is represented in a separately reviewed exposure record.
