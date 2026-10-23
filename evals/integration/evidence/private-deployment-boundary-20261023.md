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
  `39784378797` before the current uncommitted changes.
- The local public release workflow edits have not been committed or published.
  This evidence does not claim a same-SHA CI run for the current worktree, a
  private workflow dispatch, a self-hosted deployment, or an end-to-end CD run.
- The authorized v10 public first-wave run on the same candidate finished with 3
  failed routes, 5 unknown routes, and 0 successful routes. No Jev or judge call
  was made; unknown requests remain reserved and were not replayed. See
  [`public-first-wave-live-20261023-v10-9d61688.md`](../../benchmarks/evidence/public-first-wave-live-20261023-v10-9d61688.md)
  for exact route, usage, and cost-estimate evidence.
- The current local worktree passed the complete deterministic suite (`2,160
  passed, 18 skipped`), Ruff, and strict validation for
  `guest-sop-local-trial`. This does not substitute for same-SHA public CI,
  PostgreSQL integration, browser E2E, Docker, Runner, or private CD acceptance.

## Supporting sources

- [GitHub package access and visibility](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility)
- [GitHub Container registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
