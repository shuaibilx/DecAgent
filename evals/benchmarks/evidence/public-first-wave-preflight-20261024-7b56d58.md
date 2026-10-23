# Public benchmark first-wave preflight

Date: 2026-10-24 (Asia/Shanghai)

## Result

The fixed `first-wave --check-images` preflight passed against code SHA
`7b56d588682a36d8ea2171890676af3e6f45ac3c` with status
`preflight_passed_no_execution`. It selected the frozen EnterpriseOps CSM/ITSM
and τ-Bench retail/telecom cases, with eight ReAct/Plan route slots. The full
smoke cohort remains 24 tasks / 48 route slots.

The manifest digest is
`1096d0515168eab71b06cb6b9bde558a0fa2a5269ec723aef996886a342b2fed`; the
profile digest is
`4fa091d2840928bfb6a00dccfbd452a2dee8c42c465512955fd63923eb0a6721`. Source
manifest and task inventory digests are in the machine report.

No trial ledger was created. The preflight made zero model calls and zero MCP
calls. It did not run routes, call the official scorer, or renew the expired
v10 batch authorization. The previous v10 cohort remains terminal at 3 failed,
5 unknown, and 0 successful routes; cleanup is verified for 8/8. Unknown routes
were not replayed. The official benchmark remains 0/286 scored tasks.

## Reproduction

Run from the DecAgent repository with the `Agent311` environment:

```powershell
python evals/benchmarks/run_public_trials.py --phase first-wave --check-images --report evals/benchmarks/evidence/public-first-wave-preflight-20261024-7b56d58.json
```

The immutable machine output is
[`public-first-wave-preflight-20261024-7b56d58.json`](public-first-wave-preflight-20261024-7b56d58.json).
The same-SHA GitHub Actions run [40014153430](https://github.com/shuaibilx/DecAgent/actions/runs/40014153430) later passed all 12 jobs. This validates the code candidate under CI; it does not execute benchmark routes or grant live-run authorization.

The printed batch ceilings are configuration limits, not a new live-run
authorization. Any next live batch requires a clean candidate, same-SHA CI,
runtime gates, a newly frozen budget, and separate authorization.
