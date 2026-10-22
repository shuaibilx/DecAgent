# Guest admission and queue UI verification

Date: 2026-10-22 (Asia/Shanghai)

## Isolated setup

- Started a disposable PostgreSQL 16 container named `decagent-guest-rate-test-20261022`, bound only to `127.0.0.1:15441`.
- Started `evals.integration.business_guest_browser_fixture` at `http://127.0.0.1:8025/`; the fixture creates and drops its own database and registers only the synthetic `service-tickets` capability.
- No Worker was started. No Jev, Qwen, OpenRouter, or other provider request was made. The submitted read-only directory task remained queued and was removed with the fixture database.

## Browser observations

- A new guest session showed the live budget date and caps: `$0 / $5` cost, `0 / 5,000,000` global Qwen tokens, and `0 / 30` guest tasks.
- Submitting one read-only directory task updated the guest task counter to `1 / 30`.
- The task list rendered its queue position; its detail view rendered the position, the reason `当前执行并发槽位暂满`, the 20-item global waiting cap, one active task per guest workspace, and two active tasks globally. In this follow-up browser run an earlier fixture task was already waiting, so the new task correctly showed position 2.
- Five new guest sessions from one browser address succeeded. The sixth attempt after clearing the browser cookies returned `429 guest_rate_limited`; the workbench displayed the returned `Retry-After` value in seconds. The browser console's corresponding 429 is the expected denial response.
- Selecting the queued compatibility task also caused the unrelated memory view to receive HTTP 409 `contract_changed` because the browser fixture does not provide a published conditional plan. This does not affect the quota, submission, or queue display checks; memory behavior was not evaluated here. The two browser console errors were these expected memory-view 409 responses; there were no warnings.

## Cleanup

Closed the Playwright browser and stopped the fixture server so its isolated database was dropped. The disposable PostgreSQL container was stopped after the integration suite. Existing DecAgent service containers and databases were not accessed.
