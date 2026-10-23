# GHCR 与私有 CD

目标 CD 路径是通过 CI 固定版本向本机 SOP 服务发布。公共 `main` 当前只做 tag-only GHCR 发布，没有调度本机的步骤；开发分支保留受 `DECAGENT_PRIVATE_CD_ENABLED` 门控、只向固定私有 workflow 派发的实现。**真实端到端 CD 仍未验收**：GitHub App `5443230` 已创建并安装到唯一选择的私有仓库 `shuaibilx/DecAgent-deploy`；GitHub 安装页显示 `Only select repositories` 且仅选中该仓库。权限为 `actions: write` 与必需的 `metadata: read`。私有仓库 Runner `decagent-local-deploy` 在线，公开仓库 Runner 为 0，旧公共 Runner 计划任务已禁用。公开仓库的 App Client ID variable 与加密私钥 secret 已配置，但 `DECAGENT_PRIVATE_CD_ENABLED` 仍未设置。尚未做 App-authenticated 独立安装范围核验、固定 workflow dispatch 或持久 SOP 服务部署；没有创建发布 tag 或启动 rollout。详细核验过程见[GitHub App 安装范围核验与恢复记录](../../evals/integration/evidence/github-app-install-scope-recovery-20261024.md)。

## 版本发布路径

```mermaid
sequenceDiagram
    participant D as 开发者
    participant P as Public DecAgent
    participant CI as GitHub-hosted CI
    participant G as GHCR
    participant A as GitHub App
    participant R as Private DecAgent-deploy
    participant W as 本机 Windows Runner
    participant C as SOP Compose
    D->>P: 将 main 上的提交打 vMAJOR.MINOR.PATCH tag
    P->>CI: 运行完整复用 CI
    CI-->>P: 任一必需 job 失败则停止发布
    CI->>G: 发布 control/executor/runner 镜像及 digest
    alt DECAGENT_PRIVATE_CD_ENABLED = true 且凭证就绪
        P->>A: 获取仅 DecAgent-deploy/actions:write 的短期 token
        A->>R: 派发固定 workflow_dispatch 与 allowlist 字段
        R->>R: 固定验证器核对仓库、tag、SHA、run/attempt、CI 与 digest
        R->>W: 仅在私有 main 环境调度私有 Runner
        W->>C: 预检、备份、拉取不可变 digest、升级并检查业务 readiness
        alt schema 兼容且候选健康失败
            C-->>W: 恢复旧应用 digest，保留数据库
        else schema 不兼容
            C-->>W: 停止应用并保留数据及备份，等待人工处理
        end
    else
        P-->>D: GHCR 发布结束；本机不部署
    end
```

## 公开与私有仓库的设置边界

| 位置 | 需要的设置 | 当前状态 |
| --- | --- | --- |
| Public `DecAgent` | 开发分支的私有 dispatch 需要 `DECAGENT_PRIVATE_CD_ENABLED=true`、`DECAGENT_DEPLOY_APP_CLIENT_ID` variable、`DECAGENT_DEPLOY_APP_PRIVATE_KEY` secret | Client ID variable 与私钥 secret 已配置；`DECAGENT_PRIVATE_CD_ENABLED` 未设置。`main` 的发布路径只发布 GHCR 镜像。不得把 App 私钥放进日志或源码。 |
| GitHub App | 仅安装到 `DecAgent-deploy`，提供固定 Actions workflow dispatch 所需的最小权限 | 安装页显示只选择 `DecAgent-deploy` 一个仓库；App 仅有 Actions 写权限（另含必需 Metadata 只读），webhook 关闭。App-authenticated API 独立核验仍待完成。 |
| Private `DecAgent-deploy` | 固定 `main` workflow、`decagent-local-production` 环境、Windows runner label `decagent-deploy` | workflow/package 已同步；Runner `decagent-local-deploy` 在线，标签包含 `decagent-deploy`。 |
| Local Windows host | `DECAGENT_LOCAL_PROJECT_DIR`、Python 3.11+ venv、`ops/deploy/sop.env`、三份文件密钥和运行中的 Linux Docker Desktop | env/secret 已准备、预检及一次性空密钥容器 smoke 通过；`DecAgentPrivateDeployRunner` 计划任务运行，旧公共 Runner 任务禁用；持久 SOP 服务尚未部署。旧版 `decagent-api` 仍运行在 `127.0.0.1:8000`。 |

GHCR control 镜像当前为公开包，可匿名 pull。`DecAgent` 在 Manage Actions access 中显示 Admin，说明该仓库有该 package 的管理权限；它不等于向私有部署仓库派发 workflow 的 App 权限。当前公开的旧版 control image 不是本 change 的 SOP 构建，不能据此启动生产 rollout，也不需要额外授予 package Read。来源与权限检查的实测细节见[私有部署边界证据](../../evals/integration/evidence/private-deployment-boundary-20261023.md)。

私有 workflow 固定 checkout 私有仓库 `main`，先运行 `release_verifier.py`，再把验证器产生的镜像 digest 传给固定 rollout coordinator。它不 checkout 或执行公开 release 指定的源码，不接收脚本、shell 命令、工作目录或任意镜像输入。并发部署使用不取消的单一锁。

## 尚未完成的门槛

1. 完成 App-authenticated API 的独立安装范围核验，并用不启动 rollout 的方法验证固定 workflow dispatch 身份与公共 PR/Actions 无法调度本机 Runner；部署开关保持关闭。
2. 已完成按 private repo 限制注册 Runner；本机 env/secrets 已准备，Runner API 显示仅私有仓库有 `decagent-deploy` 标签的在线 Runner。
3. 先完成任务 4.4 的 Docker 首装、失败恢复、兼容升级、健康失败回滚与 schema 不兼容停服演练。
4. 再执行经批准的可信 GHCR 发布到本机的 5.4 全链路演练，记录 release SHA/run、镜像 digest、readiness、备份和恢复结果。

截至 2026-10-24，安装页显示 App 仅选择私有 `DecAgent-deploy`；该仓库有 1 个在线 self-hosted Runner，公开 `DecAgent` 为 0。公开仓库未设置 `DECAGENT_PRIVATE_CD_ENABLED`，且没有发布 tag、触发私有 dispatch 或部署持久 SOP image。最新公开分支 CI run [40032074530](https://github.com/shuaibilx/DecAgent/actions/runs/40032074530) 的全部 12 个 job 均运行在 `ubuntu-latest`，未调度本机 Runner。隔离 smoke 不代表发布或部署验收。此计划不启用公网 URL、Cloudflare Tunnel 或独立 Linux 主机。
