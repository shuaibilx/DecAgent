# Benchmark data and provenance

The benchmark sources have been downloaded into the local ignored cache at
`evals/benchmarks/vendor_cache/`. You do not need to download the task data by
hand. The cache is excluded by this directory's `.gitignore`; the project keeps
only the source manifest and task-ID inventory.

`manifests/source_manifest.json` pins repository revisions, licenses, source
archive checksums, database snapshots, MCP image digests, and the data paths used
for evaluation. `manifests/task_inventory.json` records task IDs and official
split membership without copying prompts, verifier SQL, reference actions, or
database state into the inventory. Rebuild the inventory after installing its
small optional reader dependency:

```powershell
python -m pip install -r evals/benchmarks/requirements.txt
python evals/benchmarks/build_task_inventory.py
```

## Task facets and model input boundary

Every pinned EnterpriseOps and τ-Bench task now has a compact task-facet record:
workflow-family estimate, explicit multi-step cues, configured tool names,
state-dependency cues, structured and semantic branch candidates, clarification
cues, and recovery cues. These are reproducible surface heuristics for coverage
analysis, marked `heuristic_unreviewed`; they do not claim hidden reference
trajectories or ground-truth branch labels. Tool names describe the configured
catalog, not a required call sequence.

The inventory records each source split and currently marks all selected sets
`not_run`. `agent_input.py` projects only EnterpriseOps prompts and configured
tool names. For τ-Bench, it accepts a live user-simulator turn plus policy and
transcript; it never accepts the hidden task object, because `user_scenario` is
provided to the simulator rather than the agent. Verifiers, target state,
reference actions and simulator instructions stay in the scorer/simulator path.

## Frozen evaluation scope

EnterpriseOps-Gym is the primary multi-step business workflow benchmark. The
current frozen comparison uses every CSM and ITSM row in the `oracle` mode:
103 CSM tasks and 103 ITSM tasks. The other official tool-set modes and their
task IDs remain in the inventory. The project README and dataset card publish
different total task counts, so scoring uses the exact row IDs and checksums in
the pinned Hugging Face revision rather than either headline number.

τ-Bench supplements the enterprise benchmark with conversational interaction.
The inventory preserves each domain's official `base`, `train`, and `test`
membership. The resource-limited supplementary run uses the official `test`
splits for `retail` and `telecom`, 40 tasks from each domain. Its full `base`
split remains available for later full evaluation. A separate deterministic
EnterpriseOps smoke subset uses the ten task IDs with the lowest SHA-256 rank
per selected domain; smoke results must not be presented as full benchmark
scores.

DecAgent also keeps a small internal workflow set at
`evals/benchmarks/business/frozen-v2.json` for contract and harness development.
It has 40 development cases and 80 holdout cases split by scenario family; the
same scenario template never appears on both sides. The earlier `frozen-v1.json`
is retained as history, but its scenario families overlap and it must not be used
to claim holdout generalization. This internal synthetic set supplements the
public workflow benchmarks and cannot replace them. Its labels are generated
references, not human-verified labels. The review checklist remains pending, and
the base set covers all three decision primitives through operation selection,
explicit-assignee detection, and work-blockage scoring.

A separate memory-decision supplement is frozen at
`evals/benchmarks/business/memory-decisions-v1.json`: 40 development and 80
holdout cases cover Jev Choice/Noul decisions for write, read-needed, and
relevance judgments. Scenario families are split-isolated, model input is an
allowlist that excludes labels and split metadata, and all labels remain
`synthetic-reference` with `human_reviewed=false`. The set is currently marked
`not_run`; it must not be used to claim measured memory accuracy or select a
runtime policy. The memory contract currently publishes Choice and Noul
templates; Score is covered by the base workflow set rather than these memory
decisions. Its review record remains pending, and memory-case label review is
still required before making any measured-accuracy claim.

Human review checklist for the internal labels:

- [ ] Verify all development and holdout scenario-family assignments.
- [ ] Check reference actions, terminal states, and forbidden writes per family.
- [ ] Check dynamic-candidate and ambiguity labels against the seeded directory.
- [ ] Independently review the new memory-decision supplement's references and scope boundaries.
- [ ] Record reviewer, date, dataset checksum, and any corrections in a new frozen dataset version.

```mermaid
flowchart LR
    A[Fixed upstream commits] --> B[Local ignored source cache]
    B --> C[Verify archive and file hashes]
    C --> D[Build task-ID and split inventory]
    D --> E[Select the frozen task IDs]
    E --> F[Agent receives task, policy and tools]
    E --> G[Scorer keeps verifiers and target state]
    F --> H[Run pinned MCP environment]
    H --> G
    G --> I[Report exact task denominator and status]
```

## Offline confidence-threshold analysis

Business decision exports can be converted into a content-addressed prediction
cache and scanned repeatedly without additional Jev requests. The scanner
requires the current program-owned question registry, rejects stale question
versions and holdout data, and only marks a threshold policy-eligible when the
labels come from a reviewed validation split with at least 100 automatic cases
and observed accuracy of at least 95%. Ties in automatic coverage select the
higher confidence threshold. Zero automatic cases retain null accuracy and
interval values.

The current `frozen-v2.json` labels are synthetic references. By the owner's
2026-10-18 decision, all internal sets are auxiliary regression evidence only:
they cannot select a threshold or runtime policy, and are never headline scores.
Saved predictions can produce diagnostic threshold curves. The
saved report contains one measured point per threshold and decision family,
including coverage, accuracy, Wilson interval, false interventions, missed
interventions, and unresolved answers; unavailable values stay null.

```powershell
.\.venv\Scripts\python.exe -m decagent.evaluation.business calibrate-cache --input development-export.json --output predictions.json
.\.venv\Scripts\python.exe -m decagent.evaluation.business threshold-scan --input predictions.json --output threshold-report.json --minimum-auto 100 --target-accuracy 0.95
```

These commands are offline. `calibrate-cache` accepts only a complete frozen
development export; a production policy requires a separately reviewed
validation cache. Every output is new-file-only and tied to the frozen dataset
checksum and current registered decision families.

To check sample sufficiency without collecting predictions or making model
calls, run the following command with a new output filename:

```powershell
.\.venv\Scripts\python.exe -m decagent.evaluation.business eligibility-diagnostic --output .decagent/evaluation-closeout/new-eligibility.json
```

This is cardinality-only evidence, not a measured threshold scan. The six
workflow families each have at most 40 development examples; memory write has
20, and memory read-needed/relevance each have 10. All are below the 100
automatic-sample minimum even under perfect predictions. Accuracy and chosen
threshold remain null; the holdout is not used for tuning.

## Evaluation cost accounting

Every live Jev request reserves against the shared cumulative $1 ledger before
HTTP dispatch. Pending or unknown usage blocks further Jev calls; the recorded
historical spend is retained when the ledger is first initialized. The report
keeps actual primary-model requests and token counts even when the owner uses a
free quota. Scripted fixtures make no provider calls, and replaying a saved
prediction cache has zero new model cost; neither is reported as live evidence.

## Local readiness

EnterpriseOps CSM and ITSM database snapshots are already extracted under the
ignored cache and mapped from the pinned Parquet rows. The MCP images are
resolved to immutable Linux/AMD64 manifest digests in the source manifest. Both
images were pulled by their exact digest on 2026-10-18, and the local provenance
gate passed. On a new machine, pull those exact `image@sha256` references, verify
the local image digests, and run the MCP wire smoke test before formal scoring.

```powershell
docker pull docker.io/shivakrishnareddyma225/enterpriseops-gym-mcp-csm@sha256:eaa456ac9aa85728426e7d3813a0bbca0949d6a8695be30e26f03894e6e6b189
docker pull docker.io/shivakrishnareddyma225/enterpriseops-gym-mcp-itsm@sha256:a234ae3fb7cee196ba25e6b9957969dea829919b6e8271dddae128f065aaf39f
.\.venv\Scripts\python.exe evals/benchmarks/validate_source_manifest.py --for-scoring
```

The pinned τ-Bench source reports version 1.0.1 and requires Python 3.12 through
3.13. Its `data/tau2/results/` directory is excluded from evaluation inputs.
Only `data/tau2/domains/` supplies task, policy, database, and split data. Some
task records contain verifier or reference information; the agent adapter must
not pass those fields to the model. Keep scoring and target-state checks in the
evaluation harness.

The source manifest is the gate for formal scoring: an unknown license, missing
snapshot, changed checksum, unresolved image digest, or unverified environment
must stop the run and leave the score unreported.

## Frozen model profiles and public comparison manifest

`manifests/evaluation_model_profiles.json`, v2, and comparison manifests
through v5 are preserved as historical, unrun inputs. The current cohort uses
`manifests/evaluation_model_profiles_v3.json`.
It freezes secret-free identities for planning, τ-Bench simulation, judging and
Jev, including endpoint, price provenance, retry/output limits, timeout,
provider routing, sampling-parameter support/omission, response mode and tool
choice. The profile matches the readiness model without embedding credentials.
The earlier Qwen zero-rate profile reflected a mistaken interpretation of the
account voucher. Qwen API usage is billed; the voucher offsets the bill. In v3,
Qwen rates and per-request costs remain explicitly `unknown` until the rate
applicable to the configured endpoint and deployment region is verified. Known
token usage remains reportable and does not become a zero cost. The route
callback can persist exact per-request gross API charges, voucher credits and
cash paid as separate original-currency evidence; missing values remain
unknown, account-window totals are never spread across requests, and non-USD
values are not converted without a verified rate. The Qwen endpoint's bill
records are not fetched automatically yet, so its current route costs remain
unknown until evidence is supplied. `PublicTrialLedger.billing_snapshot()`
reports supplied request records by currency and exposes missing voucher/cash
fields; it does not allocate account-level billing data. Jev's input rate is owner-reported and must
be checked against each OpenRouter generation record. Cross-provider fallback
stays disabled within a paired cohort.

The public-trial ledger also retains a minimal official score summary for
recovery/reporting: scorer source, score in `[0, 1]`, success flag and measured
forbidden-side-effect count. Its digest is checked against the frozen benchmark
for new scored routes. The summary contains no verifier SQL, gold state or task
labels and is not added to agent context. Older ledgers migrate with a nullable
column; absent historical score detail stays absent. This persistence contract
does not mean the public runner has produced any official score; the live runner
is still disabled and the benchmark score denominator remains 0/286.

`manifests/comparison_trial_manifest_v7.json` pre-registers the 206 EnterpriseOps
oracle tasks and 80 τ-Bench official test tasks, plus separate 20-task
EnterpriseOps and 4-task τ-Bench smoke sets. It pins the source-manifest and
task-inventory hashes, all role-specific model-profile fingerprints,
resource-profile digest, per-role request/token budgets for the Agent, user
simulator and judge, first-wave aggregate ceilings, trial isolation rules,
route pairs, and scoring boundaries.
The manifest also freezes the first wave: one fixed case each for EnterpriseOps
CSM/ITSM and τ retail/telecom, paired as ReAct and Plan for 8 route slots. Its
current status is `pre_registered_not_run`;
the engineering protocol smokes below do not change that status or count as
public task attempts.

Frozen input SHA-256 values are newline-stable: the source manifest is hashed
as UTF-8 text with LF line endings, while the task inventory retains its
original UTF-8 CRLF canonical digest. The verifier normalizes checkout line
endings before hashing, so Git's Windows/Linux checkout settings do not look
like benchmark-data drift; all non-newline content remains covered by SHA-256.

Exposure groups remain `unassigned` because task-family mappings still need
independent review. The manifest does not copy prompts, simulator scenarios,
verifiers, expected actions, or database contents. Rebuild it deterministically
from the frozen local inventory with the v3 pricing profile and v7 cohort using:

```powershell
.venv\Scripts\python.exe evals/benchmarks/build_trial_manifest.py --manifest-version 7 --model-profiles evals/benchmarks/manifests/evaluation_model_profiles_v3.json --output .decagent/evaluation-closeout/new-comparison-trial-manifest.json
```

For the current cohort, use `evaluation_model_profiles_v3.json` and explicitly
select manifest version 7. The builder's default remains v5 for compatibility.
The v5 and earlier files remain unchanged for historical traceability. The v7
batch ceilings are 500 Qwen requests / 5,000,000 Qwen tokens / 1,600 tool calls
and 4 hours, with separate Agent (300 / 3,000,000), user-simulator (160 /
1,600,000), and judge (40 / 400,000) request/token caps. A persisted batch
ledger carries these limits and its deadline across resume; a stop line keeps
unfinished trials untouched. This is an admission guard, not a hard cancel for
an already-running network request, so the live runner remains gated.

The command uses exclusive file creation, so choose a new output path. Earlier
v2 and v3 manifests remain as historical evidence; they are not overwritten.
Route pairs must carry the same exposure assignment, model-profile and
resource-profile digests. Structured branches use code rules; a Jev semantic
comparison is marked not-applicable until the benchmark contains a naturally
occurring semantic decision with a trusted, reviewed question template. Do not
manufacture Jev cases or change the official 206+80 denominator.

## Isolated comparison runtime

Public benchmark tools enter the plan executor through the evaluator-only
`EvaluationCatalog`. A fresh `EvaluationScope` captures the benchmark revision
and the task's original MCP tool schemas; it never registers those tools in the
production `PluginRegistry` or HTTP API. EnterpriseOps and τ-Bench sessions use
the same plan compiler, input binding resolver, and `PlanRuntime` as the
service-tickets workflow. Each controlled route receives a new environment and
an exclusive per-trial SQLite journal. This keeps comparison state separate
from the company's PostgreSQL task and receipt tables.

EnterpriseOps initial-state identity now uses a private live-database
fingerprint collected after each fresh restore. The fingerprint includes the
SQLite schema and deterministic `rowid`-ordered rows, and returns only a digest
and row/table counts to the evaluator. The collector refuses schemas above 256
objects, more than 128 tables, more than 10,000 rows in a table, or more than
64 MiB of row data. The original snapshot hash remains a separate provenance
check. The fingerprint stays outside model input and binds paired routes to the
same restored state.

`enterprise_scoring.EnterpriseOfficialScorer` now provides the default private
scorer for Enterprise sessions. It verifies the pinned source archive and every
executable benchmark source before importing the original VerifierEngine. Exact
private configs, live database identity and task context go only to that engine.
All valid verdicts must pass; a valid false verdict is a task failure, while a
throwing, malformed or error-bearing verifier is a scoring failure. Initial and
post-state digest/count audit projections are stored in the isolated journal.
Unmeasured intermediate side effects remain null / `not_measured`.

Plan routes now render actual returned journal observations as the default final
response. A frozen response policy may require a narrative or a summary when the
complete deterministic output is too large; then at most one explicitly injected
`qwen3.8-flash` callback is used. It receives only public task/policy and actual
observations, must cite observed call IDs, and is charged to Agent primary usage
and latency. Oversized inputs are refused, not truncated. Unknown usage skips
all private scoring so no new official judge request can follow. ReAct uses the
same frozen UTF-8 output byte limit. Citation/format validation is not proof of
semantic correctness; the official verifier still determines task success.

The output policy is bound into `ComparisonTrial` and the v4 comparison manifest.
The prior, unrun v1 manifest is retained under
`evidence/comparison-trial-manifest-v1-before-final-response.json`. These changes
do not authorize live requests; provider budget and readiness gates remain the
caller's responsibility. All 286 formal cases remain unrun.

The journal retains exact upstream tool responses alongside a deterministic
JSON view for planned and ReAct calls, including errors. A received MCP tool
error remains an observation for ReAct or follows a plan's explicit error edge;
a transport-uncertain write pauses and cannot be replayed. A
PlanRuntime completion means only that declared evidence was observed; the
official EnterpriseOps or τ-Bench verifier remains the sole source of task
success. The synchronous τ-Bench adapter crosses an in-memory MCP client/server
session while delegating tool results and errors to the original environment.

The adapter and executor can be checked without model calls through
`evals/deterministic/test_business_public_evaluation_session.py` and
`evals/deterministic/test_business_tau_tools.py`. The Docker smoke test in
`evals/integration/test_business_enterprise_environment.py` checks pinned image,
snapshot, tool catalog, independent live-state fingerprints across fresh
resets, and original SQL verifier readiness; it is not a task score. τ-Bench's
opt-in route smoke uses scripted zero-cost model adapters to exercise ReAct and
Plan clarification, original tools, the private `SimulationRun` transcript
journal, and the original evaluator. Its numeric evaluator output is protocol
evidence only and is not a model success result. Formal comparisons still
require ready measured model callbacks, the original benchmark scorer, and frozen paired task
identities. No EnterpriseOps or τ-Bench task result is claimed by the offline
contract tests.

Route timing separates environment setup, the agent interval, private scoring,
cleanup, and total wall time. Agent machine latency includes planning and tool
execution, excludes harness setup/scoring/cleanup, and subtracts measured human
and user-simulator waiting. Initialization failures retain `agent_started=false`
and must not be presented as fast successful runs.

## Separately locked τ2 tool process

The pinned upstream core lock conflicts with the application's MCP 2.3.0
dependency chain. Keep both locks: `tau_process.PinnedTauRuntime` starts one
dedicated Python 3.12 worker per trial. The worker uses only stdlib and original
τ2; application MCP stays in the main environment. This pinned revision also
needs the locked optional `websockets==15.0.1` because of an eager voice import.
Python 3.13 fails on that upstream audioop import, so the tested runtime is
Python 3.12.7. Upstream source and dependency pins were not edited.

Only trusted evaluator code selects the executable/domain/task. Original
schemas, JSON argument types, text results, errors and mutation metadata cross
the bridge unchanged. The worker validates the pinned source archive/files,
does not inherit model credentials, disables dotenv/network connections, and
exposes simulator/scorer operations only through evaluator-private ports. Model
requests use measured parent callbacks; the worker receives no provider keys.
Request IDs, 4 MiB frames and bounded waits
prevent stale/unbounded responses. Transport failure closes the process without
tool replay. Closing the MCP session reclaims the owned process tree on Windows;
the POSIX process-group cleanup remains outside independent Linux acceptance.

`evals/benchmarks/verify_tau_runtime.py` checks all 80 official supplementary
test tasks with two fresh original initializations. The committed runtime
readiness evidence records 80/80 ready and **zero formal attempts**, not task
success. `evals/deterministic/test_business_tau_process.py` separately exercises
real upstream tools through MCP and the production plan binding path. Its public
fixture lookup is a synthetic protocol regression, not an official task result.
On Windows, run these commands with Python UTF-8 mode enabled: without it, the
telecom task files can raise `UnicodeDecodeError` under the active locale. Set
`PYTHONUTF8` in PowerShell so the setting reaches Python subprocesses too.

Opt in to real offline subprocess tests from the project root:

```powershell
$env:PYTHONUTF8 = '1'
$env:DECAGENT_TEST_TAU_PYTHON = (Resolve-Path ".decagent/evaluation-runtimes/tau2-96a3ef9-py312/Scripts/python.exe").Path
.\.venv\Scripts\python.exe -m pytest evals/deterministic/test_business_tau_process.py -q -rs
```

The 2026-10-20 offline recheck without UTF-8 mode initialized 40/80 tasks and
reported `UnicodeDecodeError` for all telecom tasks. Repeating it with
`PYTHONUTF8=1` initialized 80/80; both runs made zero model requests and scored
zero official tasks. The failed and successful records are preserved separately
as `evidence/tau2-runtime-readiness-20261020.json` and
`evidence/tau2-runtime-readiness-20261020-pythonutf8.json`.

Without that separate runtime, real τ process checks explicitly skip; standard
CI does not download the public corpus or call providers. The old optional test
that imports τ2 inside the application environment intentionally remains skipped.
See the [runtime guide and current evidence](../../generated/261018/评测/1928_τBench独立运行时桥与验证.md).

## Public comparison preflight

The public runner is offline by default. It verifies the frozen v7 trial manifest,
source and inventory checksums, model profiles, routed-model readiness evidence,
and optionally local digest-pinned EnterpriseOps images. It does not create a
trial ledger, call a model, or call an MCP tool:

```powershell
python evals/benchmarks/run_public_trials.py --mode smoke --check-images
python evals/benchmarks/run_public_trials.py --mode full --check-images
python evals/benchmarks/run_public_trials.py --phase first-wave --check-images
python evals/benchmarks/run_public_trials.py --phase remaining-smoke --check-images
python evals/benchmarks/run_public_trials.py --phase full --check-images
```

`--phase` selects only a pre-registered subset while preserving the full cohort
denominator in the report: `first-wave` selects 4 tasks / 8 ReAct/Plan slots,
`remaining-smoke` selects the other 20 / 40, and `full` selects 286 / 572.
These selectors are dry-run previews; they do not claim routes or resume a
ledger.

To preserve a new preflight report, use `--report` with a new JSON filename under
`evals/benchmarks/evidence`; existing evidence cannot be overwritten. The `--state`
option is reserved for future live execution and is rejected in dry-run mode.
`--execute` currently fails closed until a persistent phase runner owns the
provider dispatch lifecycle, cancellation, official scorer accounting, and
verified environment cleanup. The smoke/full reports dated 2026-10-19 are
preflight evidence only: they contain 24/286 planned cases, not executed tasks or
benchmark scores. They also enumerate the fixed four-case first wave and its
eight ReAct/Plan route slots without dispatching them. Jev remains N/A unless a
naturally occurring semantic decision has been reviewed and matched to a trusted
template.

The gated live first-wave path now reuses the production PlanRuntime route
adapters, pinned EnterpriseOps/τ environments, measured model callbacks,
ledgered MCP gateway, original benchmark scorers, and verified cleanup. Before
opening the canonical trial ledger it preflights all four frozen tasks and
requires their isolated environments to clean up successfully. The scheduler
checks every live `PublicComparisonTrial` against its frozen manifest ID,
benchmark, version, case, exposure group, resource limits, response policy,
and role-profile digests before reserving a route.

Live execution remains opt-in and accepts only the fixed `first-wave` phase.
It fails closed unless the checkout is clean, the exact `HEAD` has a completed
successful GitHub Actions workflow named `CI`, the frozen readiness/runtime
profiles match, the main-model credential is configured, cross-model fallback
is disabled, both digest-pinned EnterpriseOps images are already local, and
the pinned τ Python 3.12 runtime is available. Reports are exclusive new JSON
files under `evals/benchmarks/evidence`; the persistent cohort and Jev ledgers
always use their canonical paths.

Example, from a clean checkout whose exact commit has passed GitHub CI:

```powershell
python evals/benchmarks/run_public_trials.py `
  --execute --mode smoke --phase first-wave `
  --tau-python .decagent/evaluation-runtimes/tau2-96a3ef9-py312/Scripts/python.exe `
  --report evals/benchmarks/evidence/public-evaluation-live-first-wave-unique.json
```

The current worktree is not eligible while it contains local changes. No real
provider dispatch is implied by a dry-run, readiness record, callback unit test,
or image check. The first live report is an eight-route engineering smoke; it
does not by itself establish Jev performance or a benchmark-wide result. Any
unknown route remains terminal and is never replayed. Jev remains outside this
first-wave comparison unless a separate approved pairing is scheduled.

Environment-only preflight evidence for the four fixed first-wave tasks is in
[`evidence/public-first-wave-environment-preflight-20261020.json`](evidence/public-first-wave-environment-preflight-20261020.json).
It confirms source-pinned initialization and verified cleanup only; it contains
no route execution, official score, business tool dispatch, or provider request.

### First live cohort status and candidate binding (2026-10-21)

The first live attempt did run on candidate
`9bc3ee6654e46630c9ae8238a28eb6e34a088698` after GitHub CI run `39442012550`
passed. It covered the fixed four tasks and eight ReAct/Plan routes: four routes
ended `failed`, four ended `unknown`, all eight cleanups were verified, and no
official scorer ran. The canonical v7 ledger has four Qwen attempts and does
not contain candidate-SHA metadata. These are terminal historical attempts;
they must not be replayed or retroactively attributed to a candidate using the
current checkout, a later report, or a CI run.

The live ledger now binds a new, empty cohort to one exact CI-verified SHA
before any route claim. Resume, dispatch, and report generation verify the same
binding; an active historical ledger without this metadata is rejected, and
malformed metadata fails closed. As a result, the existing v7 cohort cannot be
used for another candidate. A new real batch requires a new cohort, a separately
frozen budget, and same-SHA CI evidence. Current official benchmark result
remains 0/286 scored tasks; this engineering attempt does not establish model
quality or a Jev advantage.
