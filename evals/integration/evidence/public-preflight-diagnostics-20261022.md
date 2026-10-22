# Verified model preflight diagnostics evidence

Date: 2026-10-22
Branch: `feat/conditional-evaluation`
Base HEAD before this uncommitted slice: `8373c73`

## Purpose

The public runner could prove that some local model-client failures happened before HTTP dispatch, but it retained only a generic error category. This slice carries a safe exception class and allowlisted stage into the existing attempt ledger so operators can distinguish setup/input failures without seeing request content.

## Change

- The isolated primary worker returns an exception class name only for a verified preflight failure. It never returns the exception message.
- `PrimaryRequestNotDispatched` accepts only a bounded Python identifier for its diagnostic type. Invalid values are discarded.
- The ledger records `model_worker_request_pre_send`, a finite preflight reason, and the sanitized exception type on the `not_sent` attempt. `http_status` remains null.
- A verified `not_sent` attempt releases its reservation and leaves the route running. The failure does not become an unknown provider request.
- Historical candidate `f1a4a70` attempts and reports remain unchanged. No cohort was replayed.

## Verification

The new assertions failed before the code change: the worker omitted `ValueError`, and the ledger path had no diagnostic arguments. After the change:

- Focused public model, cancellation, ledger, and diagnostic suites: **120 passed**.
- Current focused re-run across public model dispatch, cancellation, trial ledger, diagnostics, and execution: **138 passed in 25.91s**; the public release-boundary regression passed separately (**1 passed in 0.10s**).
- CI lint scope, `ruff check decagent evals ops`: **passed**.
- `openspec validate --all --strict`: **6 changes passed, 0 failed**.
- `git diff --check`: **passed**; Git reported existing Windows LF-to-CRLF conversion notices.

No Qwen, Jev, MCP business tool, τ simulator, or official scorer request was sent during this verification.

## Limits

This improves future local preflight attribution only. It cannot recover details hidden by immutable historical attempts, determine whether an earlier dispatched request reached its provider, or complete the real four-domain benchmark. OpenSpec task 9.6.F remains incomplete.
