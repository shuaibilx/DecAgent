# DecAgent 公共仓库与本机 Runner 边界核验

核验日期：2026-10-22（Asia/Shanghai）

仓库：`shuaibilx/DecAgent`（public，default branch `main`）

方式：GitHub CLI/API 只读检查；未注册 Runner、未改仓库设置、未派发部署。

## 线上 GitHub 状态

- `GET /repos/shuaibilx/DecAgent/actions/runners` 返回 `total_count=0`。
- `GET /user/orgs` 返回空列表；当前账号没有组织级 Runner 可供此仓库调度。
- GitHub workflow registry 当前列出 `CI` 和 `Release and local deployment` 两个 active workflow。
- 远端 `main` 的 `.github/workflows/ci.yml` 中所有 job 使用 `ubuntu-latest`；触发器为 main push、pull request、workflow dispatch 和 workflow call。
- 远端 `main` 的 `.github/workflows/release.yml` 只监听 `v*` tag；quality job 调用 CI，publish job 使用 `ubuntu-latest`，只负责向 GHCR 发布镜像。
- release YAML 中没有 `self-hosted` runner、没有本机部署 job，也没有 SSH/本机触发路径。发布使用 GitHub-hosted runner 与 job-scoped `packages: write` 权限。

## 结论与边界

当前公共 GitHub 仓库没有本机 Runner 注册，现有 workflow 也不请求 `self-hosted` 标签，因此 PR、CI 和 tag release 都不会在用户 Windows 电脑执行。tag release 会发布 GHCR 镜像，但这不等于已部署到本机。

workflow 对外显示名仍是 `Release and local deployment`，与实际 GHCR-only 职责不符。这个名称修正留待经过审查的仓库变更，不据此推断存在 CD。私有 `DecAgent-deploy`、最小触发凭证、自托管 Runner 迁移及部署/回滚演练均未完成。

## 可复核命令

```text
gh api repos/shuaibilx/DecAgent/actions/runners --jq .total_count
gh api user/orgs --jq ".[] | [.login,.id] | @tsv"
gh api repos/shuaibilx/DecAgent/actions/workflows --jq ".workflows[] | [.name,.path,.state] | @tsv"
gh run list --repo shuaibilx/DecAgent --limit 10
```

未读取或输出任何凭据；未运行部署 workflow；未更改 GitHub 外部状态。

## 当前工作站复核（2026-10-22）

再次读取 GitHub Runner API，仍为 `total_count=0`；远端 release workflow 的所有 job 仍使用 GitHub-hosted `ubuntu-latest`，职责仅为 CI gate 和 GHCR 发布。当前工作站查询未发现 `GitHub Actions Runner` 服务、`Runner.Listener` 或 `Runner.Worker` 进程，`DecAgentLocalDeployRunner` 计划任务状态为 `Disabled`。公共仓库到本机的部署调度边界已冻结；私有部署仓库和本机部署演练仍是后续任务。

## Feature-branch guard verification (2026-10-22)

The remote feature branch `feat/conditional-evaluation` points to candidate `f994fdcdf72e73f6a94b3823e4399c4176b7fbf8`. Its release workflow is named `Release image publishing (GHCR only)` and is tag-only; `evals/deterministic/test_public_release_boundary.py` asserts the public release jobs use GitHub-hosted Ubuntu, only call the reusable CI workflow and publish images, and cannot schedule a self-hosted runner. The default branch still reports the older display name `Release and local deployment`; its workflow has no local runner job, but the name correction is not yet in `main`.

GitHub Actions run [39691794406](https://github.com/shuaibilx/DecAgent/actions/runs/39691794406) matches the candidate SHA and all five jobs succeeded. The boundary tests passed locally under both available Python 3.13 environments (`9 passed` each). This verifies the committed candidate and its regression guard, not the current dirty worktree or private CD. No runner was registered and no deployment was triggered.
