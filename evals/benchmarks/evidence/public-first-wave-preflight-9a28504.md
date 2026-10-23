# Public first-wave dry-run preflight

Date: 2026-10-24 (Asia/Shanghai)

## Result

The `first-wave --check-images` preflight passed on candidate `9a28504439c28bbd35006e860c78801ac4183c82` with status `preflight_passed_no_execution`. The exact candidate's GitHub Actions CI run [40041663757](https://github.com/shuaibilx/DecAgent/actions/runs/40041663757) completed successfully with all 12 jobs green, including Python 3.11/3.13 checks, PostgreSQL integration shards, Chromium lifecycle E2E, Docker boundaries, wheel/MCP verification and image build.

The preflight selected the four frozen first-wave tasks: EnterpriseOps CSM/ITSM and τ-Bench retail/telecom, with eight ReAct/Plan route slots. The broader smoke denominator remains 24 tasks / 48 route slots. The v10 manifest digest is `1096d0515168eab71b06cb6b9bde558a0fa2a5269ec723aef996886a342b2fed`; the model-profile digest is `4fa091d2840928bfb6a00dccfbd452a2dee8c42c465512955fd63923eb0a6721`. The source-manifest and inventory digests are recorded in the machine report. Digest-pinned EnterpriseOps images were present locally.

No ledger was created. This preflight made 0 model calls and 0 MCP calls. It did not execute routes or call the official scorer, and it does not renew the expired v10 authorization. The v10 cohort remains 3 failed, 5 unknown and 0 successful; cleanup was verified for 8/8 routes, with one official score of 0.0. Unknown routes were not replayed. The Jev comparison remains outside these eight ReAct/Plan slots.

## Reproduction

Run from the DecAgent repository with the `Agent311` Python environment:

```powershell
python evals/benchmarks/run_public_trials.py --phase first-wave --check-images --report evals/benchmarks/evidence/public-first-wave-preflight-9a28504.json
```

The immutable machine output is [`public-first-wave-preflight-9a28504.json`](public-first-wave-preflight-9a28504.json). Its batch ceilings are configuration only; this dry-run did not create a ledger or authorize spending. A live cohort still requires a newly frozen budget and separate authorization.
