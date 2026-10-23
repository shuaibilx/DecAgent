# Guest Jev fee profile verification

Date: 2026-10-23 (Asia/Shanghai)

## Frozen profile

The guest trial route is pinned to OpenRouter model typesafe/jev-1.14. OpenRouter's model page lists an input price of $0.042 per million tokens, free output, and a 262,144-token context window: [Jev 1.14 model and pricing](https://openrouter.ai/typesafe/jev-1.14/).

Under this dated fee profile, pricing the entire input context gives:

```text
262,144 × $0.042 / 1,000,000 = $0.011010048
```

Each guest attempt reserves at least $0.02 before dispatch. The requested model, provider, and endpoint must exactly match typesafe/jev-1.14, openrouter, and https://openrouter.ai/api/alpha/decisions; the price-basis ID is openrouter-typesafe-jev-1.14-2026-10-23. Jev aliases, other providers, a reservation below the calculated full-context fee, and requests whose serialized-input estimate plus framing reserve exceeds 262,144 are rejected before either ledger reservation or HTTP dispatch. The budget derives the estimate from the exact requested model, state, and questions in the request.

The estimate guard is an application-side request-size check; the fee ceiling itself uses the pinned model's published context limit and frozen price. This does not rely on provider.max_price, whose hard-cap behavior is not established for OpenRouter's Decisions API. Actual returned usage.cost remains the settlement source. Unknown usage retains the full reservation and blocks further model calls in that task. Employee Jev configuration is unchanged.

This is a dated pricing profile, not an OpenRouter-enforced per-request price cap. If the upstream price, model context, model identity, or route changes, update and re-verify the profile before enabling guest Jev. The guest feature flag remains off; this verification does not complete the real Jev workbench/E2E task.

## Verification

- evals/deterministic/test_guest_jev_pricing.py: 4 passed.
- evals/integration/test_business_guest_trial_budget.py: 38 passed, 0 skipped on an isolated, temporary PostgreSQL 17 container bound to loopback.
- The combined test command reported 42 passed in 17.82 seconds.
- After correcting employee `jev-latest` binding to the adapter's OpenRouter alias, the broader Jev, business-service, conditional-decision, and guest-budget regression set reported 193 passed in 194.57 seconds. This rerun includes the guest fee-bound cases and verifies that employee alias normalization does not weaken the guest profile pin.
- Ruff passed for the touched Jev profile, adapter, budget, storage, and related test files.
- Integration HTTP used httpx.MockTransport; there were no live Jev or other provider requests and no provider charges.
- Coverage includes the successful pinned profile, recorded $0.02 reservation and mock settlement, and pre-dispatch rejection for a mutable alias or insufficient reservation.

The earlier [guest Jev fee-bound gate record](guest-jev-fee-bound-20261022.md) documents the previous fail-closed state. Its finding that provider.max_price is not a Decisions API hard-cap contract remains valid; the new gate uses the pinned model's dated published rate and context profile instead.
