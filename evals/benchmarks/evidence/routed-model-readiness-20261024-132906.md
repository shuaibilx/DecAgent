# SOP-Agent routed model readiness v2

**Run date:** 2026-10-24 13:29:06–13:30:03 UTC
**Candidate:** `8acbe767ce1d7091c7ce23760c630bd34002ad94`
**Same-SHA CI:** [run 40097640965](https://github.com/shuaibilx/DecAgent/actions/runs/40097640965), 12/12 jobs passed
**Machine-readable evidence:** [routed-model-readiness-20261024-132906.json](routed-model-readiness-20261024-132906.json)

## Result

The authorized batch passed through OpenRouter using `deepseek/deepseek-v4.1-flash`. Qwen `qwen3.8-flash` passed connection and the minimal tool-parameter contract, but its read-only plan proposal failed the frozen plan compiler with the safe error class `PlanProposalInvalid`. The runner then tested OpenRouter as an independent provider. Its connection, tool contract, read-only production compiler, and streaming response/usage checks passed. The planner's format-repair retry was disabled; no same-provider retry was issued.

| Provider | Requests | Tokens | Frozen-rate gross API estimate | Result |
| --- | ---: | ---: | ---: | --- |
| Qwen | 3 | 14,459 | ¥0.0137883 | Plan did not compile |
| OpenRouter DeepSeek | 4 | 16,457 | $0.0121728 | Readiness passed |
| **Total** | **7** | **30,916** | Separate currencies | **Qualified provider: OpenRouter** |

Elapsed time was 56.433 seconds. All seven attempts settled with known token usage; there were no unknown attempts, no replay, no business MCP calls, and no official scorer calls. The tool-contract phase only validated a fixed readiness tool proposal (`tool_executed=false`). This batch does not measure end-to-end task success, compare Agent strategies, or establish a Jev advantage.

The provider did not return a separately recorded billed-cost field. The amounts above are standardized gross API estimates from actual token usage and the frozen rates, before any voucher or account-credit deduction; they are not provider invoices or cash-paid amounts. Qwen input is estimated at the uncached upper-bound rate.

## Request trace

| Provider / role | Tokens | Latency | Server request ID | Outcome |
| --- | ---: | ---: | --- | --- |
| Qwen / connection | 1,303 | 4,433 ms | `d2bcf501-2e4e-9b23-b59f-0df68f7af813` | Passed |
| Qwen / tool contract | 407 | 4,164 ms | `305c9993-f65d-9144-a266-e9e5d2e2e0b7` | Passed; tool not executed |
| Qwen / planning | 12,749 | 21,226 ms | `4ac45ce5-444c-9f0e-bfbe-0762fd8498e9` | `PlanProposalInvalid` |
| OpenRouter / connection | 1,141 | 3,530 ms | `gen-1792848579-ZTkROjbCv0BGck1v9tj9` | Passed |
| OpenRouter / tool contract | 379 | 3,691 ms | `gen-1792848582-gKnmJTueaifa2d5S8u4e` | Passed; tool not executed |
| OpenRouter / planning | 13,755 | 14,200 ms | `gen-1792848586-evc4nUmE6hrLSZKYrZX1` | Two-node read-only plan compiled |
| OpenRouter / stream | 1,182 | 4,363 ms | Not returned | Passed with usage |

The machine report's per-phase `resolved_models` field aggregates attempts sharing a role across providers. For provider attribution, use the per-attempt `provider`, `requested_model`, and `resolved_model` fields: each Qwen attempt resolved to `qwen3.8-flash`, and each OpenRouter attempt resolved to `deepseek/deepseek-v4.1-flash`. No cross-provider execution occurred. The per-attempt ledger is authoritative.

## Gate and next step

This run establishes generic OpenRouter model readiness for this diagnostic profile, but it did not record the provider-selection policy. It therefore does not qualify the Fireworks-pinned public profile, does not qualify Qwen, and does not open any public pilot gate. The full public benchmark remains `0/286`; no Jev comparison has run.

### Post-run gate status

The v2 evidence above is immutable and cannot be upgraded by adding routing metadata after the fact. The local 10.3.B implementation now requires v3 evidence whose durable ledger and report bind the full frozen cohort fingerprint, primary profile digest, per-provider request profiles, and exact price identity; each OpenRouter attempt must record its actual Fireworks route and matching request-profile fingerprint. The cost digest must bind the recorded rate envelope, which must cover every public role. Qwen unknown remains unresolved and reserved, but a fully successful, independently metered OpenRouter qualification can stand on its own; any unresolved OpenRouter attempt prevents that provider from qualifying. Regression tests reject metadata-only v2 promotion and profile/attempt drift. At evidence capture time, these changes were uncommitted and had not passed same-SHA CI; no v3 readiness or business route had run. Before a public comparison, complete the matched v3 readiness and a separate real development-task Plan→MCP→official-score→cleanup route. Keep provider groups and ledgers separate; readiness is not a benchmark result.
