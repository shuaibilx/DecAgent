# Spec Delta

## Purpose

把 Jev 的答案确定性用于可解释、可验证的自动推进与人工接管，让用户能够在合法范围内选择阈值，同时保证模型分数不会被误认为正确率、统计置信区间或业务操作权限。

## ADDED Requirements

### Requirement: Primitive-specific certainty semantics

系统 MUST 保留 primitive、原始答案、概率、原始 confidence 和实际模型版本。Choice/Score MUST 使用其 confidence 进行对应策略门控；Noul MUST 保存 p_yes，统一展示时使用明确命名的 derived_certainty=abs(2p_yes-1)，不得伪称服务返回 confidence。

#### Scenario: Noul near uncertainty
- **WHEN** Noul 返回 p_yes=0.96 且 derived_certainty 阈值为 0.95
- **THEN** 归一值为 0.92，当前关键决定转人工，不能按 0.96 大于 0.95 自动通过

#### Scenario: Noul exact tie
- **WHEN** p_yes=0.5
- **THEN** 系统视为未决，不因配置阈值为零就自动选择是或否

#### Scenario: Score value distinct from certainty
- **WHEN** Score 返回最高严重性档位但 confidence 较低
- **THEN** 系统按 confidence 判断是否需人工，不把严重性档位当作确定性

### Requirement: Validated versioned threshold policy

自动消费关键决定 MUST 使用与问题、模型实际版本、候选范围、归一规则、风险和校准数据相符的有效策略。达到阈值的定义 MUST 为 certainty >= threshold。无有效策略、异常分布或版本漂移 MUST 进入明确待处理状态，不静默转主模型判断。

#### Scenario: Exact threshold equality
- **WHEN** 有效答案的 certainty 恰等于匹配策略阈值
- **THEN** 答案可进入后续参数与权限校验，不能因边界实现不一致而随机转人工

#### Scenario: Latest alias resolves to new model
- **WHEN** Jev latest 的实际模型版本不再匹配现有策略
- **THEN** 系统使该自动策略不可用，记录原因并等待有效策略或人工，不能沿用旧校准声明

### Requirement: User-configurable bounded threshold

用户 MUST 能在已验证策略允许范围内设置任务阈值，并看到其含义及版本。任务阈值 MUST NOT 低于管理员策略下限或改变强制确认规则；未验证阈值只能在明确的隔离评测模式试验。正在等待的任务 MUST 保留原决策与策略历史。

#### Scenario: Employee selects stricter setting
- **WHEN** 员工选择已验证范围内更高阈值
- **THEN** 后续待消费判断按新阈值评估并记录生效事件，历史已执行决定保持原记录

#### Scenario: Attempt to bypass floor
- **WHEN** 请求把阈值设为策略最低允许值以下
- **THEN** 服务端拒绝并说明可用范围，不因前端参数而扩大自动执行权限

### Requirement: Direct intervention on active uncertain decisions

当前实际分支的关键决定低于阈值时，系统 MUST 持久暂停并请求人选择、更正或补充。低分本身 MUST NOT 触发主模型 fallback；未激活分支 MUST NOT 产生接管。无回复 MUST 保持等待。

#### Scenario: Low certainty in active branch
- **WHEN** 当前分支需要一个未达到阈值的 Choice 答案
- **THEN** 系统创建一次可恢复人工待办，展示合法候选、证据和 Jev 建议，并不增加主模型请求

#### Scenario: Memory write proposal is uncertain
- **WHEN** Jev 对一条记忆写入提议的答案未达到当前已发布策略阈值
- **THEN** 系统将提议标记为待复核且不创建或激活记忆；不能用“可选记忆”规则自动跳过证据或本人确认

#### Scenario: No response after restart
- **WHEN** 等待中的任务重启且员工未回复
- **THEN** 原待办继续存在，任务不自动同意或重新发起相同模型请求

### Requirement: Authentic and current human decisions

人工回复 MUST 绑定原员工、任务、节点、计划版本、问题和证据/候选摘要，并在消费时重新验证身份、权限和新鲜度。系统 MUST 支持合法选择、无匹配、补充和拒绝；重复回复只消费一次，失效回复不得推进。

#### Scenario: Candidate no longer authorized
- **WHEN** 员工回复前候选人员已失去合法资格
- **THEN** 系统拒绝按旧候选执行，刷新状态并重新展示可用选择

#### Scenario: Duplicate or foreign reply
- **WHEN** 同一回复重复提交，或另一员工向待办提交选择
- **THEN** 重复不产生第二次推进，非原员工回复被拒绝，均不触发副作用

#### Scenario: User reports no suitable choice
- **WHEN** 用户选择无匹配或补充事实而非现有候选
- **THEN** 系统记录纠正，走补充信息或计划失效路径，不把文本强行映射到 Jev 原建议

### Requirement: Certainty never grants business authority

高确定性或人工语义确认 MUST NOT 绕过当前权限、资源版本、硬规则或单独的具体写入确认。系统 MUST 在界面与审计中区分语义接管、参数补充、写入确认和结果核实。

#### Scenario: Certain but unauthorized action
- **WHEN** Jev confidence 很高但操作超出当前员工权限
- **THEN** 操作被拒绝，不能通过提高阈值或让员工选择“继续”授权

#### Scenario: Semantic choice followed by write
- **WHEN** 人工已决定下一业务分支且分支包含敏感写入
- **THEN** 系统仍生成具体操作确认，语义回复不能自动消费为该确认

