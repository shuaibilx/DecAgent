# Public first-wave preflight after offline diagnostics

- Date: 2026-10-22 (Asia/Shanghai)
- Candidate: `8b8d4c2be7d0d1cee8819f22742a9f8201328960`
- Manifest digest: `f984ec74c9776efef0b6ba808ff035198500f1997537e01776dc2a895173a604`
- Model profiles digest: `abfa2d06c4be59580726711a5d9325f9e063daff34e2fb83dad75b5e479e50a4`

The first-wave selector and digest-pinned EnterpriseOps image check completed successfully with status `preflight_passed_no_execution`. It selected the same four fixed cases—EnterpriseOps CSM and ITSM plus τ-Bench retail and telecom—and eight ReAct/Plan route slots. The enclosing smoke set remains 24 tasks / 48 routes; this preflight selected only 4 / 8.

The report records `ledger_created=false`, `live_model_calls=0`, and `mcp_calls=0`. It verifies local image availability and frozen selection only; it is not a model-quality result, official score, or authorization to replay historical `unknown` attempts.

The same candidate SHA passed all nine GitHub Actions CI jobs in **12m42s**: [run 39773178389](https://github.com/shuaibilx/DecAgent/actions/runs/39773178389). This verifies code and package/container checks for this candidate; it does not authorize a new billable cohort.

The evidence-only follow-up commit `06b390328c5eb3a089f08b68899dd8f6d285c5b4` also passed all nine CI jobs in **10m18s**: [run 39775263485](https://github.com/shuaibilx/DecAgent/actions/runs/39775263485). That commit changed documentation and evidence only; it did not change runtime code.

Command:

```powershell
D:/Anaconda3/envs/Agent311/python.exe evals/benchmarks/run_public_trials.py --phase first-wave --check-images --report evals/benchmarks/evidence/public-first-wave-preflight-8b8d4c2.json
```

Machine-readable output: [`public-first-wave-preflight-8b8d4c2.json`](public-first-wave-preflight-8b8d4c2.json).
