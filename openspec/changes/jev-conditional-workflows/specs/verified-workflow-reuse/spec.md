# Spec Delta

## Purpose

让重复的已知业务复用经过验证的条件计划，以减少通用模型规划开销，同时保持每个任务独立的身份、实时数据、权限与操作回执，并能追踪模板发布和失效的原因。

## ADDED Requirements

### Requirement: Controlled workflow publication

复用计划 MUST 经校验、代表性分支与失败场景测试后由有权管理者发布，并记录内容摘要、输入及目标契约、适用范围、工具/问题/策略依赖和验证报告。一次任务成功 MUST NOT 自动发布新的通用流程。

#### Scenario: Draft from successful task
- **WHEN** 某次任务的模型计划成功完成
- **THEN** 系统可将其作为待验证候选，但不能立即供其他任务自动命中

#### Scenario: Published validated workflow
- **WHEN** 有权管理者发布已完成规定验证的计划
- **THEN** 系统保存不可混淆的发布版本和报告引用，后续实例可追溯其来源

### Requirement: Explicit applicability matching

复用 MUST 依据已注册目标、输入 schema、有效版本及当前适用条件。系统 MUST 只对明确匹配的流程实例化；模糊、不唯一或缺少必要信息时 MUST 询问或在预算内初次规划，不能仅凭文本相似度假定适用。

#### Scenario: Selected known workflow
- **WHEN** 用户选择明确流程且提供合法必填输入，所有适用条件成立
- **THEN** 系统直接实例化该计划，无需通用模型重新编排

#### Scenario: Multiple plausible workflows
- **WHEN** 当前请求同时可能匹配多个流程且无法可靠消歧
- **THEN** 系统请求澄清或受控规划，不静默任选一个写入业务

### Requirement: Fresh binding and isolated instances

每次复用 MUST 重新核对原员工当前权限、scope 和资源/目录事实，使用参数槽位绑定真实数据。新实例 MUST 有自己的任务、节点及操作身份，MUST NOT 复用历史任务的资源 ID、审批或凭据作为当前事实。

#### Scenario: Directory changes between runs
- **WHEN** 同一流程再次运行但人员目录或资格发生变化
- **THEN** 系统重新读取并使用当前候选，旧名单不能决定此次分派

#### Scenario: Historical operation identifier in template
- **WHEN** 候选模板包含上次提交的 operation_id 或确认令牌
- **THEN** 系统拒绝发布或实例化该模板，不重用历史授权

### Requirement: Verified zero-primary execution

适用复用任务 MUST 能在不调用任何通用模型的情况下完成正常路径，包括上下文整理和最终回复。Jev、工具与人工请求仍 MUST 如实计数；冷启动、未命中、异常重规划 MUST 与成功复用分别统计。

#### Scenario: Reused workflow completes
- **WHEN** 已验证流程命中且运行期间没有计划失效
- **THEN** 其通用模型实际请求总数为零，包含潜在摘要或结果渲染请求

#### Scenario: Reuse later encounters new condition
- **WHEN** 复用过程中出现模板未覆盖的新情况
- **THEN** 系统记录失效原因并按局部重规划或人工规则处理，该任务不能被计为零主模型成功复用

### Requirement: Dependency invalidation and traceability

模板依赖版本、权限适用范围或验证条件不再成立，或模板被撤销时，系统 MUST 停止新命中并重新校验未执行实例。历史任务 MUST 保留所用版本和证据，不因撤销丢失恢复信息。

#### Scenario: Contract or policy version changes
- **WHEN** 已发布模板依赖的工具契约、问题定义或阈值策略不兼容
- **THEN** 系统使模板不再适用，需重新验证发布后才能自动复用

#### Scenario: Template revoked during execution
- **WHEN** 一个未完成实例所用模板被撤销
- **THEN** 系统在下一安全边界暂停并重新校验，已提交或未知操作继续保留原回执核实路径

