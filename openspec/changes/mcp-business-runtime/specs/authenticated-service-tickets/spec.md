# Spec Delta

## Purpose

提供具有真实持久化数据、员工归属和职责约束的合成企业工单业务。系统用于验证业务自动化、人工确认和可靠执行，保留现有生命周期与目录消歧能力，明确其不是公司真实 API 或实际设备维修服务。

## ADDED Requirements

### Requirement: Persistent authenticated ticket lifecycle
业务 MUST 保留目录、创建、分配、开始、评论、解决、关闭、重开、调整优先级、单条查询、列表和回执查询十二项能力。工单 MUST 按 open、assigned、in_progress、resolved、closed 合法流转，结果、历史和回执来自持久化数据而非固定返回值。

#### Scenario: Multi-role lifecycle
- **WHEN** 员工创建、主管分配、处理人开始并解决、员工批准关闭本人已解决工单
- **THEN** 每一步具有真实可查询状态、正确操作者、连续修订号与提交证据

#### Scenario: Close before resolution
- **WHEN** 有权限的员工要求关闭尚未解决的工单
- **THEN** 拒绝非法迁移，权限或确认不能覆盖状态限制

### Requirement: Responsibility-based action permissions
所有角色 SHALL 为本人创建工单。员工 SHALL 评论、关闭或重开本人适用工单；处理人 SHALL 评论本人或获分配工单、开始或解决获分配工单，并保留员工本人权限。仅主管 SHALL 分配、改派和调整已有优先级，且 SHALL 管理全部合法工单动作。

#### Scenario: Employee assigns another person
- **WHEN** 普通员工准备分配或改派工单
- **THEN** 返回权限不足，即使该员工是请求者或提供了确认

#### Scenario: Handler closes someone else's request
- **WHEN** 处理人准备关闭自己处理但由别人提出的工单
- **THEN** 拒绝关闭；本人请求者或主管才可发起该合法动作

#### Scenario: Supervisor performs a sensitive change
- **WHEN** 主管改派或调整已有优先级
- **THEN** 角色允许仍需具体人工确认，操作只影响展示的工单与参数

### Requirement: Principal-bound creation and real directory selection
新工单请求者 MUST 从可信调用员工身份确定，不能代填其他请求者。分类和经办人 MUST 使用真实目录 ID；经办人 MUST 是有效处理人或主管。重名 MUST 返回可区分候选，必要事实不足时 MUST 澄清，不编造 ID。

#### Scenario: Caller supplies another requester
- **WHEN** 创建请求试图指定其他员工为请求者
- **THEN** 参数或权限校验拒绝，不能冒充另一员工创建

#### Scenario: Ambiguous handler name
- **WHEN** 主管提供对应两个可分配人员的同名姓名
- **THEN** 保留 ID 和团队的候选，明确选择后再准备分配

### Requirement: Fresh revision and retained evidence
每个已有工单写入 MUST 使用当前修订号并验证合法引用；过期修订号 MUST 不产生业务变更。评论和历史 MUST 保留真实操作者。解决记录 MUST 来自员工明确提供并认可的内容，不能被解释为已经修复企业设备或系统的证明。

#### Scenario: Concurrent modification
- **WHEN** 两个操作使用同一旧修订号先后提交
- **THEN** 只有满足当前版本的操作可提交，另一操作重新核实且不能沿用旧批准

#### Scenario: Invented repair evidence
- **WHEN** 模型未取得员工提供的解决记录就声称已修复 VPN
- **THEN** 不接受该解决或成功声明，界面说明实际能力是记录工单处置

### Requirement: Safe legacy data boundary
已有工单、历史和回执 MUST 保留。缺少员工归属的旧记录 MUST 标记为历史数据，并仅供主管或运维历史入口查看；MCP 员工执行 MUST NOT 静默认领、改写或重新播放旧匿名操作。

#### Scenario: Legacy ticket lacks requester
- **WHEN** 普通员工尝试取得无员工归属的历史工单
- **THEN** 不因该员工当前登录而授予资源访问或写入权限

#### Scenario: Inspect historical receipt
- **WHEN** 合法历史查看入口读取旧回执
- **THEN** 保留其原始证据及旧版本身份范围，不生成虚假新员工审计

