# Guest task queue verification — 2026-10-22

## Scope

Verified the local PostgreSQL-backed guest execution queue: enqueue atomicity with daily task admission, global waiting capacity, cancellation, fair claims, per-workspace/global concurrency, expired lease recovery, stale-generation rejection, API-visible queue status, and HTTP 429 rollback when the queue is full.

The queue uses the existing `business.guest_trial_task_admissions` ledger. Migration `028_guest_execution_queue.sql` adds queue state and a task-state synchronization trigger. `GuestExecutionQueue` serializes guest enqueue/worker claim through a PostgreSQL advisory transaction lock. The worker still uses its existing task lease and checkpoint recovery; guest tasks without a durable queue admission are not eligible for guest dispatch.

## Evidence

- `evals/integration/test_business_guest_task_queue.py`: **6 passed in 43.32 seconds**.
- Worker, Gateway, Workbench v2, and guest trial budget regression files: **64 passed in 327.97 seconds**.
- Ruff for the changed queue, Worker, budget, Gateway, database and integration test files: passed.
- Python compile check for the changed runtime modules: passed.
- `openspec validate guest-sop-local-trial --strict`: valid.
- `git diff --check`: passed; Git emitted only the repository's existing LF-to-CRLF working-copy warnings.

Tests used a disposable PostgreSQL 16 container bound only to loopback port `15439`, named `decagent-guest-queue-test-20261022-01`. It was started for this test run and is stopped/removed after verification. No existing application or database container was used.

## Boundaries

These tests use local synthetic data and make no Qwen, Jev, OpenRouter, GitHub, or benchmark-provider calls. They verify one new lease generation is issued under concurrent recovery and the previous generation is fenced; this is queue/lease evidence, not a claim that the complete guest product or public deployment is ready. Guest entry remains disabled by default. IP/request rate limiting, guest UI polish, Compose deployment, private CD, and live-provider validation remain open tasks.
