# 代码仓库与版本流转

DecAgent 把应用开发和本机部署控制拆到两个 GitHub 仓库。这样公开代码可以正常接受 PR 和 CI，而拥有本机 Docker/数据访问权限的 Runner 只读取私有部署代码。

## 两个仓库的职责

| 仓库 | 可见性 | 保存内容 | 不承担的职责 |
| --- | --- | --- | --- |
| [`shuaibilx/DecAgent`](https://github.com/shuaibilx/DecAgent) | Public | 应用源码、OpenSpec、自动化测试、公共 CI、GHCR 镜像构建及发布触发配置 | 不直接把公开 PR 或公开 workflow 派发到本机 Runner。 |
| `shuaibilx/DecAgent-deploy` | Private | 固定的发布验证器、SOP Compose、受控 rollout 及本机 Runner workflow | 不存应用源代码、不运行公开仓库 checkout 的脚本，也不保存 plaintext secret。 |

私有仓库截至 2026-10-23 的已核实提交为 `362a00d1604bc2daa7dcdf90d9c7b58a89e36a34`；其中 11 个部署包文件（含 manifest）的内容已核验。该提交证明部署代码已同步，不证明 GitHub App 已安装、Runner 已注册或本机服务已部署。

## 应用仓库主要目录

| 路径 | 作用 |
| --- | --- |
| `decagent/` | Agent、gateway、业务身份、执行运行时和存储。 |
| `ops/` | 本机工具、Docker 执行边界、SOP Compose 与部署辅助代码。 |
| `evals/` | 确定性/集成/E2E 测试、公开基准适配、固定清单与证据。 |
| `openspec/changes/` | 当前 change 的需求、设计和验收任务。 |
| `.github/workflows/ci.yml` | PR 与 push 使用的公共质量门。 |
| `.github/workflows/release.yml` | main 上语义版本 tag 的镜像发布和可选私有部署触发器。 |
| `.agents/skills/` | 开发助手参考，不是业务 Skill、MCP server 或运行时依赖。 |
| `docs/` | 按仓库、CI、CD、本机部署拆分的操作者文档。 |

## 建议的变更流

1. 从 DecAgent 根目录检查当前分支、未提交内容和 OpenSpec 任务；先确认编辑的是公共应用库还是私有部署库。
2. 应用、评测和 Compose 变更放在公共 `DecAgent`；固定 Runner 控制逻辑和部署工作流放在 private `DecAgent-deploy`。公开发布触发器使用加密 Actions variable/secret，并由关闭状态的 CD 开关保护；SOP 服务自身的 API 密钥仍保存在本机 ACL 限制文件中。
3. 先运行与改动对应的本地检查，再让 GitHub PR/push CI 验收同一提交 SHA。CI 只会覆盖提交中的文件，不覆盖之后产生的工作区差异。
4. 只有 main 上的 `vMAJOR.MINOR.PATCH` tag 才进入 release workflow；未创建 tag 就没有新的 GHCR release，更没有私有部署。
5. 每次变更更新 OpenSpec task、测试证据和对应分区文档。不要从旧 Waku 仓库复制当前运行数据，也不要把 `.decagent/`、本地 env 或 secret 文件提交。

## 安全核对命令

以下只读命令是供操作者核对状态的参考，**本轮没有再次运行这些 GitHub 操作命令**；其中 `origin` 必须先确认指向预期仓库。

```powershell
git status --short
git branch --show-current
git log --oneline --decorate -8
git remote -v
```

当前工作目录曾出现 local HEAD、远端 tracking ref 与 GitHub API 当前分支提交不一致的情况。推送前应先比对本地和远端提交图，避免用 force push 覆盖远端历史；此文档不指导无审阅地推送当前分支。

## 参考

- [仓库根 README](../../README.md)
- [项目技能来源与边界](../../.agents/skills/README.md)
- [私有部署仓库边界检查](../../evals/integration/evidence/private-deployment-boundary-20261023.md)
- [当前 OpenSpec 任务索引](../../openspec/changes/guest-sop-local-trial/tasks.md)
