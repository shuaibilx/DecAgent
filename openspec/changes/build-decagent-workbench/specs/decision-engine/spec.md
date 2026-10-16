# Spec Delta

## Purpose

将有限选项的语义判断独立于代码生成与实际执行，使工具风险、人工介入和记忆治理拥有一致的决策记录。该能力需要正确处理模型概率、版本变化与故障降级，并支持验证判断组件的实际效果。

## ADDED Requirements

### Requirement: Dedicated typed decisions
系统 SHALL 使用独立决策服务处理工具风险、是否需要人工介入、记忆候选价值和记忆检索相关性；每类问题 MUST 使用明确选项或二元概率。决策服务 MUST NOT 承担代码生成或实际执行。

#### Scenario: Tool risk is evaluated
- **WHEN** 一个已通过基本权限检查的操作需要语义风险判断
- **THEN** 系统提交与该操作相关的最小证据，并获得允许、拒绝或人工审核的结构化结果

#### Scenario: Memory relevance is evaluated
- **WHEN** 当前任务存在候选项目记忆
- **THEN** 系统判断是否检索及哪些候选相关，而不要求决策服务生成记忆事实

### Requirement: Strict decision response validation
系统 MUST 校验返回的题目标识、类型、选项、概率范围、概率分布和模型标识；缺失、未知选项、类型错误或非有限数值 MUST 按不可用处理，不得通过通用真值转换变成允许。

#### Scenario: String false is returned
- **WHEN** 二元判断返回字符串 `false` 而不是约定类型
- **THEN** 系统拒绝该响应并进入降级流程，不将字符串解释为真

#### Scenario: Choice contains an undeclared option
- **WHEN** 响应选项不属于提交的问题选项集合
- **THEN** 系统记录无效响应且不执行依赖该判断的动作

### Requirement: Versioned decision policies
每类问题的选项、输入格式、阈值及降级规则 MUST 版本化；系统 MUST 记录请求模型和实际返回模型。未校准的策略 MUST 采用保守行为，不将高置信度直接解释为安全证明。

#### Scenario: Model alias resolves to a new version
- **WHEN** 同一模型别名返回不同模型版本
- **THEN** 记录体现版本变化，旧校准结论不会自动当作新版本已经验证

#### Scenario: No calibrated policy exists
- **WHEN** 新实例尚未导入适用于当前模型和问题版本的校准策略
- **THEN** 仅确定性低风险行为自动继续，其余执行动作审核，自动记忆激活等待校准或审核

### Requirement: Correct probability semantics
系统 MUST 区分二元概率与供应商提供的置信统计；未返回置信字段时 MUST NOT 编造该字段。界面与导出 MUST 明示概率信息不等于实际正确率。

#### Scenario: Binary answer lacks confidence
- **WHEN** 决策服务返回有效二元概率但没有独立 confidence
- **THEN** 系统保存并展示该概率，不宣称收到供应商置信度

### Requirement: Bounded failure degradation
决策请求 MUST 有超时和有限重试；不可用时，确定性规则允许的低风险操作 SHALL 继续，其余依赖决策的操作 MUST 转人工确认，自动记忆写入与激活 MUST 暂停。

#### Scenario: Quota exhausted
- **WHEN** 决策服务在有限重试后仍报告额度或速率限制
- **THEN** 任务明确显示降级原因，安全读取继续，需要该判断的执行操作等待审核

#### Scenario: Memory decision unavailable
- **WHEN** 记忆候选价值或激活判断无法完成
- **THEN** 系统保留任务内证据及待审候选，不将其发布为活跃项目记忆

### Requirement: Traceable outcomes and reusable decisions
系统 MUST 保存脱敏输入摘要与哈希、问题及策略版本、模型结果、最终治理结果、时间和使用量。只有输入、相关版本和策略一致时 SHALL 复用判断；记录 MUST 将模型建议与实际处理分开。

#### Scenario: Hard policy overrides model advice
- **WHEN** 模型建议允许，但执行前再次校验发现任务授权不足
- **THEN** 记录同时包含模型建议和程序拒绝，操作不执行

#### Scenario: Context changes before reuse
- **WHEN** 代码、证据或策略与旧判断不同
- **THEN** 系统不复用旧判断，重新评估或请求人工介入
