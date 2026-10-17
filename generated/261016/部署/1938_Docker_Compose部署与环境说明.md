---
title: DecAgent Docker Compose 部署与环境说明
type: how-to
audience: [A1, A4]
runs: no
verified_on: 2026-10-17
sources:
  - compose.yaml
  - compose.windows.yaml
  - ops/local/start.py
  - ops/control/prepare_secrets.py
  - ops/control/offline_smoke.py
  - README.md
---
# DecAgent Docker Compose 部署与环境说明

CI 负责检查和构建；CD 在版本标签通过检查后，把镜像发布到 GHCR 并更新已配置的本机 Windows Docker Desktop。当前没有独立 Linux 主机，因此本文也把 Linux 部署前提单独保留，不能用本机部署替代 Linux 验收。

## 先区分两种运行环境

DecAgent 当前有两条运行路径，不能把它们当成同一个部署包：

| 运行路径 | 组成 | 用途与状态 |
| --- | --- | --- |
| Windows 本机业务模拟 | `compose.yaml` + `compose.windows.yaml` 提供 PostgreSQL 和 broker；`ops.local.start` 在宿主机启动 MCP、员工 API、业务 Worker | 用于本机模拟员工工单流程。源码 README 标记当前 schema 为 17；2026-10-14 的旧使用指南记录 schema 11，只是旧版本快照。不要把旧指南当成当前数据库升级说明。 |
| Compose 容器栈 | PostgreSQL、迁移、可选 owner 初始化、API、`repository_repair` Worker、egress proxy、executor broker | 当前 `compose.yaml` 的 Worker 命令是 `worker --kind repository_repair`。该文件不能证明业务 MCP 与 business Worker 已经完成 Linux 部署验收。 |

```mermaid
flowchart LR
    subgraph Windows[Windows 本机业务模拟]
      A[浏览器员工工作台] --> B[宿主机 API]
      B --> C[宿主机业务 Worker]
      C --> D[业务 MCP 模拟服务]
      B --> E[(Compose PostgreSQL)]
      C --> E
      F[Compose Executor Broker] --> C
    end
```

## Compose 容器栈包含什么

```mermaid
flowchart TD
    P[PostgreSQL] --> M[数据库迁移]
    M --> A[API]
    M --> W[repository_repair Worker]
    X[Executor / egress proxy] --> W
    X --> A
    O[首次初始化 owner<br/>setup profile] -.仅新实例.-> M
    U[用户] -->|loopback 8000| A
```

配置文件通过 `${...}` 读取 `.env`，敏感文件从 `.decagent/secrets/` 装载。Compose 在启动前要求设置 `DECAGENT_RUNNER_IMAGE` 为本机 runner 镜像的不可变 `sha256` image ID。API 默认仅绑定 `127.0.0.1`；PostgreSQL 与 broker 不应直接暴露到公网。`executor-broker` 挂载 Docker socket，因此运行它的主机权限和隔离需要谨慎管理。

## 部署前需要准备

未来部署需要一台独立 Linux 主机、Docker Engine 与 Compose 插件、DecAgent 代码、可持久化备份的数据库卷、模型/API 凭据、安全的 HTTPS 入口，以及和当前源码版本一致的迁移计划。部署负责人还要确认 runner 镜像、宿主机 Docker socket 与执行隔离策略。

当前没有独立 Linux 主机。Windows Docker Desktop 已用于恢复并检查本机现有服务，也运行了隔离离线 Compose smoke；这些检查不等同于新机器安装、完整网页闭环或生产部署。

## 未来 Linux Compose 部署准备

以下命令是未来在专用 Linux 主机上的操作示例，本轮没有执行。先从 GitHub clone 已审查的仓库 commit，在项目根目录配置环境。仅在全新目录且 `.env` 不存在时复制模板；不要覆盖已有 `.env`：

```bash
if [ ! -f .env ]; then cp .env.example .env; fi
```

在 `.env` 中填入部署所需的地址、模型名和 API 凭据。不要提交 `.env` 或把密钥写进命令历史。程序提供的秘密初始化脚本只创建缺失文件并保留现有 secret；运行前先审阅设置来源：

```bash
python -m ops.control.prepare_secrets
```

构建 runner 镜像并查看不可变镜像 ID：

```bash
docker build -f ops/runner/Dockerfile -t decagent-executor-runner:supervised .
docker image inspect decagent-executor-runner:supervised --format '{{.Id}}'
```

将输出的 `sha256:...` ID 配置给 `DECAGENT_RUNNER_IMAGE`，再检查 Compose 最终配置，并按部署审查结果启动：

```bash
docker compose -f compose.yaml config --quiet
docker compose -f compose.yaml build
docker compose -f compose.yaml up -d --wait
docker compose -f compose.yaml --profile setup run --rm owner-init
docker compose -f compose.yaml ps
```

首次初始化只适用于全新数据库。已有实例不要重复执行 owner 初始化，也不要为排障直接删除数据库卷。上线前需在目标机实测 API 健康状态、登录、迁移、备份恢复、runner 网络隔离和日志脱敏；具体业务模拟栈的 Linux 部署方案还需要单独完成设计与验收。

停止容器可使用 `docker compose -f compose.yaml down`，它会保留命名数据卷。不要使用 `down -v`，除非你明确要删除数据库及其他持久化数据并已有可验证备份。

## GHCR 发布与本机自动部署

`.github/workflows/release.yml` 只响应 `v*` 标签。它先在 GitHub 托管 Runner 上复用完整 CI，再检查标签格式为 `vMAJOR.MINOR.PATCH`、提交位于 `main` 历史中；随后发布 control、executor 和 runner 镜像到 GHCR，使用 Registry 返回的 digest 部署。普通 push 和 Pull Request 不会使用本机 Runner。

首次安装本机 Runner 前，确认 GitHub CLI 已以仓库管理员账号登录，当前用户已经配置 `.env` 与 `.decagent/secrets/`，Docker Desktop Linux engine 正在运行。然后在 DecAgent 根目录的 PowerShell 执行：

```powershell
.\ops\deploy\install-local-runner.ps1
```

脚本会下载官方 Windows x64 Actions Runner、注册仓库级 `decagent-local-deploy` Runner，并创建当前用户登录时启动的计划任务。此终端没有管理员权限，所以 Runner 不注册为 Windows 服务。任务启动后会等待 Docker Desktop Linux engine 就绪；用户登出、Docker Desktop 未启动或电脑休眠期间，本机 Runner 不在线，发布部署会等待或失败。若希望开机后自动部署，请在 Docker Desktop 设置中启用登录时启动，并保持当前 Windows 用户处于登录状态。

Runner 默认安装在 `%USERPROFILE%\DecAgentRunner\actions-runner`，由当前用户的计划任务写入和更新；启动诊断记录位于该目录的 `_diag\local-bootstrap.log`。如果曾经按旧版本安装在 `%LOCALAPPDATA%\DecAgent\actions-runner`，安装脚本会把现有 Runner 配置迁移到用户目录。

仓库环境 `local-deploy` 限制为 `v*` 标签。GHCR package 默认私有，发布工作流只使用短期 `GITHUB_TOKEN`：发布 job 仅有 `packages: write`，本机部署 job 仅有 `packages: read`。没有把 Qwen、Jev 或 GitHub 业务凭据上传到 Actions secrets；部署时仍从本机项目目录读取 `.env` 和 `.decagent/secrets/`。

私有 GHCR 镜像必须允许 `DecAgent` 工作流读取。三个 package 的 **Package settings → Manage Actions access** 均已确认包含仓库 `shuaibilx/DecAgent`，角色为 **Admin**，高于读取镜像所需的权限；部署 job 声明 `packages: read`。`v0.1.1` release run [38988637726](https://github.com/shuaibilx/DecAgent/actions/runs/38988637726) 已证明 `docker/login-action` 使用短期 `GITHUB_TOKEN` 可以成功登录并拉取/发布镜像。随后部署脚本在 PowerShell 5.1 的 `docker inspect` 模板标签参数处失败，发生在数据库备份和 Compose 更新之前。现改为解析 Docker 返回的 JSON 标签，Windows PowerShell 5.1 的 `-ValidateOnly` 已通过；真实备份和服务升级仍待下一补丁版本确认。

三个 package 页面：[control](https://github.com/users/shuaibilx/packages/container/package/decagent-control)、[executor](https://github.com/users/shuaibilx/packages/container/package/decagent-executor)、[runner](https://github.com/users/shuaibilx/packages/container/package/decagent-runner)。

首次 `v0.1.0` 已发布；部署修复的下一次验证计划使用 `v0.1.2` 标签：

```powershell
git tag v0.1.2
git push origin v0.1.2
```

部署脚本 `ops/deploy/deploy-local.ps1` 会先检查当前 API、PostgreSQL、Compose 项目名、密钥文件和配置，再将数据库备份写入 `.decagent/backups/predeploy-<版本>-<UTC时间>.dump`；然后按 digest 拉取三类镜像、执行 Compose 迁移与更新，并等待 `/health` 返回 `ok`。脚本使用 `compose.yaml` 和 `compose.windows.yaml`，不运行 owner 初始化，不重写 `.env`，也不删除 Compose 卷。备份文件在 `.decagent/` 下，已被 Git 忽略。

若更新或健康检查失败，GitHub Actions 会标记失败并保留更新前备份；脚本不会自动恢复数据库，因为迁移不一定能由旧版程序读取。先查看 Actions 的错误步骤与本机容器状态，再根据迁移兼容性选择恢复方案；不要使用 `docker compose down -v` 排障。Runner 具有本机 Docker daemon 权限，因此即使仓库公开，仍应把 `.github/workflows/release.yml` 和 `ops/deploy/` 的变更按高权限代码审查。

## Windows 本机说明

当前 Windows 模拟环境使用 `compose.windows.yaml`，将 PostgreSQL 暴露到本机 `127.0.0.1:55432`，broker 暴露到本机 `127.0.0.1:8002`；MCP、业务 API 和 Worker 由 `python -m ops.local.start` 在宿主机运行。2026-10-16 验证时，Docker Compose 服务处于运行状态，API `/health` 返回 `ok`。隔离离线 smoke 通过；它使用临时项目，不重置当前用户数据。历史[本机使用指南](../../261014/1646_MCP业务Agent使用指南.md)按 schema 11 编写，而当前源码 README 是 schema 17，使用前需先按当前迁移与数据状态核对；不要对已有业务数据库盲目重建或重置。

## 部署状态

本机 Windows Docker Desktop 的现有 Compose 服务健康，本机 Runner 在线。`v0.1.1` release run [38988637726](https://github.com/shuaibilx/DecAgent/actions/runs/38988637726) 通过完整 CI 并发布三个 GHCR 镜像，部署 job 也成功登录 GHCR；部署脚本之后在 PostgreSQL 标签检查处因 Windows PowerShell 5.1 解析模板引号失败而停止。该失败发生在备份、迁移和 Compose 更新之前，现有 API 仍健康，三个持久数据卷名称与部署前相同。`deploy-local.ps1 -ValidateOnly` 的 Windows PowerShell 5.1 复测已通过；`v0.1.2` 仍须完成真实备份、digest 部署和健康检查。新实例完整网页闭环与独立 Linux 主机验收仍未完成。
