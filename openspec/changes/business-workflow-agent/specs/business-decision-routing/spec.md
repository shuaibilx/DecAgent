# Spec Delta

## Purpose

把业务语义理解拆成具有固定含义和可追溯候选的类型化判断，使程序能够组合 Jev 的答案并解释最终路由。复杂任务由主模型处理，但模型生成的计划不得改变业务能力、操作权限或判断标准。

## ADDED Requirements

### Requirement: Typed business judgments
系统 MUST 支持 Choice、Noul 和 Score 并严格校验响应。Choice 的分支来自流程契约或真实查询候选；Noul 表示单一命题的是否概率；Score 使用版本化有序情境等级，不解释为权限或业务成功。

#### Scenario: Impact scored
- **WHEN** 对工作受阻程度进行评分
- **THEN** 使用 0 未影响、1 可绕过、2 工作受阻的初始尺度，并记录等级描述、分布、加权得分和建议

#### Scenario: Candidate absent
- **WHEN** 实体候选中没有用户要求的人员
- **THEN** 可以返回无匹配，不编造人员 ID

### Requirement: Traceable fast path and fallback
系统 MUST 保存问题与策略版本、最小化状态、候选来源、实际模型版本、原始答案和最终路线。增强流程优先规则及 Jev；基础插件、复杂请求、低确定性、无兼容校准或服务故障 MUST 进入有预算的主模型或明确询问用户。

#### Scenario: Jev unavailable
- **WHEN** Jev 返回超时或非法答案
- **THEN** 系统记录故障并有限降级，不声称 Jev 已成功判断

#### Scenario: User information missing
- **WHEN** 合法下一步依赖无法由数据或模型得到的必要参数
- **THEN** 向用户提出具体问题而不是循环猜测

### Requirement: Program owns contract and authority
主模型 MUST NOT 修改问题、选项来源、评分尺子、插件命令或执行权限。模型计划 MUST 在执行前由注册表和当前业务状态校验，确定性禁止 MUST 优先。

#### Scenario: Planner invents operation
- **WHEN** 主模型提出未注册的命令或非法状态迁移
- **THEN** 系统拒绝该动作并保留依据

### Requirement: Calibration evidence stays scoped
自动语义路线 MUST 绑定问题、实际模型、策略和数据集版本；合成参考标签 MUST 与独立人工标签区分。合成校准 MUST NOT 声称证明真实企业场景准确率。

#### Scenario: Model version changes
- **WHEN** latest 别名解析到不同实际模型版本
- **THEN** 旧校准不自动覆盖新版本，语义判断降级或等待复核

