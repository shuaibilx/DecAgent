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

- **已完成：** tasks 1.1–1.4（4/28）：legacy/workspace 迁移与身份解析、目录/工单/任务/操作 ACL、任务子记录归属、审批/trace/export 与 Worker/MCP 恢复隔离。Schema migration 020 建立空间及同空间外键，021 由可信父任务/操作派生子记录 workspace。
- **验证状态：** 2026-10-21 隔离 PostgreSQL 的核心员工、操作、工单 ACL、MCP 与 Gateway 组合 81 项通过；扩展运行时/恢复/记忆/计划组合初次为 190 passed、2 failed，失败来自历史 schema fixture 使用新 workspace 字段。将该 fixture 改为按旧表结构造数后，计划/记忆/迁移相关整组 34 项通过。Ruff 通过；OpenSpec strict 为 6 passed、0 failed。测试不包含真实游客生命周期或付费模型调用。
- **仍待实施：** tasks 1.5–6.2（24 项）：记忆空间隔离、迁移说明、游客 session/角色/清理/UI、试用账本与队列、SOP Compose、私有 CD 和最终端到端验收。游客入口保持关闭；公开 release 不负责调度本机 Runner，正式私有 CD 仍待建置与演练。
- **评测边界：** 之前的公开首波路线保持终态，不重放；本 change 不据此宣称模型质量或 Jev 收益。
