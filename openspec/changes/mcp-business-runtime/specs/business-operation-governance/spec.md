# Spec Delta

## Purpose

使 DecAgent 和外部 MCP 客户端共享可检查的业务操作与批准记录。准备、人工确认、提交和核实拥有清晰语义，模型提案不能自授权，重复请求和故障恢复不会通过换操作 ID 重复产生业务副作用。

## ADDED Requirements

### Requirement: Durable immutable preparation
每个写工具 MUST 准备一个持久化操作，绑定员工、请求键、命令、规范化参数、资源版本和契约版本，并返回操作 ID 与状态。准备 MUST 校验业务输入和权限，但 MUST NOT 修改工单或产生业务提交回执；已准备参数 MUST 不被原地覆盖。

#### Scenario: Prepare a create
- **WHEN** 有权限的员工准备创建工单
- **THEN** 返回可查询操作句柄，工单数量、历史和业务回执数量均不增加

#### Scenario: Repeated preparation
- **WHEN** 同一员工重用请求键并提交相同命令和参数
- **THEN** 返回原操作；改变命令或参数则返回冲突，不产生新业务变更

### Requirement: Unified server-owned approval
业务服务 MUST 是批准与提交状态的统一事实源。所有客户端 MUST 遵守相同确认策略；敏感操作只能由操作绑定员工的已认证网页会话批准或拒绝。MCP MUST NOT 暴露自批准工具或接受客户端提交的批准布尔值。

#### Scenario: Direct MCP bypass attempted
- **WHEN** 外部客户端提交待确认操作或声称已在工具文本中获得同意
- **THEN** 返回待确认状态与网页入口，不执行工单写入

#### Scenario: Another employee approves
- **WHEN** 其他员工或拥有全局查询权限的主管确认别人的操作
- **THEN** 拒绝该确认，查询权限不转化为代签权限

### Requirement: Specific expiring confirmation
确认 MUST 绑定员工、操作 ID、命令、参数摘要、资源修订号和策略版本，未提交确认默认 15 分钟失效。参数编辑或资源、契约、权限变化 MUST 使旧确认不能提交；修改参数 MUST 产生新操作并保留关联审计。

#### Scenario: Confirmed resource changes
- **WHEN** 工单在批准后、提交前被其他合法请求修改
- **THEN** 原操作不提交，重新读取并准备新操作，必要时再次确认

#### Scenario: Approval expires
- **WHEN** 客户端在确认有效期之后提交尚未完成的敏感操作
- **THEN** 返回确认失效，不写入资源；已完成操作仍可查询历史结果

### Requirement: Guarded commit
提交 MUST 使用已保存操作，不接受替换业务参数；MUST 验证当前员工、权限、契约和资源版本。关联 Agent 任务的提交 MUST 同时验证其执行授权、当前租约、取消与预算状态，普通客户端不得绕过这些任务守卫。

#### Scenario: Commit a valid operation
- **WHEN** 原员工提交已就绪且所有守卫有效的操作
- **THEN** 执行保存的确切动作并保存真实结果，提交者不能顺带改变参数

#### Scenario: Stale worker or cancelled task
- **WHEN** 过期 Worker 或普通客户端提交属于已取消、预算耗尽或已被接管任务的操作
- **THEN** 不启动新业务写入，并保留可检查原因

### Requirement: Transactional employee-bound receipts
业务写入、资源历史和业务回执 MUST 在同一事务提交。回执 MUST 绑定操作 ID、员工、命令、输入摘要、资源和修订号。同一操作的并发或重复提交 MUST 只产生一次已提交业务变更；不同输入或身份不得重用其结果。

#### Scenario: Concurrent duplicate commit
- **WHEN** 同一操作被两个请求同时提交
- **THEN** 只存在一次业务变更及对应历史和回执，调用方取得同一已提交结果

#### Scenario: Different employee reuses handle
- **WHEN** 另一员工使用已有操作 ID 提交或取得受限回执
- **THEN** 服务按身份拒绝，不返回原员工的业务内容或执行新写入

### Requirement: Reconciliation before retry
派发后超时、断连或进程终止 MUST 保留原操作并标记待核实，不将传输取消等同于事务撤销。恢复 MUST 先核实匹配回执与资源历史；无充分证据时保持 `unknown`，不得换 ID 重写。仅证明未提交且无在途执行时才可按原操作重新提交。

#### Scenario: Commit succeeds but response is lost
- **WHEN** 事务已提交但客户端未收到结果
- **THEN** 通过原操作回执和资源历史恢复成功，不重复调用业务写入

#### Scenario: No conclusive evidence
- **WHEN** 恢复查询无法证明是否发生提交
- **THEN** 保留 `unknown` 和人工核实入口，不把无响应或暂时查不到回执当作未执行

### Requirement: Evidence-specific completion
准备成功、批准成功或 MCP RPC 成功 MUST NOT 被视为业务完成。写入成功 MUST 由已核实回执支持；任务完成 MUST 覆盖实际目标和全部已执行写入，并允许省略辅助读取。权限或状态规则不得通过 Jev 或模型解释绕过。

#### Scenario: Prepared action described as completed
- **WHEN** 模型仅引用准备句柄就声称工单已创建
- **THEN** 拒绝完成声明并保留实际操作状态

#### Scenario: Create and assign goal proven
- **WHEN** 已核实创建和分配同一工单的目标动作及所有写入
- **THEN** 可以完成对应 Agent 任务，不要求工单进入解决或关闭状态

