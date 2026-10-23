# Public first-wave dry-run preflight

Date: 2026-10-24 (Asia/Shanghai)

## Result

The `first-wave --check-images` preflight passed on code HEAD `edd30f1e60a10a21eebd3d41bced20d6bc8b4063` with status `preflight_passed_no_execution`. It selected the four frozen first-wave tasks (EnterpriseOps CSM/ITSM and τ-Bench retail/telecom) and their eight ReAct/Plan route slots. The broader smoke denominator remains 24 tasks / 48 route slots.

The v10 manifest digest is `1096d0515168eab71b06cb6b9bde558a0fa2a5269ec723aef996886a342b2fed`; the role-profile digest is `4fa091d2840928bfb6a00dccfbd452a2dee8c42c465512955fd63923eb0a6721`. Source-manifest and task-inventory digests are recorded in the machine report. Required local images were present.

No execution ledger was created. The preflight made 0 model calls and 0 MCP calls. It did not run routes, call the official scorer, or create a new live-batch authorization. The previous v10 live cohort remains terminal at 3 failed, 5 unknown, 0 successful, with cleanup verified for 8/8 and one official score of 0.0; unknown routes were not replayed. The official benchmark remains 0/286 scored tasks.

## Reproduction

Run from the DecAgent repository with the `Agent311` Python environment:

```powershell
python evals/benchmarks/run_public_trials.py --phase first-wave --check-images --report evals/benchmarks/evidence/public-first-wave-preflight-20261024-edd30f1.json
```

The immutable machine output is [`public-first-wave-preflight-20261024-edd30f1.json`](public-first-wave-preflight-20261024-edd30f1.json). Its recorded batch ceilings describe the existing v10 configuration; this dry-run neither renews nor authorizes spending. A future live run still requires a clean candidate, same-SHA CI, all runtime gates, a newly frozen budget, and separate authorization.
