# Spec Delta

## Purpose

提供一个可独立部署、具有真实持久化状态和执行凭据的企业服务工单业务，用于验证 Agent 的完整工作链路。业务数据来自合成目录和用户记录，不模拟已经完成企业系统修复，也不依赖原公司的后端。

## ADDED Requirements

### Requirement: Full ticket lifecycle
业务服务 SHALL 支持查询、创建、分派、开始处理、评论、记录解决、关闭、重开及优先级变更，使用 open、assigned、in_progress、resolved、closed 状态，并验证合法迁移。

#### Scenario: Full lifecycle
- **WHEN** 按合法顺序处理工单并提供解决记录
- **THEN** 各次变更可查询，关闭后可重开，评论和历史保留

#### Scenario: Invalid close
- **WHEN** 请求直接关闭尚未解决的工单
- **THEN** 服务拒绝迁移，不由 Agent 评分绕过

### Requirement: Real directory candidates and revisions
人员和分类 MUST 从业务目录查询且保留唯一 ID；重名 MUST 不被静默消除。更新 MUST 使用当前资源版本并在冲突时拒绝过期写入。

#### Scenario: Duplicate names
- **WHEN** 用户指定一个对应多个目录人员的姓名
- **THEN** 返回可区分的候选资料，Agent 请求选择

### Requirement: CLI receipts
JSON CLI MUST 提供结构化成功、失败和执行凭据查询，写入及回执 MUST 在同一事务中提交。服务 MUST 拒绝相同幂等键绑定不同动作。

#### Scenario: Same key different content
- **WHEN** 客户端重用已执行键但改变参数
- **THEN** 返回冲突且不执行新变更

