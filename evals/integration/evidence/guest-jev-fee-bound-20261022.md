# Guest Jev fee-bound enforcement

Date: 2026-10-22

## Decision

The guest trial ledger must not dispatch Jev while the configured `$0.01` reservation is only a fixed accounting amount and there is no verified price profile plus enforced request/response token ceiling. This preserves the trial budget invariant: a provider may not charge more than the amount reserved before dispatch. Employee Jev requests continue through the existing employee ledger path.

## Implementation

`UsageLedger.reserve()` now rejects `model_family="jev"` when bound to a guest trial ledger, before inserting either an `app.usage_records` row or a `business.guest_trial_usage_reservations` row. `LedgerAttemptBudget` converts that budget rejection into the existing `budget_exceeded` decision result, so the adapter does not send an HTTP request.

The guest test uses an HTTP mock transport and asserts all three boundaries: the evaluation is unavailable with `budget_exceeded`, the mock provider receives zero requests, and both task-level and trial-level reservation counts remain zero. It does not contact TypeSafe, OpenRouter, Qwen, or any other provider.

## Verification

- Test-first red check: before the guard, the regression failed because the evaluation was available and the mock provider was called with a `$0.01` reservation.
- After the guard: `test_guest_jev_request_is_rejected_before_dispatch_without_fee_bound` — 1 passed.
- Full `evals/integration/test_business_guest_trial_budget.py` module — 28 passed.
- Employee-path regression `test_jev_retries_share_the_same_primary_ledger` — 1 passed.
- Ruff on `decagent/storage/usage.py` and `evals/integration/test_business_guest_trial_budget.py` — passed.
- Tests requiring PostgreSQL used an isolated, disposable PostgreSQL 16 container on loopback. The container and its databases were removed after each run; no existing project container or database was changed.

## Remaining limitation

This is a fail-closed safety boundary, not a guest Jev pricing implementation. Guest Jev remains unavailable until an immutable, auditable provider price profile and enforced request/response limits can produce a reliable per-request upper bound. OpenSpec task 3.2 therefore remains unchecked; the guest trial feature flag remains off by default.
