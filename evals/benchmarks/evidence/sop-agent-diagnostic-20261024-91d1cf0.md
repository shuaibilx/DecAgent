# SOP-Agent bounded Qwen diagnostic

**Result:** readiness gate not passed. This is a bounded diagnostic, not the frozen 24-task pilot or a performance comparison.

## Frozen candidate and run

- Candidate: `91d1cf0e25f87d3f74eb83674f6257c8004b8a98`.
- Same-SHA [GitHub Actions run 40068945541](https://github.com/shuaibilx/DecAgent/actions/runs/40068945541): final attempt 2 passed all 12 jobs. The initial attempt had one failing Python 3.13 PostgreSQL integration shard; rerunning failed jobs passed.
- Scope: four fixed, previously exposed development tasks and eight ReAct/Plan route slots. No 24-task manifest was frozen.
- The phase ran for 64.455 seconds. The original batch window was 2026-10-24 13:44:35–14:29:35 China Standard Time; execution stopped at 14:25:31 when the Agent-role Qwen request cap was reached. That window is closed and must not be resumed or extended.

## Observed outcomes

| Measure | Result |
| --- | ---: |
| Selected tasks / route slots | 4 / 8 |
| Terminal routes | 2 failed |
| Pending, unattempted routes | 6 |
| Successful plan→MCP→official-score routes | 0 |
| Qwen requests / reported tokens | 3 / 22,191 |
| Qwen usage-unknown attempts | 0 |
| Frozen-profile pre-credit API fee estimate | ¥0.0201905 |
| MCP tool calls | 2 |
| Jev / simulator / judge requests | 0 / 0 / 0 |

The only route receiving a score was CSM/ReAct. It failed model response validation with `agent_failure_malformed_response`; the EnterpriseOps original verifier returned 0.0. It used three Qwen requests and two tool calls (`find_user`, classified read, and `find_contact_by_portal_user`, classified write by registered metadata). Cleanup was verified. The forbidden-side-effect count was not measured.

The CSM/Plan route ended as failed at `agent_execution` with zero model and zero tool calls because the per-role Agent request cap had already been consumed by ReAct. It received no score; cleanup was verified. The remaining six route slots were not dispatched. The batch stop reason was `agent_qwen_requests`.

The ¥0.0201905 amount is a frozen-profile estimate from known token usage, not a provider invoice or a claim about voucher credit or cash paid. No attempt had unknown token usage. There was no Jev measurement, no completed ReAct/Plan pair, and no latency or cost comparison with a valid business-success route.

## Interpretation and next step

This diagnostic did not validate plan generation followed by MCP execution and official scoring. The ReAct-first scheduler consumed the three-request Agent-role allowance before the Plan route could dispatch. The offline correction gives the diagnostic-only scheduler Plan-first ordering per task; normal comparison ordering and the frozen task denominator remain unchanged. A regression test exercises the actual phase scheduler.

The previous authorization has ended. Do not rerun or resume its ledger. The correction must pass the full local checks and same-SHA CI before a new diagnostic window and explicit authorization can be considered. The production qualification target remains at least 95% automatic-decision accuracy with at least 100 automatic samples per decision family.

Machine evidence was written under the ignored local state path `.decagent/evaluation/evidence/sop-agent-diagnostic-20261024-91d1cf0.json`; prompt and verifier details are not included here.
