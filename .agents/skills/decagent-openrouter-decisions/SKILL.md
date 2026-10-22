---
name: decagent-openrouter-decisions
description: Apply DecAgent's OpenRouter Decisions integration rules when changing Jev request construction, model identity, provider routing, usage accounting, or guest dispatch limits.
---

# DecAgent OpenRouter Decisions

Use this project-specific reference when changing the Jev integration. Its constraints come from DecAgent's existing accounting and trust boundaries; consult the linked OpenRouter contract for current protocol details.

## Project rules

- Keep bounded semantic judgments in Decisions `noul`, `choice`, or `score`; keep arithmetic, permissions, quotas, and explicit business rules in code.
- Send only the state fields needed by the questions. Keep stable model identity, request ID, response model, token usage, latency, cost, and cost basis in the project trace.
- Pin a versioned/canonical model identity for evaluated runs. Do not silently switch to an alias or another provider inside a paired comparison.
- Route every actual attempt through the existing `UsageLedger` reserve → dispatch → settle/unknown lifecycle. Do not call the provider around its budget gate.
- Employee Jev may settle a confirmed OpenRouter `usage.cost` as provider-billed USD. Guest requests need a verified single-request fee bound before any reservation or HTTP dispatch; the current `$0.01` placeholder is not such a bound.
- The Decisions API contract currently shows a generic `provider` object, while the general provider-selection page documents `max_price`. Do not claim that this is a hard guest fee cap for `/api/alpha/decisions` without explicit endpoint-specific contract support and verified enforcement. Keep guest Jev fail-closed meanwhile.
- If a provider call has unknown usage or fee, keep its reservation and stop further model dispatch for that guest task. Only release a reservation after local evidence proves no request was sent.
- Test request serialization and refusal behavior with local mock transports. These tests do not prove a remote provider honors a routing limit. Do not make live model calls unless the user has authorized a fresh, bounded request batch.

## Official references

- [Submit a Decisions request](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request)
- [Provider selection and `max_price`](https://openrouter.ai/docs/guides/routing/provider-selection)
- [Decision models in the OpenRouter catalog](https://openrouter.ai/api/v1/models?output_modalities=decisions)
- [OpenRouterTeam upstream skill](https://github.com/OpenRouterTeam/skills/tree/33597a44e38b0cdc38ea1256ef8bcf50c2704e3d/skills/openrouter-decisions) (inspected but not vendored: repository has no declared license)
