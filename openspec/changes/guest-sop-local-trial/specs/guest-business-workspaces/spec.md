# Spec Delta

## Purpose

为无需注册的访客提供可恢复的独立模拟公司，使其能够完整体验员工、处理人与主管的 SOP 交互，同时隔离公司数据、记忆、权限和操作证据，不把游客授予实例管理权限。

## ADDED Requirements

### Requirement: Anonymous isolated workspace session

系统 MUST 在游客开始体验时创建独立模拟公司和不可预测的浏览器会话。workspace MUST 由服务端认证解析，不接受客户端任意指定。会话 MUST 使用安全 Cookie、CSRF 和来源校验；本机 HTTP 例外 MUST 仅限 loopback。

#### Scenario: Two independent visitors
- **WHEN** 两个浏览器开始游客体验
- **THEN** 系统建立不同空间和模拟身份；其中一方不能读取另一方的空间数据或重用其操作标识

### Requirement: Workspace isolation across every business surface

系统 MUST 对人员目录、分类、工单、任务、候选、审批、操作、回执、trace、记忆和工作流实例执行 workspace 归属校验。MCP 与 Worker MUST 使用可信会话/凭证绑定的空间；主管权限 MUST 仅覆盖其当前空间。

#### Scenario: Foreign object and trace identifiers
- **WHEN** 游客提交另一空间的任务、回执、审批或 trace ID
- **THEN** 服务端拒绝，不能泄漏其存在、内容或模型上下文，也不产生副作用

#### Scenario: Cross-workspace memory candidate
- **WHEN** 其他空间存在相同 subject 的已验证员工记忆
- **THEN** 当前工作流不能读取该记录，模型上下文也不得包含它

### Requirement: Local simulated role switching

游客 MUST 能在本人空间的员工、处理人与主管间切换。切换 MUST 重新建立当前身份权限并清理浏览器旧详情；原任务、人工待办和确认 MUST 继续绑定原执行身份。游客 MUST NOT 获得全局 owner、任意 Shell 或真实公司权限。

#### Scenario: Role switch during confirmation
- **WHEN** 游客切换到另一个模拟员工后提交此前员工的确认
- **THEN** 确认被拒绝；切回原身份后仍需按原操作、资源版本和独立确认规则处理

### Requirement: Inactivity expiry and controlled cleanup

游客空间 MUST 在 7 天无活动后过期，拒绝后续访问，支持清空当前空间。清理 MUST 撤销会话/凭证并阻止新执行，在安全边界终止任务后删除相关数据；未知操作 MUST 先核实，不能因删除空间重放写入。清除浏览器会话后不提供跨设备找回。

#### Scenario: Expiry with in-flight work
- **WHEN** 空间过期或清空时仍存在执行中或结果未知任务
- **THEN** 系统撤销新准入，保留受限恢复材料直至任务安全终结；后台清理不让任务复活或重复提交

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
