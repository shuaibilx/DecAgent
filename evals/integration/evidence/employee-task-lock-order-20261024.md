# Employee task lock-order regression

Date: 2026-10-24 (Asia/Shanghai)

## CI failure

The browser E2E job in [GitHub Actions run 39999606835](https://github.com/shuaibilx/DecAgent/actions/runs/39999606835) failed on commit `58351757afdee64f3a6618fb0eefa1b1b69ab613`. Its final assertion expected the employee task to reach `目标已完成`; the API instead returned `database_unavailable` while the browser waited for confirmation. The test ran through Chromium, PostgreSQL, and the production gateway path; other jobs in the run completed successfully.

## Root cause and correction

Two employee task paths acquired rows in opposite orders. Approval and task mutation could lock `app.tasks` before the employee identity; task memory and workflow registry locked the identity before `app.tasks`. Concurrent requests could therefore form a PostgreSQL lock cycle. A shared `OperationService.lock_employee_task()` now establishes identity/session/account/person first, then task, and is used by the browser task mutation, memory, and workflow-registry paths.

Reviewing the broader gateway suite exposed a second transaction interaction: resume held the employee identity lock in one transaction and then `issue_mcp()` opened another connection that tried to lock the same rows. Resume now issues that credential on its active transaction connection. If the outer transaction rolls back after the credential file was written, idempotent cleanup removes the orphaned file only when no matching database credential exists. MCP issuance uses a shared row lock for its session/account validation, avoiding an unnecessary lock upgrade.

## Verification

- Current code: 6 focused PostgreSQL integration cases passed, covering resume, concurrent approval plus memory history, MCP issue/revoke, outer-transaction rollback cleanup, and stale browser sessions.
- Broader gateway, memory, and workflow-registry integration files: 50 passed in 424.83 seconds on the same-connection resume fix before the final lock-mode narrowing; the six focused cases above were rerun against the final code.
- Ruff check, Ruff format check, and `git diff --check` passed for the changed implementation and test files.
- Tests used disposable local PostgreSQL 17 containers bound to loopback with no persistent volumes. No model, external business API, or deployment was called.

## Remaining verification

The fix has not yet been pushed or re-run in GitHub Actions. The failed run remains an accurate historical failure; a new same-commit CI run is required before calling the CI regression resolved. No private dispatch, release tag, or SOP deployment was triggered.
