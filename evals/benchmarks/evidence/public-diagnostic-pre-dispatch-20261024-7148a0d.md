# Bounded diagnostic pre-dispatch failure

This record documents an engineering preflight failure. It is not a model result and is excluded from benchmark scores.

## Attempt

- Candidate: `7148a0d0bb5a14aa492cdec1d7d5633d291ad950`
- Phase: bounded four-domain diagnostic, 4 tasks × 2 routes
- Command: `python evals/benchmarks/run_public_trials.py --root . --mode smoke --phase diagnostic --execute --report .decagent/evaluation/evidence/sop-agent-diagnostic-20261024-7148a0d.json`
- Failure: `Trial resource profile differs from the frozen public cohort`
- Failure point: plan/route construction, before model or business-tool dispatch

## Ledger evidence

The canonical ledger remains at `.decagent/business-eval/public-trials/cohorts/3863cc9274a26025fe5082ee5a97b66709f4959d479ffbe8ef8a67a49d42c29b-7148a0d0bb5a14aa492cdec1d7d5633d291ad950.sqlite3`.

- Route rows: 8 pending; no route was started.
- Model, tool, machine-time, and τ-step attempt rows: 0 each.
- Scores and cleanup results: none.
- Model requests, tokens, provider request IDs, and fees: none; no model request was sent.
- Original batch window: `2026-10-24T05:44:35.806457+00:00` through `2026-10-24T06:29:35.806457+00:00`. This window is not extended by the correction.

## Cause and correction

The bounded diagnostic replaced the plan's resource limits but retained each trial's digest from the broader smoke plan. The production compiler correctly rejected the mismatch. The correction recomputes each selected trial's resource-profile digest from the bounded diagnostic limits and tests that invariant.

Because the first candidate was rejected before any dispatch, a successor candidate may continue this same authorized window only when both old and new ledgers prove all eight routes pending, zero model/tool/machine-time/τ attempts, no scoring or cleanup evidence, and no stopped batch. The original ledger is retained; the successor inherits the original start and deadline. If any check fails or the original deadline expires, transfer is denied. A different candidate does not receive fresh limits.

The correction passes the offline dry-run, Ruff, strict OpenSpec validation, and the full deterministic suite (`2,236 passed, 19 skipped`). The successor candidate's same-SHA CI and the real diagnostic remain pending. No benchmark outcome can be inferred from this pre-dispatch failure.
