# Spec Delta

## Purpose

让单用户能够通过默认业务工作台发起自然语言请求、检查决策与执行进度、回复必要问题并确认业务变更。界面必须体现真实业务状态及等待原因，同时保留历史修复任务的查看和操作入口。

## ADDED Requirements

### Requirement: Default business workspace
默认首页 SHALL 提供插件能力与业务任务创建入口；修复入口 SHALL 为非默认模式。业务任务 MUST 展示请求、当前状态、任务结果、关联工单及决策路线。

#### Scenario: Create business task
- **WHEN** 用户选择已注册插件并提交业务指令
- **THEN** 无需填写仓库或提交版本，即可查看任务和后续进度

### Requirement: Inspect and resume
用户 MUST 能查看 Jev 问题、候选、原始判断和最终路由，回复澄清问题并对具体业务动作批准、拒绝或修改。事件断线重连 MUST 保持顺序，不重复显示已处理动作。

#### Scenario: Human reply
- **WHEN** 任务询问应选择哪个同名人员
- **THEN** 用户回复后继续同一任务，选择结果和操作凭据可检查

### Requirement: Documentation matches behavior
使用指南、架构说明和图册 MUST 同步实际业务能力、运行命令、确认策略和限制；生成交付 MUST 放入独立 generated 目录，且 MUST 不将未验收部署或未证明加速描述为完成。

#### Scenario: Read architecture
- **WHEN** 用户打开项目说明和图册
- **THEN** 能看到插件、模型、规则、确认、CLI、业务服务及持久化之间的流程和时序

