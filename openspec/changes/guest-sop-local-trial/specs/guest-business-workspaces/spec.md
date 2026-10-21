# Spec Delta

## Purpose

为无需注册的访客提供可恢复的独立模拟公司，使其能够完整体验员工、处理人与主管的 SOP 交互，同时隔离公司数据、记忆、权限和操作证据，不把游客授予实例管理权限。

## ADDED Requirements

### Requirement: Anonymous isolated workspace session

系统 MUST 在游客开始体验时创建独立模拟公司和不可预测的浏览器会话。workspace MUST 由服务端认证解析，不接受客户端任意指定。会话 MUST 使用安全 Cookie、CSRF 和来源校验；本机 HTTP 例外 MUST 仅限 loopback。
游客入口 MUST 由 `DECAGENT_GUEST_TRIAL_ENABLED` 显式开启，该配置默认 MUST 为关闭；配额、限流和生命周期清理尚未完成时，入口 MUST 保持关闭。

#### Scenario: Two independent visitors
- **WHEN** 两个浏览器开始游客体验
- **THEN** 系统建立不同空间和模拟身份；其中一方不能读取另一方的空间数据或重用其操作标识

#### Scenario: Guest trial is disabled by default
- **WHEN** `DECAGENT_GUEST_TRIAL_ENABLED` 未设置或为 `false`，客户端请求开始游客体验
- **THEN** 系统拒绝游客会话创建，不创建 workspace、模拟公司数据或额度占用

### Requirement: Workspace isolation across every business surface

系统 MUST 对人员目录、分类、工单、任务、候选、审批、操作、回执、trace、记忆和工作流实例执行 workspace 归属校验。MCP 与 Worker MUST 使用可信会话/凭证绑定的空间；主管权限 MUST 仅覆盖其当前空间。

#### Scenario: Foreign object and trace identifiers
- **WHEN** 游客提交另一空间的任务、回执、审批或 trace ID
- **THEN** 服务端拒绝，不能泄漏其存在、内容或模型上下文，也不产生副作用

#### Scenario: Cross-workspace memory candidate
- **WHEN** 其他空间存在相同 subject 的已验证员工记忆
- **THEN** 当前工作流不能读取该记录，模型上下文也不得包含它

### Requirement: Workspace-bound employee memory and stable slots

业务记忆、来源撤回、提炼规则和审计 MUST 绑定认证员工当前 workspace。`MemoryContext` MUST 从已验证员工身份构造；所有候选、稳定槽解析、写入、确认和撤销 MUST 同时约束 workspace 与 employee。已发布工作流仍声明 `memory_kind + subject`，执行时只解析当前员工在当前 workspace 下与流程版本匹配的唯一有效记录；workspace ID 不属于面向用户的记忆 API。

#### Scenario: Same stable slot in separate workspaces
- **WHEN** 两个 workspace 分别为员工保存同一 `kind + subject`，但内容不同
- **THEN** 每个工作流只解析当前员工和当前 workspace 的值，不把另一空间的内容放入上下文

#### Scenario: Rule source from another workspace
- **WHEN** owner 在一个 workspace 发布经验规则，但其验证回执属于另一个 workspace
- **THEN** 来源校验拒绝发布，不产生规则或审计副作用

#### Scenario: Preserve and revoke legacy memory
- **WHEN** 旧 `company_scope` 记忆升级到 workspace-aware schema
- **THEN** 记忆保留原员工、内容、期限和状态，仍可按其稳定槽恢复；员工可撤销旧记忆，另一 workspace 无法读取或撤销

### Requirement: Task-owned workspace propagation
任务派生的事件、操作、审批、执行尝试、模型用量、计划、决策干预和工作流记录 MUST 继承可信父任务或父操作的 workspace；客户端或旧内部写入路径提供的空间值 MUST NOT 改变归属。读取任务 trace、导出或恢复人工待办前，系统 MUST 重新验证当前员工、任务和空间身份。

#### Scenario: Legacy writer creates a guest task record
- **WHEN** 旧写入路径为游客任务创建子记录并遗漏空间或给出 legacy 默认值
- **THEN** 数据库将记录绑定到父任务的游客空间；另一空间的员工不能读取、回复或恢复该记录

### Requirement: Local simulated role switching

游客 MUST 能在本人空间的员工、处理人与主管间切换。切换 MUST 重新建立当前身份权限并清理浏览器旧详情；原任务、人工待办和确认 MUST 继续绑定原执行身份。游客 MUST NOT 获得全局 owner、任意 Shell 或真实公司权限。
切换响应 MUST 返回固定角色集合中的当前 `role_key`，并轮换 CSRF token；浏览器 MUST 更新当前角色并清空身份相关的任务、操作、审批、工单详情、trace、记忆和事件流。工作台 MUST 只在游客入口显式开启时展示入口。

#### Scenario: Role switch during confirmation
- **WHEN** 游客切换到另一个模拟员工后提交此前员工的确认
- **THEN** 确认被拒绝；切回原身份后仍需按原操作、资源版本和独立确认规则处理

#### Scenario: Switch roles in the browser
- **WHEN** 游客切换角色或刷新带有效游客会话的页面
- **THEN** 工作台显示服务端确认的当前角色；切换前的身份详情从页面和事件流状态中清除，刷新后只恢复当前角色。

### Requirement: Inactivity expiry and controlled cleanup

游客空间 MUST 在 7 天无活动后过期，拒绝后续访问，支持清空当前空间。清理 MUST 撤销会话/凭证并阻止新执行，在安全边界终止任务后删除相关数据；未知操作 MUST 先核实，不能因删除空间重放写入。清除浏览器会话后不提供跨设备找回。

#### Scenario: Expiry with in-flight work
- **WHEN** 空间过期或清空时仍存在执行中或结果未知任务
- **THEN** 系统撤销新准入，保留受限恢复材料直至任务安全终结；后台清理不让任务复活或重复提交

#### Scenario: Clear while a Worker is writing a checkpoint
- **WHEN** Worker 已领取游客任务并持有任务锁，游客同时清空空间
- **THEN** 新 Worker 不得领取该空间任务，清理保持 pending；在途 Worker 释放锁后，清理删除任务和其最终 checkpoint，任务不得因租约或恢复再次执行

#### Scenario: Ambiguous operation evidence blocks deletion
- **WHEN** 游客空间中的操作收据历史不完整、与操作状态不匹配，或成功操作缺少已验证收据
- **THEN** 清理保留业务数据和恢复材料并返回 pending；不得通过重放操作来“核实”结果

#### Scenario: Verified receipt allows safe terminal cleanup
- **WHEN** Worker/操作已退出临界区，租约已结束，且操作的提交结果可由一致 receipt 验证
- **THEN** 清理可以终结并删除空间任务、操作和 checkpoint；重复运行清理器不创建新操作或改变其业务结果

#### Scenario: Background polling does not extend guest activity
- **WHEN** 游客超过 7 天没有发起有效用户请求，但浏览器持续恢复 session、读取 status 或订阅任务事件
- **THEN** 被动请求不更新 `last_activity_at`；游客 session、员工会话回退、MCP token 认证和 Worker 凭证解析均拒绝过期身份

#### Scenario: Clear revokes workspace credentials and permits safe retry
- **WHEN** 游客使用当前 CSRF token 和精确 Origin 清空自己的空间，或在清空成功但响应丢失后重试相同 DELETE
- **THEN** workspace 停用、浏览器和 MCP 凭证立即失效、模拟员工账号禁用；相同请求只返回已清空且不重复改变状态，错误 Origin 或 CSRF 不得执行清空
- **AND** 物理清理后只保留验证同一 token/CSRF/Origin 重试所需的哈希 tombstone，不恢复 workspace 访问或原始业务记录

### Requirement: Uncalibrated decisions require human selection

无合格策略时，游客 MUST 看到真实 Jev 建议、问题类型、选项/评分含义和必要证据，并由本人确认业务路径。语义选择 MUST NOT 自动批准写入；界面 MUST 区分真实模型、模拟数据和未验证质量，不能承诺 95% 正确率。

#### Scenario: Jev suggests an assignee
- **WHEN** Jev 给出处理人建议但该判断族没有有效校准策略
- **THEN** 原节点等待游客合法选择，之后仍展示具体分派写入确认；不自动转主模型判断

### Requirement: Preserve legacy business recovery

升级 MUST 保留旧单公司任务的身份、checkpoint、操作与回执，不重标任务版本。游客隔离失败时 MUST 保持游客入口关闭，不能用 UI 筛选代替服务端隔离。

#### Scenario: Migration with a pending legacy operation
- **WHEN** 旧员工有尚未确认或结果未知的操作
- **THEN** 升级后其原身份与操作键继续有效，其他游客不可访问，旧任务由兼容运行器恢复
