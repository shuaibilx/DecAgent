# Proposal

## Why

当前员工工作台只服务单一模拟公司，发布流程仍偏向仓库修复服务，不能安全承接陌生游客。需要把已注册 SOP 能力交付为可恢复、隔离且费用受控的本机试用，并把公共 CI 与有本机权限的 CD 隔开。

## What Changes

- 无注册游客会话、独立模拟公司、空间内角色切换；7 天无活动过期、可清空、不支持跨设备找回。
- workspace 身份由服务端贯穿员工、目录、工单、任务、审批、trace、回执、记忆及 MCP，旧单公司任务保留兼容身份与恢复路径。
- 未校准的 Jev 只给建议，游客确认语义路径，写入仍独立确认；仅开放已注册模拟业务。
- 新增独立每日试用预算与原子预留：全站 $5/500 万 Qwen token，游客 30 任务/50 万 token；游客并行 1、全站 2、等待队列 20；限流、unknown 保留及生命周期清理。
- SOP 专用 Compose；公共仓库 CI/GHCR，私有部署仓库校验可信发布后通过本机 Runner 部署不可变 digest；备份、业务健康验证及兼容应用回滚。
- 分区指南与图示，域名/公网开通和独立 Linux 验收后置。本 change 不证明 Jev 性能，也不替代公开基准和真实员工研究。

## Capabilities

### New Capabilities

- `guest-business-workspaces`：游客身份、独立公司、角色与数据/记忆隔离及生命周期。
- `trial-resource-admission`：日额度、原子模型预留、并发、队列、限流与未知用量。
- `trusted-local-sop-delivery`：SOP 本机拓扑、公共 CI/私有 CD、发布校验、升级与恢复。

### Modified Capabilities

无主规格可修改；现有员工/治理/条件计划契约仍由原 change 承接，本 change 增加游客空间边界，不隐式同步或归档旧 change。

## Impact

影响 gateway 会话与工作台、business 身份/ACL/MCP/任务和记忆、追加 SQL 迁移、模型记账、Worker 准入与清理、Compose、Actions 与运维脚本。公共接口增加游客会话、当前空间、空间清空与模拟角色切换，不开放任意 workspace 参数。依赖 `jev-conditional-workflows` 的有效计划/执行器与现有治理边界；模型未就绪时仍可完成隔离、配额和部署测试，不能以脚本模拟当真实模型验收。

## 当前实施状态

- **已完成：** tasks 1.1–1.6、2.1–2.4、2.6、3.1–3.5、4.1–4.3、5.1、5.2、5.5（22/28）：legacy/workspace 迁移与身份解析、目录/工单/任务/操作 ACL、任务子记录归属、审批/trace/export 与 Worker/MCP 恢复隔离、workspace 记忆与稳定槽隔离、游客 session/角色/生命周期、预算账本及模型派发预留/结算、费用说明、持久队列及匿名请求限流/受信代理、SOP 专用 Compose/配置预检、真实业务 readiness 与 Worker 故障恢复烟测、安全首装/同 digest/config/secret 内容恢复、优化并验收的公共 CI、私有部署发布验证器和最小 workflow，以及分区仓库/CI/CD/本机部署指南及图示。task 3.2 的专用 PostgreSQL 集成测试最近复验 `38 passed, 0 skipped`；迁移 020–030 建立 workspace、游客生命周期、预算结算、执行队列与限流边界。
- **Jev费用边界：** 游客 Jev 固定使用 OpenRouter 的 typesafe/jev-1.14 费率 profile：每百万输入 token $0.042、输出免费、上下文 262,144 token；完整上下文费率上界 $0.011010048，每个 attempt 至少预留 $0.02。派发要求固定模型、正确路由、输入估算在上下文内且费率 basis 匹配；不依赖 Decisions API 未承诺的 provider.max_price。员工配置不变。3.2 已完成价目边界与账本 mock 集成验证；真实 Jev UI/E2E 仍属 task 2.5，游客入口默认关闭。
- **验证状态（2026-10-21）：** 本轮隔离 PostgreSQL 游客生命周期测试 19 项通过；Worker、操作回执、workspace 迁移与游客清理联合回归 67 项通过；目标 Ruff 检查通过。覆盖在途 Worker 延后写 checkpoint 后再清理、独立进程锁等待、租约保护、清理证据歧义保留、已验证 receipt、跨 Cleaner 重启幂等、自动过期与重复 clear。此前游客浏览器首次进入、角色切换、刷新恢复和清空回登录页的 Playwright CLI 实测通过；Node 工作台回归通过。更早的 deterministic 全集记录为 1937 passed、18 skipped（τ 固定子进程运行时未配置及 Windows symlink 环境限制），不代表本轮新改动已重跑全套 deterministic。无模型调用或付费评测。
- **仍待实施：** tasks 2.5、4.4、5.3、5.4、6.1、6.2（6 项未验收）：游客 UI 的真实 Jev 人工建议闭环、真实 Docker 首装/恢复与兼容升级回滚实测、Runner 与 Windows 恢复、私有 CD 和最终验收。4.3 覆盖首装/同 digest/配置/secret 内容恢复的离线实现；4.4 明确要求真实 Docker 演练首次安装、失败恢复、正常升级、兼容健康失败回滚及 schema 不兼容停服，并核验 Windows/Docker/Runner 重启。5.2 的发布验证器与私有 workflow 已迁入 DecAgent-deploy；Actions 白名单与 SHA pinning 已收紧，Environment 仅允许 main；GHCR 镜像为公开包，可匿名拉取，无需 package Read grant。GitHub 当前计划不支持该私有库的分支保护，环境分支规则限制部署来源。`.github/workflows/release.yml` 已包含受开关控制的最小 GitHub App dispatch；App `5443230` 已创建，Client ID variable 和 private-key secret 已配置，安装页仅选择私有 `DecAgent-deploy` 并等待最终确认；`DECAGENT_PRIVATE_CD_ENABLED` 仍未设置，Runner 未注册，因此没有派发私有 CD。本机 SOP env/secrets 已准备且静态 preflight 通过，但服务未部署。游客入口由 DECAGENT_GUEST_TRIAL_ENABLED 控制且默认关闭。4.1–4.3 不代表备份回滚、生产可用性或公网验收完成。分区文档索引：[docs/README.md](../../../docs/README.md)。
- **部署边界复核（2026-10-21）：** GitHub API 显示公共仓库 self-hosted Runner 数量为 0；现有工作流没有自托管 deployment job，GHCR 仍由 GitHub-hosted runner 发布；本机 `DecAgentLocalDeployRunner` 计划任务为 Disabled，Runner 服务/进程为 0。公共 CI、GHCR 和现有容器未改动。5.3 的私有仓库、可信触发器与私有 Runner 迁移仍未开始，因此该复核不计为 CD 完成。
- **部署边界与安装器复核（2026-10-23）：** 公共仓库 Runner API 仍为 0，本机旧 Runner 任务为 Disabled 且无服务/进程；私有 `DecAgent-deploy` 仍为 404。当前工作区收紧安装器，仅允许私有部署仓库并拒绝继承公开 Runner 配置；该代码尚未提交或迁移到私有仓库，不创建 Runner，5.3 仍未完成。详见[公共 Runner 边界证据](../../../evals/integration/evidence/public-runner-boundary-20261022.md)。
- **私有仓库创建时状态快照（2026-10-23，历史）：** 已按本机私有 CD 方案创建 `shuaibilx/DecAgent-deploy`。当时 GitHub API 确认其为 private、默认分支 `main`，Actions 已启用；仓库仅含初始化 README，尚无 Environment、workflow、分支规则、GHCR 包授权或 Runner。此快照之后已将审核的私有部署包同步至其 `main`；最新 Runner、变量与本机配置状态见下方 2026-10-23 检查点及[私有部署边界证据](../../../evals/integration/evidence/private-deployment-boundary-20261023.md)。
- **评测边界：** 之前的公开首波路线保持终态，不重放；本 change 不据此宣称模型质量或 Jev 收益。
- **本轮复核（2026-10-23）：** GHCR public image 使用匿名拉取，私有 workflow 不请求 package Read。发布来源只由固定私有 workflow 的验证器输出边界保证；rollout CLI 的 digest 格式校验不是来源认证。首装恢复指纹现纳入 file-backed secret 内容摘要，记录不含明文。本机完整 deterministic `2,169 passed, 18 skipped`、guest budget PostgreSQL 集成最近复验 `38 passed`、Ruff 与 OpenSpec strict 通过。审查后的 11 文件包已同步到 DecAgent-deploy main `362a00d1604bc2daa7dcdf90d9c7b58a89e36a34`，manifest 10 个文件哈希逐一核对通过。公共发布边界更改提交 `4e932ce` 的 CI run `39935184424` 曾因测试固定预算日与系统日期不一致，在 Python 3.11/3.13 两个集成分片失败；时钟注入和 timeout 阶段分类已在 `14ffd1f` 修复，guest budget 集成模块 `36 passed`。`14ffd1f` 的同 SHA CI [run 39940453829](https://github.com/shuaibilx/DecAgent/actions/runs/39940453829) 9/9 jobs 全绿。此段部署边界状态在本机配置准备前记录；后续准备结果见[本机 SOP 预检](../../../evals/integration/evidence/local-sop-preflight-20261023.md)。此早期历史快照中的“App 凭证未配置”已被后续设置取代；App `5443230` 和 GitHub App 凭证已建立，仅对私有 `DecAgent-deploy` 的安装仍待确认，Runner 尚未注册，真实容器部署未进行，CD 开关保持关闭，5.3/5.4 未验收。详见[私有部署边界](../../../evals/integration/evidence/private-deployment-boundary-20261023.md)。
- **公共 CI 验收（2026-10-23）：** task 5.1 完成。最终 SHA `9743e1a6d303e41cd88175a97d3b386e7edfefd6` 的 [run 39951724425](https://github.com/shuaibilx/DecAgent/actions/runs/39951724425) 12/12 jobs 通过：墙钟 8:19，累计 Runner 52:05；旧 9-job 基线 `39940453829` 为 12:27、51:39。新增生产 Gateway Chromium E2E 使用锁定 Playwright 镜像的 Python 3.12，Python 3.11/3.13 主矩阵不变。两个 Python 版本的 deterministic 各为 `2,164 passed, 18 skipped`；每个版本三个 PostgreSQL shard 合计 `678 passed, 33 skipped`。跳过项和各 job 时间见[CI 验收报告](../../../evals/integration/evidence/ci-browser-sharding-20261023.md)。本机 env/secrets 和 Compose 静态预检现已完成；App 凭证已配置但单仓库安装待确认，Runner 尚未注册，真实容器部署仍未完成，5.3/5.4 未验收。
