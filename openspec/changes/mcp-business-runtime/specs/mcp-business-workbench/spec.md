# Spec Delta

## Purpose

让员工在统一业务工作台发起自然语言任务，并查看来自 DecAgent 或其他 MCP 客户端的真实操作状态。界面提供明确的人工确认和恢复入口，文档解释模拟身份、协议版本与证据范围，避免将准备动作或模拟流程包装为生产成果。

## ADDED Requirements

### Requirement: Employee-first business workspace
默认业务工作台 MUST 提供员工登录、当前角色与范围、自然语言任务创建和工单查询。运维 owner 与业务员工 MUST 明确区分；业务页读取与任务操作 MUST 按当前员工授权过滤，历史修复入口保留原行为。

#### Scenario: Employee opens the workspace
- **WHEN** 员工登录并打开业务首页
- **THEN** 可看到本人权限、可用业务能力和可访问任务，不需填写 GitHub 仓库

#### Scenario: Switch account
- **WHEN** 从员工 A 切换到 B
- **THEN** 清理旧会话视图，B 不能看到 A 的受限待办或恢复 A 的任务

### Requirement: Shared cross-client operation inbox
工作台 MUST 展示员工通过 DecAgent、兼容 MCP 或其他合法客户端准备的操作，包含来源、操作 ID、命令、参数、目标资源和状态。外部工具操作 MUST 不被伪装成已经运行主模型的 Agent 任务。

#### Scenario: External client prepares a sensitive action
- **WHEN** 员工从外部 MCP 客户端准备关闭工单
- **THEN** 同一员工的网页出现对应待办，显示待确认而非已关闭

### Requirement: Concrete approval and safe editing
网页 MUST 展示具体变更、员工、资源修订号与确认有效期，并允许批准或拒绝。编辑参数 MUST 创建新操作并留下关联，不能沿用旧确认；浏览器业务修改 MUST 通过员工会话及 Origin/CSRF 检查。

#### Scenario: Approve then resume
- **WHEN** 原员工批准当前有效的具体操作
- **THEN** 操作可按原句柄提交；关联 Agent 任务继续，外部客户端按同一句柄提交或查询

#### Scenario: Forged browser approval
- **WHEN** 未认证或不合法来源发送确认请求
- **THEN** 确认被拒绝，操作和工单状态不改变

### Requirement: Honest resumable state display
界面 MUST 区分待确认、已就绪、执行中、结果不明、成功、失败、拒绝和失效。成功展示 MUST 来自服务端核实结果；等待、重启或事件重连 MUST 保留操作关联，不重复执行。模型决策展示保留现有真实记录。

#### Scenario: RPC succeeds before business commit
- **WHEN** 准备工具成功返回操作句柄
- **THEN** 页面不显示工单创建成功，仅显示真实准备状态

#### Scenario: Restart during confirmation
- **WHEN** 服务在待确认时重启
- **THEN** 原员工仍能查看未处理待办，原客户端取得同一操作状态

### Requirement: Current visual operating documentation
README、使用指南、身份权限说明、MCP 调用指南及架构、流程、泳道、时序和恢复图 MUST 同步新行为。生成说明 MUST 放入独立日期目录；指南 MUST 包含本机初始化、账号设置、启动、确认、恢复及安全凭据引用，不将未执行验收写为通过。

#### Scenario: Follow the MCP guide
- **WHEN** 用户按指南配置环境变量凭据并通过 mcp-cli 发现、检查和调用工具
- **THEN** 命令指向实际服务及工具，示例不内嵌密码或 token，并解释主协议与兼容协议

#### Scenario: Read the project description
- **WHEN** 用户阅读项目说明或简历项目素材
- **THEN** 可以区分合成业务、真实公司 API、协议兼容与独立部署的实际验收范围

