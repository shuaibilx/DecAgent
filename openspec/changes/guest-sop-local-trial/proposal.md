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

- **已完成：** tasks 1.1–1.6、2.1–2.4、2.6、3.1、3.3–3.5、4.1–4.2（17/28）：legacy/workspace 迁移与身份解析、目录/工单/任务/操作 ACL、任务子记录归属、审批/trace/export 与 Worker/MCP 恢复隔离、workspace 记忆与稳定槽隔离、游客 session/角色/生命周期、预算账本与费用说明、持久队列及匿名请求限流/受信代理、SOP 专用 Compose/配置预检、真实业务 readiness 与 Worker 故障恢复烟测。迁移 020–030 建立 workspace、游客生命周期、预算结算、执行队列与限流边界。
- **部分完成：** task 3.2 的模型 attempt 原子预留/结算及 unknown 保留已有隔离测试。2026-10-22 增加 guest Jev fail-closed 门禁：在没有冻结费率上界及强制 token 限制时，于创建 task/trial 预留之前拒发；隔离 PostgreSQL 的新回归验证未调用 provider 且无账本记录，预算集成模块 28 项通过。固定 `$0.01` 仍不是费用上界，因此游客 Jev 目前不可用；3.2 保持未勾选，直到实际支持可靠上界并完成相应验收。游客入口默认关闭。
- **验证状态（2026-10-21）：** 本轮隔离 PostgreSQL 游客生命周期测试 19 项通过；Worker、操作回执、workspace 迁移与游客清理联合回归 67 项通过；目标 Ruff 检查通过。覆盖在途 Worker 延后写 checkpoint 后再清理、独立进程锁等待、租约保护、清理证据歧义保留、已验证 receipt、跨 Cleaner 重启幂等、自动过期与重复 clear。此前游客浏览器首次进入、角色切换、刷新恢复和清空回登录页的 Playwright CLI 实测通过；Node 工作台回归通过。更早的 deterministic 全集记录为 1937 passed、18 skipped（τ 固定子进程运行时未配置及 Windows symlink 环境限制），不代表本轮新改动已重跑全套 deterministic。无模型调用或付费评测。
- **仍待实施：** tasks 2.5、3.2、4.3–6.2（11 项未验收）：游客 UI 的真实 Jev 人工建议闭环、Jev 费用上界、兼容升级/回滚、Windows/Runner 恢复、私有 CD 和最终集成验收。4.3 已有只接受 GHCR digest 的发布协调器、备份清单和兼容应用回滚路径，但真实容器/卷的成功升级、健康失败回滚及 schema 不兼容停服演练尚未执行，因此仍保持未勾选。游客入口由 `DECAGENT_GUEST_TRIAL_ENABLED` 控制且默认关闭；公共 release 不负责调度本机 Runner，正式私有 CD 仍待建置与演练。4.1–4.2 只证明本机 disposable stack 的配置、业务 readiness 和故障检测恢复，不代表备份回滚、生产可用性或公网验收完成。
- **部署边界复核（2026-10-21）：** GitHub API 显示公共仓库 self-hosted Runner 数量为 0；现有工作流没有自托管 deployment job，GHCR 仍由 GitHub-hosted runner 发布；本机 `DecAgentLocalDeployRunner` 计划任务为 Disabled，Runner 服务/进程为 0。公共 CI、GHCR 和现有容器未改动。5.3 的私有仓库、可信触发器与私有 Runner 迁移仍未开始，因此该复核不计为 CD 完成。
- **评测边界：** 之前的公开首波路线保持终态，不重放；本 change 不据此宣称模型质量或 Jev 收益。
