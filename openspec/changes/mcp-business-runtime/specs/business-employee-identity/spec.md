# Spec Delta

## Purpose

为单个模拟公司的业务操作建立独立且可核实的员工身份。网页登录和 MCP 调用绑定同一员工，任务恢复保持原授权主体，角色、资源归属和凭据有效性由服务检查，不能由模型或单 owner 登录替代。

## ADDED Requirements

### Requirement: Independent synthetic employee login
系统 MUST 提供员工、处理人、主管的独立合成账号，唯一登录名与目录人员 ID 绑定。账号初始设置 MUST 是明确管理操作；凭据 MUST 安全保存，不写入源码、生成说明或模型上下文。owner MUST 保留运维身份而非自动成为业务员工。

#### Scenario: Employee signs in
- **WHEN** 合成员工提供有效登录凭据
- **THEN** 获得绑定该员工的业务会话，可检查其角色和业务身份

#### Scenario: Owner token used as employee token
- **WHEN** 使用 owner 运维凭据请求员工业务接口或 MCP
- **THEN** 请求被拒绝，不自动赋予员工或主管权限

### Requirement: Authenticated MCP credentials
MCP 调用 MUST 使用独立业务访问凭据，并从服务端验证结果取得员工身份。服务 MUST 检查凭据有效期、撤销状态和账号有效性；字段、工具注解及客户端自报身份 MUST NOT 替代认证。

#### Scenario: Forged employee identity
- **WHEN** 已登录员工在请求数据中声明另一员工或主管角色
- **THEN** 服务拒绝身份覆盖，实际调用身份仍由认证决定

#### Scenario: Account disabled with an existing connection
- **WHEN** 账号或访问凭据被停用而连接仍存在
- **THEN** 后续请求不能执行业务，返回明确认证或权限失败

### Requirement: Fixed task principal
业务任务 MUST 在创建时固定发起员工和凭据引用，恢复与重试 MUST 使用原员工的当前有效授权。切换界面身份、凭据过期或权限不足 MUST NOT 回退统一服务账号或静默替换任务身份。

#### Scenario: Login changed during a paused task
- **WHEN** 员工 A 的任务等待确认时界面改为员工 B
- **THEN** B 不能代 A 确认，任务不会以 B 的权限继续

#### Scenario: Original employee loses permission
- **WHEN** 恢复时原员工已无所需权限
- **THEN** 保存可见阻断原因，不执行旧操作或自动升级角色

### Requirement: Role and resource authorization
普通员工 MUST 仅访问本人提出的工单；处理人 MUST 仅访问本人提出或当前分配给自己的工单；主管 SHALL 管理全局工单。动作权限 MUST 同时满足角色、资源范围和业务状态；请求参数不得扩展该范围。

#### Scenario: Guess another ticket ID
- **WHEN** 普通员工查询不属于本人的工单、历史或操作
- **THEN** 返回不泄露资源内容的不可访问结果，过滤条件不能绕过范围限制

#### Scenario: Handler reads assigned work
- **WHEN** 处理人查询当前分配给自己的工单
- **THEN** 在合法范围内返回工单及历史；改派后重新应用访问规则

### Requirement: Employee-bound operation and audit
每个新操作、批准和业务历史 MUST 保存实际员工 ID、来源和关联操作 ID。提交 MUST 重新验证当前角色与资源权限；原始 token、密码和会话秘密 MUST 不进入审计或结果。历史匿名记录 MUST 明示无员工绑定，不自动归属给当前登录者。

#### Scenario: Permission revoked after prepare
- **WHEN** 已准备操作在提交前发生角色撤销
- **THEN** 提交被拒绝并留下安全审计，准备阶段的权限快照不构成继续授权

#### Scenario: Legacy anonymous task inspected
- **WHEN** 用户查看旧 owner 模式产生的任务
- **THEN** 显示历史身份范围，不伪造员工归属或让新登录凭据接管其旧批准

