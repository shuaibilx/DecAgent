# Spec Delta

## Purpose

为单用户自部署实例提供一致的网页和命令行入口，使用户能够提交任务、查看证据、处理审批并继续中断工作。访问控制必须覆盖任务、记忆、配置和事件流，断线后仍能恢复完整任务进度。

## ADDED Requirements

### Requirement: Authenticated owner access
任务、审批、记忆、敏感配置和事件流 MUST 仅对已认证拥有者开放；状态变更请求 MUST 防止跨站伪造。健康检查 MUST 不暴露凭据或项目内容。

#### Scenario: Unauthenticated event subscription
- **WHEN** 未认证客户端尝试订阅任务事件
- **THEN** 系统拒绝请求且不泄漏任务内容

#### Scenario: Cross-site approval submission
- **WHEN** 其他站点借用户浏览器发送审批请求
- **THEN** 系统拒绝请求且不执行待审批操作

### Requirement: Evidence-centered task workbench
网页 SHALL 支持仓库登记、任务提交、进度、差异、验证记录、判断结果、审批和交付链接；等待状态 MUST 显示需要用户做什么，不能只显示通用加载提示。

#### Scenario: Task waits for clarification
- **WHEN** 任务因为复现条件或验收条件不明确而暂停
- **THEN** 用户看到已取得的证据、具体缺失信息和回复入口

#### Scenario: User reviews a proposed action
- **WHEN** 页面展示待审批动作
- **THEN** 用户能查看参数、目标、相关版本、风险信息，并批准、拒绝或提交修改

### Requirement: Reconnectable task events
任务事件 MUST 有稳定顺序与恢复标识；客户端断线重连 SHALL 从已收到位置继续，不重复渲染副作用，不将实时连接状态误认为任务状态。

#### Scenario: Browser disconnects during execution
- **WHEN** 浏览器在任务执行中断线后重连
- **THEN** 页面补齐遗漏事件并显示持久化的当前状态，任务执行不因此重启

### Requirement: Configuration without credential disclosure
用户 SHALL 配置主模型地址、模型名称、独立 Jev 凭据和 GitHub 凭据，并运行连接诊断；读取配置和诊断结果 MUST 隐藏密钥，日志 MUST 不记录凭据。

#### Scenario: Existing configuration is viewed
- **WHEN** 用户打开配置页面
- **THEN** 页面展示模型与连接信息及密钥是否已配置，不返回密钥明文

#### Scenario: Live diagnostic could incur cost
- **WHEN** 用户选择真实模型诊断
- **THEN** 系统明确显示将产生外部请求，并只执行用户选择的受限诊断

### Requirement: Operational CLI
`decagent` CLI SHALL 提供初始化、启动与配置诊断，并允许查看、取消和恢复任务；其任务操作 MUST 使用与网页相同的授权、审批和审计规则，不提供绕过治理的执行入口。

#### Scenario: Environment is incomplete
- **WHEN** 用户运行启动或诊断命令，但 Docker、数据库或必要配置缺失
- **THEN** CLI 报告具体缺失项和修复步骤，不伪称服务已启动

#### Scenario: CLI resumes a task with stale approval
- **WHEN** 用户通过 CLI 恢复相关版本已变化的任务
- **THEN** 系统重新校验审批，而不是直接执行此前批准的旧动作
