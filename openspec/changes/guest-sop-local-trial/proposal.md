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
