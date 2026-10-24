# OpenSpec implementation and evidence audit

- Date: 2026-10-24 (Asia/Shanghai)
- Audited changes: `guest-sop-local-trial` and `jev-conditional-workflows`
- Code candidate: local working tree based on `5b43a45962ee16bc0f61015d1063ee9ad6c676fe`; this session's code/test changes are not committed
- Model, Jev, benchmark, or GitHub deployment/dispatch requests in this audit: 0
- GitHub installation lookup: attempted through the read-only connector but returned a transport error before repository or installation data was available; no browser UI change or GitHub mutation was made

## Validation

| Check | Result |
| --- | --- |
| `openspec validate guest-sop-local-trial --strict --type change --no-interactive` | Valid |
| `openspec validate jev-conditional-workflows --strict --type change --no-interactive` | Valid |
| Relative Markdown links in `docs/` and both active changes | 147 checked; no missing local targets; the audit report's own evidence link also resolves |
| `git diff --check` | Passed |

These checks validate change artifacts and links; they do not imply every implementation task in either change is complete.

## Current status

| Change | Completed | Open | Open task IDs |
| --- | ---: | ---: | --- |
| `guest-sop-local-trial` | 24/28 | 4 | 2.5, 4.4, 5.3, 5.4 |
| `jev-conditional-workflows` | 72/79 | 7 | 9.6.D, 9.6.F, 9.6.G, 10.3, 10.4, 10.5, 10.6 |

The two-guest browser workflow and its test evidence are recorded in [guest two-workspace workflow acceptance](guest-two-workspace-workflow-20261024.md). It satisfies guest task 6.1 on the local candidate only. It is not a hosted-CI result, Jev evaluation, public benchmark result, employee study, or deployment rehearsal.

## Evidence classification

- **Passed:** the local two-guest business, memory, quota, approval, worker-restart and write-isolation E2E; relevant PostgreSQL and deterministic test results; installed-wheel MCP protocol check; strict validation of both changes; and the scoped local Markdown-link audit. Exact commands, counts, and skips are in the linked 6.1 evidence.
- **Previously failed/unknown:** the frozen v10 public first wave remains 3 failed, 5 unknown, 0 successful routes; its only official score was 0.0. Unknown attempts were not replayed in this audit. This historical result is not changed by the guest E2E.
- **Evidence insufficient:** Jev's incremental benefit over the optimized deterministic-plan baseline, a qualified confidence threshold, the 95% automatic-accuracy target, real employee usability, and full public benchmark performance have not been established.
- **Still pending:** guest tasks 2.5 (real Jev suggestion-to-reply browser flow), 4.4 (persistent Docker install/upgrade/rollback and restart recovery), 5.3 (private App-authenticated scope and dispatch verification), and 5.4 (trusted GHCR-to-local deployment/rollback); Jev tasks 9.6.D/F/G and 10.3–10.6; public EnterpriseOps-Gym/τ-Bench completion; and real employee research. The GitHub page is open in the user's browser, but the browser-control inventory still exposes no Edge tab, so this session did not inspect or change that page.

No result in this audit authorizes replaying historical unknown calls or claiming a Jev cost/latency advantage. Any new model cohort remains a separate run with its own budget and authorization gate.

## 2026-10-24 GitHub deployment-boundary recheck

This read-only check used GitHub CLI after the earlier connector lookup failed:

- Public repository `shuaibilx/DecAgent` reports zero Actions runners. Its release workflow publishes images only for semantic-version tags after full CI and dispatches to the private deployment workflow only when `DECAGENT_PRIVATE_CD_ENABLED == 'true'`.
- The public repository has the deployment App client-ID variable and private-key secret configured, but `DECAGENT_PRIVATE_CD_ENABLED` is absent. Therefore the private dispatch job is currently gated off. Secret values were not read or recorded.
- Private repository `shuaibilx/DecAgent-deploy` is private, has the fixed `.github/workflows/deploy-sop.yml` workflow, and reports one online Windows runner with labels `self-hosted`, `Windows`, `X64`, and `decagent-deploy`.
- The private deployment repository currently has no Actions run records. An online runner and a checked-in workflow prove setup state only; no image pull, service deployment, health check, or rollback was performed. No CD flag was changed and no service was started.

This confirms the public-repository-to-private-runner isolation boundary is in place while CD remains disabled. Tasks 5.3 and 5.4 still require end-to-end dispatch and trusted image deployment/rollback rehearsal.
