# Spec Delta

## Purpose

保证已验证条件计划能够在有限预算内按真实业务状态持续执行，在进程重启、人工等待和局部重规划后继续原任务，并避免重复副作用、越权推进或隐藏的额外模型调用。

## ADDED Requirements

### Requirement: Durable node execution

系统 MUST 持久保存计划版本、实际激活分支、节点状态、输入来源、尝试次数、已接受决策及操作证据。恢复 MUST 继续原节点或核实原操作，不能以聊天历史或重新规划替代已发生事实。

#### Scenario: Crash after read completion
- **WHEN** 读取结果已经持久保存而进程在推进下一节点前退出
- **THEN** 恢复使用已保存且仍有效的结果，或明确刷新过期数据，并记录来源及刷新原因

#### Scenario: Crash after remote write
- **WHEN** 业务提交可能已成功而本地节点尚未保存成功
- **THEN** 恢复查询原操作回执，不创建另一个写入操作来代替核实

### Requirement: Dependency-correct Jev batching

系统 MUST 仅批量请求依赖已就绪、状态相容且互不依赖答案的 Jev 问题，并遵守问题数、候选数与状态大小限制。同请求中后一个问题 MUST NOT 读取尚未产生的前一个答案。

#### Scenario: Questions share fixed state
- **WHEN** 两个问题使用同一业务观察且没有答案依赖
- **THEN** 系统可以合并请求，同时分别保存答案与问题身份，并记一笔真实请求及两个问题

#### Scenario: Dynamic candidates require later tool result
- **WHEN** 人员候选必须由前序分支选定后的工具读取
- **THEN** 系统等待工具结果再构造该判断，不发送虚构或过期名单

#### Scenario: Unused speculative result
- **WHEN** 已记录的推测答案所属分支未被激活
- **THEN** 系统保留其费用和审计，不依据该答案触发人工待办或写入

### Requirement: Restricted local replanning

局部重规划 MUST 有新事实、未覆盖情况、能力失效或明确目标变更的原因，且只修改允许的未执行部分。已提交、已准备、待确认或结果未知的操作及其必要依赖 MUST 保留身份与证据。计划修订 MUST 原子验证父版本并受总修订上限约束。

#### Scenario: New unsupported observation
- **WHEN** 实际工具结果使尚未执行的计划前提失效
- **THEN** 系统在预算内向规划器提供触发证据与冻结区，只接受通过校验的局部修订

#### Scenario: Replan would repeat a completed write
- **WHEN** 候选修订删除已提交节点或换操作 ID 重做其副作用
- **THEN** 系统拒绝修订，保留原回执，不执行重复写入

#### Scenario: Concurrent plan edits
- **WHEN** 两份修订引用同一父版本而其中一份已发布
- **THEN** 另一份不能覆盖最新版本，须重新核验或停止

### Requirement: Separate model and execution budgets

系统 MUST 分别限制通用模型、Jev、总模型请求、token、业务执行次数、修订次数、活跃时长及费用，并原子预留。正常任务以 1–3 次通用模型请求为目标；默认硬上限为 primary 6、Jev 24、总模型 30、计划版本 4，超过普通目标须记录异常原因。

#### Scenario: Primary quota exhausted with executable plan
- **WHEN** 主模型额度耗尽但已有合法计划且其他相关预算仍充足
- **THEN** 系统仍能执行工具或在 Jev 额度内判断，不能仅因 primary 已耗尽阻断它们

#### Scenario: All model calls exhausted but no model needed
- **WHEN** 模型请求或 token 额度耗尽，而下一节点不需模型且整体执行预算有效
- **THEN** 系统允许该合法节点执行，同时禁止新模型请求

#### Scenario: Overall hard boundary reached
- **WHEN** 任务已取消、租约失效、总费用约束未知或耗尽、活跃时间或执行次数超限
- **THEN** 系统停止新业务动作；原操作只读核实按已有核实规则处理，不生成新授权

### Requirement: Account for every actual request

所有规划、格式修复、提供方重试、摘要、最终回复生成和 Jev 请求 MUST 使用同一可审计预算机制。计数 MUST 依据实际请求，不能依据 CLI 进程数；响应无效但已产生费用仍 MUST 记账，未知用量 MUST 保留约束。

#### Scenario: Valid transport response with invalid plan
- **WHEN** 提供方返回用量但计划校验失败
- **THEN** 系统记实际用量与失败原因，修复请求另计一次，不将失败请求抹去

#### Scenario: Crash leaves dispatch uncertain
- **WHEN** 无法证明模型请求从未发送，且没有完整费用记录
- **THEN** 系统保留未知预留，不通过新请求键绕过限制

### Requirement: Preserve governed side effects

每个业务写入 MUST 继续绑定原员工身份、当前权限、规范参数、资源与契约版本、独立确认和幂等回执。Jev 高分、人工语义选择、计划复用与记忆 MUST NOT 替代这些条件。

#### Scenario: Human selected assignee
- **WHEN** 员工在低确定性待办中选择合法处理人
- **THEN** 系统仍需准备实际分派参数并完成原有写入确认，才能提交

#### Scenario: Unknown commit outcome
- **WHEN** 提交超时且服务端尚不能证明是否成功
- **THEN** 任务停在原操作核实状态，不自动改 ID 重试或声称失败后继续

### Requirement: Version-aware recovery compatibility

新任务 MUST 使用条件计划运行版本；已有 business-2 任务 MUST 保持原版本恢复语义。系统 MUST 区分已支持旧版本与未知版本，不能重标旧 checkpoint 或把新任务交给旧解释器。历史仓库修复能力 MUST 保持隔离。

#### Scenario: Mixed supported task versions
- **WHEN** 待执行队列同时存在旧业务任务与新条件计划任务
- **THEN** 两类任务由各自兼容运行器处理，共存不导致全部任务被版本检查拒绝

#### Scenario: Rollback new task creation
- **WHEN** 关闭新运行版本入口但存在未完成新任务
- **THEN** 这些任务由兼容运行器恢复或明确暂停，其记录和操作身份不会被删除或转成旧格式

### Requirement: Lifetime execution bounds

计划规模、分支深度、节点尝试、内部控制步骤和重试 MUST 有持久上限，不能因重启、人工恢复或局部修订归零。默认展开后节点至多 32、嵌套深度 4、节点尝试 128；无界等待不得被转换为静默成功。

#### Scenario: Repeated resumes
- **WHEN** 同一任务多次恢复或修订
- **THEN** 其累计执行计数继续增长，达到上限进入明确等待或终止状态，未知结果仍保留核实路径

