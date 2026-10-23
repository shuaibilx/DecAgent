# Next public cohort: usage evidence and proposed stop lines

**Status:** estimate only; no new model requests are authorized by this note.

## Evidence from v10

The frozen v10 cohort used candidate `9d61688c52ee66435a22e18c4fa1c8348a70095a`, four fixed cases, and eight ReAct/Plan route trials. All eight reached a terminal status and all eight isolated environments reported verified cleanup. Three routes failed, five remained unknown, and none succeeded. Only τ telecom/ReAct reached an official score; it scored `0.0`. Jev was not called.

| Resource | v10 consumed or reserved | v10 hard cap | Share of cap |
| --- | ---: | ---: | ---: |
| Qwen requests | 30 (21 primary, 9 user simulator, 0 judge) | 500 | 6.0% |
| Qwen tokens charged to the batch ledger | 477,433 (220,086 returned usage + 257,347 unknown reservation) | 5,000,000 | 9.55% |
| MCP tool calls | 13 | 1,600 | 0.81% |
| Wall time at final report snapshot | about 772 seconds | 14,400 seconds | 5.36% |

The frozen Qwen profile calculates ¥0.1884416 for settled requests before voucher deductions. Four primary requests have unknown usage; their ¥0.2681368 reservation remains held. Their combined ¥0.4565784 is **settled profile calculation plus unknown reservation**, not a supplier invoice or actual incurred total. The provider billed amount for those unknown requests remains unknown. Two settled tool calls were classified as write-effect by tool metadata; one read call was unknown. Teardown passing does not establish that forbidden side effects were absent.

These eight unsuccessful routes cannot provide a defensible per-success estimate for the remaining 24 smoke cases or the 206+80 official cases. Full-smoke and full-benchmark cost/time forecasts therefore remain unestimated until a cohort produces valid scored completions.

## Proposed next diagnostic cohort

If separately authorized, create a **new independent cohort** on the same four fixed cases, with new cohort and trial identities, and clean isolated state. Do not resume, mutate, or release reservations from any v10 route; v10's five unknowns remain terminal. Run the same eight ReAct/Plan slots so each domain receives an official-score opportunity. Jev and cross-model fallback remain off. This is a diagnostic cohort, not a continuation of v10 and not a performance claim. The executable base `c2dad71ba3f4ac658a70851aa16b829d59ad9bae` has passing same-SHA CI, but this evidence-note commit will create a new repository SHA; the actual cohort candidate must be that new clean SHA after its own CI passes.

Proposed hard stops for that cohort:

| Resource | Batch cap | Role split |
| --- | ---: | --- |
| Qwen requests | 100 | Agent 60; simulator 32; judge 8 |
| Qwen input + output + reserved tokens | 1,000,000 | Agent 600,000; simulator 320,000; judge 80,000 |
| MCP tool calls | 400 | Shared batch cap |
| Wall time | 2 hours | Also retain the stricter per-route limit |
| Gross Qwen API cost ceiling | ¥2.70 | 1,000,000 tokens priced at the frozen maximum output rate of ¥2.70/M |

The cost ceiling is a conservative API-value ceiling under the frozen profile, before voucher deductions; it is not a cash-payment estimate. The cohort must stop at the first exhausted request, token, tool, or wall-time cap. Unknown usage keeps its reservation and terminates only that route; no route is replayed. A complete, scored four-domain cohort is a prerequisite to estimating larger smoke/full runs.

This note does not authorize the cohort. Before dispatch, freeze a new manifest, verify the final candidate and same-SHA CI, confirm all budget counters start in a new isolated ledger, and receive a separate authorization for the proposed cap.
