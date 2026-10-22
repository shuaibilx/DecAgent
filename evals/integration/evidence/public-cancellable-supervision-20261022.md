# Public evaluation cancellation and supervision verification

- Date: 2026-10-22 (Asia/Shanghai)
- Candidate worktree HEAD: `f994fdcdf72e73f6a94b3823e4399c4176b7fbf8`; worktree was dirty.
- Provider/API requests: none. All tests used local targets, fake transports, or ledger fixtures.

## Verification

On the Windows development host, the following focused suites passed:

```text
evals/deterministic/test_public_cancellable_model.py
evals/deterministic/test_business_public_execution.py
evals/deterministic/test_business_public_trials.py
100 passed in 18.02s
```

The suite exercises the one-shot worker deadline, worker/descendant cleanup, supervisor exclusivity while a dispatched child remains active, bounded model-worker diagnostics, public route execution lifecycle, and the rule that an unresolved dispatched provider attempt remains `unknown` and cannot be replayed. Added a serialization-boundary regression: non-finite JSON and invalid UTF-8 surrogate results now produce a bounded `response` diagnostic instead of being misclassified as a generic `target` exception. Ruff passed for the modified implementation and test. No production Qwen, DeepSeek, Jev, MCP, or benchmark scorer was contacted.

An additional offline integration test sends one OpenAI-compatible request from `OneShotPrimaryModel` to a temporary loopback HTTP server, parses the response through the installed client stack, and settles `LedgeredModelCallback` with the returned usage, model identity, provider request ID, and frozen CNY rates. The combined focused suites (`test_public_model_dispatch.py`, `test_public_cancellable_model.py`, `test_business_public_execution.py`, and `test_business_public_trials.py`) passed: `127 passed in 22.71s`; Ruff passed for the touched implementation and test files. The fixture verifies the reserve/dispatch/settle path without contacting a model provider; its CNY amount is a frozen-rate estimate, not a provider invoice.

Follow-up verification added a loopback call through the full `PublicTauModelCallback` path: the real one-shot child and installed OpenAI-compatible client sent a tool schema, parsed a registered simulator tool call, and preserved model identity, request ID, and usage. The focused suite group then passed `128 passed in 25.90s`; Ruff passed. This still uses a local fake HTTP server and does not explain the historical v9 `model_worker_target/ValueError`.

A follow-up assertion then exposed an identity-priority bug: when both the HTTP `x-request-id` header and the OpenAI-compatible response body's `id` were present, the adapter persisted the body ID as `provider_request_id`. The local fixtures now assert that the HTTP request identifier takes precedence, with the body ID retained only as a fallback; the Qwen Chat Completions response is not mislabeled as a Jev-style `generation_id`. Both loopback paths first failed against the old precedence and passed after the fix. The focused four-suite group passed again (`128 passed in 25.43s`), and Ruff passed. No provider request was made.

The later full Windows/Python 3.13 deterministic run exposed one flaky deadline in the malformed-response loopback regression: its test-only three-second timeout could expire during worker startup before the assertion reached the provider-response classification. The test passed by itself and as a file; its fixture deadline is now ten seconds. The affected file passes (`24 passed in 13.93s`), and the complete deterministic suite passes (`2024 passed, 18 skipped, 0 failed in 277.80s`). This does not change production deadline behavior. No provider request was made.

## Limits

This verifies deterministic cancellation, a local model-client HTTP/ledger path, and ledger behavior only. It does not explain the historical v9 `ValueError` recorded at `model_worker_target`; the old evidence stores only a safe exception type, and the originating worker no longer exists. It also does not demonstrate a successful live model callback, official benchmark score, or four-domain route closure. Keep 9.6.F and 9.6.G unchecked, preserve the v9 unknown attempts, and require a separately frozen and authorized cohort before any new provider request.

## Unknown tool elapsed-time evidence (2026-10-22)

The frozen f1a4 CSM `find_user` tool attempt retained `unknown`, `tool_gateway`, and `NodeFailure`, but its `duration_seconds` was null. The ledger had a duration column, while the unknown settlement path did not persist the gateway's already measured elapsed time. A regression first reproduced the missing duration, then the implementation was corrected to save finite nonnegative elapsed time for both transport exceptions and post-dispatch deadline failure. The status remains `unknown`, no result digest is added, and automatic replay remains blocked. Historical f1a4 evidence is not rewritten.

Validation: focused deterministic public trial/execution/route suites `150 passed`; Ruff passed. No live model, MCP, benchmark or scorer request was made.
