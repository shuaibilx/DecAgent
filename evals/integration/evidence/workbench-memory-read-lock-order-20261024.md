# Workbench memory-read lock-order regression (2026-10-24)

## Incident

GitHub Actions run [40034412934](https://github.com/shuaibilx/DecAgent/actions/runs/40034412934), on candidate `3318179caa9e62ec1b7e5350054df9c7ceebfa5d`, failed the production Gateway browser E2E while waiting for the employee action approval button. The submitted task remained in `running / execute` and never exposed the approval.

PostgreSQL logged a deadlock among three transactions:

1. The worker heartbeat tried to update the leased `app.tasks` row.
2. The MCP operation-preparation transaction held the task row while inserting a `business.operations` row that references the employee account; its foreign-key check waited for `FOR KEY SHARE` on that account.
3. After the browser submitted a task, `business.js` loaded the task's memory view in parallel. The read-only `GET /api/v2/business/memories?task_id=...` called `BusinessMemoryControl.history()` → `_current()` → `lock_employee_task()`, locking the employee account before requesting the task row. It therefore held the account lock while waiting for the worker's task lock, completing the cycle.

The PostgreSQL cycle was `task row → employee account → task row`; the heartbeat was another waiter on the task tuple. This explains why the task stayed leased and the browser never saw a human-wait state. The prior failure log records the exact SQL and lock cycle in the run above.

## Change

Read-only task projections now validate the employee and workspace without row locks. `history`, `get`, and dependency checks use this path; workflow-instance validation can also use a non-locking task read. Approval, correction, confirmation, revocation, cancellation, and other mutations retain the existing account-then-task locking path and its permission checks.

## Verification

- The new PostgreSQL regression `test_read_only_task_memory_lookup_does_not_wait_on_running_worker_task` failed before the fix after three seconds while a worker transaction held the task row; it passes after the fix and still returns the expected `contract_changed` response for the historical `business-2` task.
- The existing concurrent approval/memory test also passes.
- Full local PostgreSQL integration run: `55 passed` across `test_business_gateway.py`, `test_business_memory_gateway.py`, and `test_conditional_dependency_boundaries.py` on Python 3.11. The disposable PostgreSQL test service was `decagent-conditional-postgres`.
- The production Gateway Chromium E2E passed locally: employee login → synthetic task → explicit approval → MCP write → verified receipt; zero paid model requests and zero browser errors.
- Ruff passed. Same-candidate CI rerun is pending; run 40034412934 remains a historical failure and is not represented as green.

This is offline concurrency and browser evidence only. It does not start model-backed evaluation, replay unknown routes, or change any benchmark ledger.
