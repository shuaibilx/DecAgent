# Spec Delta

## Purpose

将修复过程中产生的知识转化为有来源、适用范围和失效条件的项目记忆，使后续任务能够复用经过验证的事实而不继承草稿分支、错误推断或其他仓库的信息。用户可以审核、纠正并撤销这些记忆。

## ADDED Requirements

### Requirement: Evidence-bearing memory candidates
记忆候选 MUST 保存项目、来源任务、事实或决策类别、内容、证据、相关提交及适用性依据；秘密、暂时日志和缺乏支持的推测 MUST NOT 自动成为项目记忆。

#### Scenario: Tested cause becomes a candidate
- **WHEN** 一个问题原因得到可追溯复现与验证证据支持
- **THEN** 系统生成带证据的候选，并进行价值、敏感性和冲突检查

#### Scenario: Unsupported inference extracted
- **WHEN** 主模型提取了日志无法支持的原因
- **THEN** 系统将其保留为待审推测或丢弃，不标记为已验证事实

### Requirement: Confirmed design decisions
设计决策候选 MUST 引用用户对该决策的明确确认；模型推测、仓库指令或无关操作的审批 MUST NOT 被当作设计确认。与未合并代码有关的决策 MUST 遵守合并后激活规则。

#### Scenario: User confirms an architectural choice
- **WHEN** 用户明确确认一项项目设计选择
- **THEN** 候选关联该确认记录，后续按适用范围和激活规则处理

#### Scenario: Command approval is misinterpreted
- **WHEN** 模型试图把用户对一次命令的批准解释为永久设计偏好
- **THEN** 系统拒绝这种确认关联，不自动写入设计记忆

### Requirement: Task-local use before merge
源自修复分支的候选 MUST 在合并前限定为当前任务使用；其他任务和主分支上下文 MUST NOT 将其当作活跃项目事实，即使分支测试已经通过。

#### Scenario: Draft PR has passing checks
- **WHEN** 草稿 PR 验证通过但尚未合并
- **THEN** 相关候选仍仅供来源任务使用，不出现在其他任务检索结果中

### Requirement: Activate only after confirmed applicability
系统 MUST 核实对应 PR 已合并且候选仍适用于实际合并版本后，才允许激活分支知识。证据充分且无冲突的事实 SHALL 分级自动处理；证据不足、冲突或决策不可用 MUST 转审核。

#### Scenario: Compatible merged change
- **WHEN** PR 已合并、相关来源与验证依据在合并版本仍有效，且治理通过
- **THEN** 候选以可追溯方式激活为该项目的记忆

#### Scenario: Merge changes the supporting code
- **WHEN** 合并解决冲突后，支持候选的代码与原验证版本不同
- **THEN** 系统保持候选未激活，要求重新验证或人工审核

#### Scenario: PR closed without merge
- **WHEN** 来源 PR 被关闭但未合并
- **THEN** 相关候选不激活，来源任务证据仍可查看

### Requirement: Conflict-safe and idempotent updates
重复处理同一证据 MUST NOT 产生重复活跃记忆；新候选与已有记忆冲突时 MUST 保留双方来源并请求审核，不得静默覆盖。纠正与撤销 MUST 留下版本关系并影响后续检索。

#### Scenario: Activation is retried
- **WHEN** 同一候选的激活事件被重复处理
- **THEN** 系统返回同一激活结果，不增加重复记忆

#### Scenario: User revokes a memory
- **WHEN** 用户撤销已激活记忆
- **THEN** 新任务不再检索该记忆，既有使用记录仍能解释此前上下文

### Requirement: Project and version scoped retrieval
检索 MUST 限于当前项目、已激活且适用于当前版本的记忆，并受相关性判断约束；过期、撤销、冲突待审或其他项目内容 MUST NOT 自动进入上下文。记忆内容 MUST 作为带来源数据而非权限指令。

#### Scenario: Relevant active memory found
- **WHEN** 当前问题需要一个适用于该版本的已激活项目事实
- **THEN** 上下文包含该事实与证据引用，并记录检索和选择结果

#### Scenario: Memory contains permission instructions
- **WHEN** 记忆文本要求忽略审批或读取密钥
- **THEN** 这些内容不能改变权限，系统按不可信内容治理并记录异常

### Requirement: Bounded context with preserved evidence
上下文组织 MUST 受配置预算约束，保留当前授权、待审批操作、关键验证依据和完整工具调用结果配对；压缩 MUST NOT 改写原始审计事件或取代任务检查点。

#### Scenario: Context exceeds the budget
- **WHEN** 对话和工具输出超过上下文预算
- **THEN** 系统生成可追溯摘要或选择必要片段，原始事件与证据仍可查看
