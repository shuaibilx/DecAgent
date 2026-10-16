# Proposal

## Why

编码 Agent 能提出修复，但其执行权限、人工介入、记忆沉淀与中断恢复往往缺少可检查的依据。DecAgent 将这些决策独立出来，用真实 Python 缺陷修复和可复现对照评测，展示完整的 Agent 工程能力，并验证 Jev 判断在何处值得引入。

## What Changes

- 在当前 Waku 的同级目录 `../DecAgent` 创建独立 Python 项目，包与 CLI 名称为 `decagent`；原 Waku 源码和 `.waku/` 数据保留。
- 提供单用户、开源自部署的研发工作台：提交指定 GitHub 仓库的缺陷任务，自动推断项目准备与测试命令，复现、修复、回归验证并创建草稿 PR。
- 建立持久化任务、执行前权限校验、绑定具体动作的人工审批，以及带执行凭据的中断恢复。
- 独立接入 TypeSafe Jev，判断工具风险、人工介入、记忆写入和记忆读取；程序权限规则始终拥有最终约束。
- 建立有来源与版本的项目记忆：分支结论先供当前任务使用，确认合并且版本适用后再激活为项目记忆；设计决策须已获用户确认。
- 提供网页任务、审批、差异与审计界面，CLI 配置和诊断，以及 Docker Compose 部署、备份恢复和分阶段实测。
- 新项目聚焦 Python 缺陷修复，不包含 Waku 的日历、语音、Telegram、宿主直接运行生成代码或 pi 子进程委托；保留 MIT 许可与原作者归属。
- 仅执行用户认可的可信代码。外部贡献者代码可分析，获得明确认可后才可执行；执行环境允许公网访问但隔离控制服务凭据、宿主和内网。
- 允许 Agent 自动调整测试，但修改后的测试通过不能单独证明缺陷修复；缺少独立证据时暂停发布并请求人工判断。

## Capabilities

### New Capabilities

- `task-lifecycle`: 任务授权、持久化阶段、预算、取消、审批等待与重启恢复。
- `repository-repair`: Python 项目准备、缺陷复现、代码与测试修改及独立修复验证。
- `action-governance`: 操作权限、可信代码授权、执行隔离、敏感数据保护与人工审批。
- `decision-engine`: Jev 有限选项判断、严格响应校验、阈值版本化、故障降级与决策记录。
- `project-memory`: 项目记忆候选、审核、合并后激活、版本适用性、检索与上下文预算。
- `github-delivery`: 指定仓库访问、分支发布、草稿 PR、去重与合并状态核实。
- `workbench-access`: 单用户认证、网页任务与审批交互、事件恢复、辅助 CLI。
- `operations-evaluation`: 部署、诊断、备份恢复、审计、确定性验收和预算受控的对照评测。

### Modified Capabilities

无。当前 OpenSpec 没有已有 capability，且本变更不改变原 Waku 的运行契约。

## Impact

- 规划文件由当前项目的 `openspec/changes/build-decagent-workbench/` 管理；本阶段只生成规划，不创建 DecAgent 源码或移动 OpenSpec 根目录。
- 新项目复用 Waku 的模块清晰性、客户端注入、追踪和评测分层思路；重建任务运行时、执行边界与项目记忆，不复制个人助手业务和运行数据。
- 新增版本化 HTTP API、任务事件流、`decagent` CLI 和独立运行数据目录。主模型使用可配置的 OpenAI 兼容接口，Jev 使用独立密钥与适配器。
- 设计引入 LangChain/LangGraph、FastAPI/Uvicorn、PostgreSQL 驱动和 checkpoint 后端、HTTP 客户端；开发工具保持 pytest/Ruff，依赖版本在实施时验证并锁定。
- 依赖 GitHub、主模型提供商、TypeSafe API 和 Docker Engine；凭据只存在于控制服务。真实模型评测和外部仓库发布分别需要其明确配置与授权。
- 当前目录不是 Git checkout，未配置属于用户的远程仓库；本阶段不初始化、提交或推送任何 Git 仓库。
