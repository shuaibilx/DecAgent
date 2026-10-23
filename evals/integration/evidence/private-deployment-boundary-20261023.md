# Private deployment boundary check — 2026-10-23

This records the private deployment repository state after the reviewed deployment
bundle was published. It is configuration evidence only; no Runner was registered
and no SOP image was deployed. The separately authorized public evaluation first
wave did call Qwen; its complete route and billing evidence is in the benchmark
report linked below.

## Repository and bundle

- `shuaibilx/DecAgent-deploy` is private and uses `main` as its default branch.
- Commit `362a00d1604bc2daa7dcdf90d9c7b58a89e36a34` contains the reviewed 11-file
  deployment bundle and its SHA-256 manifest. The bundle contains the deployment
  workflow, image-only Compose file, standard-library verifier, and rollout tools;
  it contains no application source or local secrets.
- All 10 file hashes recorded by the remote bundle manifest were fetched from
  `main` and checked against their actual bytes. The manifest itself is the 11th
  bundle file. The README documents anonymous public-GHCR pulls, and the private
  rollout code fingerprints file-backed secret contents without storing plaintext.
- A fresh export was checked against the allowlist and manifest. The rollout CLI
  starts under the project's Python 3.13 interpreter with `-B -S`, without app
  dependencies or bytecode files.

## GitHub controls

- Repository Actions are enabled, third-party action use is restricted to
  `actions/checkout`, and SHA pinning is required. The private deployment workflow
  currently uses only the pinned checkout action.
- Environment `decagent-local-production` is limited to the `main` branch.
- An attempt to enable branch protection returned HTTP 403 because GitHub Free does
  not support that feature for this private repository. The repository remains
  private; no public visibility change or paid-plan change was made. Environment
  branch filtering remains active, but this does not prevent a repository writer
  from changing `main`.
- GitHub reports zero registered self-hosted runners in both `DecAgent` and
  `DecAgent-deploy`. The local `DecAgentPrivateDeployRunner` scheduled task and
  Runner processes are absent.

## Image and credentials

- The `decagent-control` package settings page shows the GHCR image is public and
  linked to `shuaibilx/DecAgent`. GitHub documents anonymous pulls for public
  Container registry packages. The private workflow therefore pulls without
  `docker/login-action`, `packages: read`, or a package access grant. The screenshot
  supplied earlier showed `DecAgent` as Admin in Manage Actions access; the private
  deployment workflow does not require adding `DecAgent-deploy` to that list.
- The release-to-deployment GitHub App credentials and enablement variable are not
  configured. The dispatch job in the public repository remains local and
  default-off until those settings are deliberately completed.
- Local `sop.env` and secret files are not configured, so the Runner installer
  correctly refuses registration. No credential values were read or copied.

## Application repository and verification boundary

- Public `DecAgent` remains public; its current branch CI run for candidate
  `9d61688c52ee66435a22e18c4fa1c8348a70095a` completed successfully as run
  `39784378797` before the later branch commits; it does not cover them.
- The public release workflow edits have been committed and pushed on
  `feat/conditional-evaluation` at `4e932ce`; no release tag was created, so no
  CD was dispatched.
- Manual CI run `39935184424` on `4e932ce` failed in PostgreSQL integration shard
  2 on both Python 3.11 and 3.13. Both jobs failed the same guest-budget test:
  its assertions queried the frozen 2026-10-22 budget date while the production
  ledger factory used the actual 2026-10-23 clock. The test helper now accepts an
  optional clock, and the budget module passes locally (`36 passed`).
- A code review found that a child marked `model_call` before making HTTP could
  be misreported as transport progress. The parent now retains only observed
  transport boundaries; model-call-only deadlines map to generic
  `model_worker_timeout`. Regressions cover a real stalled child, HTTP 200
  response headers, persisted unknown attempt/route status, and replay blocking.
- The current worktree's deterministic suite passes (`2,164 passed, 18 skipped`),
  the guest-budget PostgreSQL module passes (`36 passed`), Ruff and strict
  validation for both active changes pass. The correction has not been pushed,
  so same-SHA CI remains pending. This evidence does not
  claim passing CI for the current worktree, private workflow dispatch, a
  self-hosted deployment, or an end-to-end CD run.
- The authorized v10 public first-wave run on the same candidate finished with 3
  failed routes, 5 unknown routes, and 0 successful routes. No Jev or judge call
  was made; unknown requests remain reserved and were not replayed. See
  [`public-first-wave-live-20261023-v10-9d61688.md`](../../benchmarks/evidence/public-first-wave-live-20261023-v10-9d61688.md)
  for exact route, usage, and cost-estimate evidence.
- The current local worktree passed the complete deterministic suite (`2,164
  passed, 18 skipped`), Ruff, strict validation for both active changes, and the
  guest-budget integration module (`36 passed`) against disposable local
  PostgreSQL. This does not substitute for full same-SHA public CI, browser E2E,
  Docker rollout, Runner, or private CD acceptance.

## Supporting sources

- [GitHub package access and visibility](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility)
- [GitHub Container registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
