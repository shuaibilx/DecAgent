# SOP-Agent bounded diagnostic round 2

- Date: 2026-10-24
- Candidate: `907017651e72a2a5d295006f4858f0055d1b9cea`
- Candidate CI: [GitHub Actions run 40078042176](https://github.com/shuaibilx/DecAgent/actions/runs/40078042176), 12/12 jobs passed
- Machine report: `.decagent/evaluation/evidence/sop-agent-diagnostic-round2-9070176.json` (local ignored evidence; SHA-256 `0b610aa5cfe4cddf7762a2b85707f1a28ce277e35592b663e94bd7d2d63b1750`)

## Result

The bounded Qwen run ended after 180.34 seconds when the Agent-role request cap of 3 was reached. It dispatched 3 Qwen requests, all with known usage: 34,054 input-plus-output tokens, one MCP tool call, and ¥0.0433172 estimated gross API cost under the frozen profile. The estimate is based on provider usage and the frozen rate; it is not a provider invoice or proof of cash paid. There were no usage-unknown attempts and no Jev calls.

Of 8 fixed route slots across four exposed development tasks, 3 reached terminal state and 5 remained pending. No route completed successfully. Two CSM routes reached the EnterpriseOps original verifier and both returned `success=false`; their environments were cleaned. The terminal records are not a performance comparison, and the three terminal `failed` statuses should not be interpreted uniformly as task-quality failures.

| Domain and route | Result | Model requests | MCP calls | Official score | Cleanup | Evidence |
|---|---|---:|---:|---|---|---|
| CSM Plan | Failed | 1 | 0 | Scored, false | Verified | `ModelCallError`, response validation stage; 11,788 input and 8,192 output tokens |
| CSM ReAct | Failed | 2 | 1 | Scored, false | Verified | First response produced one successful read-only `find_user`; second response was malformed |
| ITSM Plan | Failed in the old ledger | 0 | 0 | Not attempted | Verified | Reached the route after the 3 Agent requests were consumed; its attempt was rejected at admission with `ValueError` |
| ITSM ReAct | Pending | 0 | 0 | Not attempted | Pending | Not dispatched |
| τ retail Plan and ReAct | Pending | 0 | 0 | Not attempted | Pending | Not dispatched |
| τ telecom Plan and ReAct | Pending | 0 | 0 | Not attempted | Pending | Not dispatched |

The 8,192-token CSM Plan response exactly met the frozen output limit, but the saved evidence does not include provider finish reason or response body. Truncation is plausible, not established; no prompt or output was changed based on that assumption. The ITSM attempt did not exercise an ITSM model or tool contract. Its old `failed` status remains immutable and is not evidence that the ITSM task or MCP contract failed.

## Interpretation and limits

This diagnostic did **not** pass the required Plan → MCP → official-score → cleanup closure: no Plan route completed the full execution path. Do not start the 24-task pilot from this result, do not replay any route, and do not include this run in ReAct/Plan or Jev performance estimates. It identifies a CSM Plan response-validation failure, a malformed second CSM ReAct response, and a scheduler/accounting defect that mislabeled an unadmitted ITSM route as a task failure.

The five pending routes remain pending. The earlier v10 result of 0 successes, 3 failures, and 5 unknowns remains a separate immutable development cohort; neither cohort is a benchmark score. The round-2 Qwen allowance is spent for this run. The separately authorized OpenRouter diagnostic has not been dispatched; it requires its own frozen model profile, ledger and provider-specific admission path before any request.
