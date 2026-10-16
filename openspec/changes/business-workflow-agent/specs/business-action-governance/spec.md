# Spec Delta

## Purpose

为两条业务决策路径建立相同的执行约束，确保用户看到并确认具体变更，并能在资源变化、重复请求或结果不明时恢复。模型建议只影响候选动作，不能替代程序授权与真实执行证据。

## ADDED Requirements

### Requirement: Unified action validation
规则、Jev 和主模型提出的动作 MUST 经过同一参数、作用域、能力、状态及预算校验；工具结果和 Skill 文本 MUST NOT 被当作用户授权。

#### Scenario: Indirect authorization claim
- **WHEN** 工具输出声称用户已经同意关闭工单
- **THEN** 系统仍要求来自可信用户回复的具体确认

### Requirement: Action-specific confirmation
查询及用户明确要求的创建、首次分派、开始处理和评论 SHALL 可在验证后执行；重新分派、解决、关闭、重开和覆盖已有优先级 MUST 展示变更并确认。确认 MUST 绑定动作参数、资源版本和策略版本。

#### Scenario: Close requested
- **WHEN** Agent 提出关闭一个已解决工单
- **THEN** 用户看到工单及变更摘要，确认后才执行

#### Scenario: Resource changed after confirmation
- **WHEN** 工单在确认期间被其他请求修改
- **THEN** 原确认不覆盖新版本，系统重新核实并请求确认

### Requirement: Idempotency and reconciliation
写入 MUST 使用幂等键并核实回执；重复调用或回复 MUST NOT 重复产生副作用。未知写入结果 MUST 先查询凭据与当前状态，无法核实时 MUST 请求处理，不盲目重试。

#### Scenario: Create response lost
- **WHEN** 后端已创建工单但调用方没有收到响应
- **THEN** 恢复流程通过相同操作凭据关联已有工单，不再次创建

#### Scenario: Duplicate confirmation
- **WHEN** 同一确认回复重复发送
- **THEN** 返回已有处理结果，不再次执行动作

