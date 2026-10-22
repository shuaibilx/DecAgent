# v10 first-wave preflight evidence

- Candidate: `d3e000692999c813c2ea084fe4dfa17c4f08aa05`
- GitHub Actions: [run 39782622299](https://github.com/shuaibilx/DecAgent/actions/runs/39782622299), all 9 jobs succeeded; wall time 9m19s.
- Trial manifest SHA-256: `1096d0515168eab71b06cb6b9bde558a0fa2a5269ec723aef996886a342b2fed`.
- Model profile set SHA-256: `4fa091d2840928bfb6a00dccfbd452a2dee8c42c465512955fd63923eb0a6721`.
- Machine-readable preflight: [`public-first-wave-preflight-20261022-d3e0006.json`](public-first-wave-preflight-20261022-d3e0006.json).

The successful preflight verified local pinned images and selected the four frozen first-wave tasks (two EnterpriseOps-Gym and two τ-Bench) with eight ReAct/Plan route slots. It retained the full smoke denominator of 24 tasks and 48 route slots. The result was `preflight_passed_no_execution`: no ledger was created, and there were zero model or MCP calls. The result is engineering and admission evidence only; it is not a benchmark score, a live execution, or authorization for a new live batch. Jev remains outside these eight baseline routes pending a naturally occurring, reviewed semantic decision.

Local verification before this same-SHA CI run: 2,122 deterministic tests passed and 18 were skipped; Ruff passed; OpenSpec strict validation passed for all 6 changes. GitHub Actions also passed Python 3.11/3.13 deterministic checks, four PostgreSQL integration shards, Docker/SQL boundary tests, Workbench browser checks, wheel/MCP protocol checks, image builds, and offline Compose startup.

The primary model timeout is 120 seconds in v10; user-simulator and judge timeouts remain 60 seconds, and Jev remains 10 seconds. Historical v9 route states and unknown reservations are unchanged and must not be replayed. The next live evaluation still requires separate batch authorization and fresh persisted budget.
