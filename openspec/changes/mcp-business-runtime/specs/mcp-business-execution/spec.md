# Spec Delta

## Purpose

为业务智能体和独立客户端提供一致、可验证的 MCP 执行接入。主业务连接使用明确的新版协议和可信工具契约，兼容客户端遵守相同授权规则，业务执行不再依赖逐次启动 CLI 子进程。

## ADDED Requirements

### Requirement: Explicit modern primary protocol
DecAgent 的主业务连接 MUST 使用 MCP `2026-07-28` 和 Streamable HTTP，并记录实际使用的协议版本。仅支持旧协议的服务 MUST 被主连接拒绝，不得静默降级后仍声称使用新版。

#### Scenario: Modern connection accepted
- **WHEN** DecAgent 连接支持 `2026-07-28` 的已配置服务
- **THEN** 连接建立且可检查实际协议版本，后续请求使用该版本

#### Scenario: Legacy-only endpoint rejected
- **WHEN** 主连接仅能使用 `2025-11-25`
- **THEN** 返回协议不兼容并停止业务派发，不回退 CLI

### Requirement: Governed legacy compatibility
业务服务 MUST 接受独立验收的 `2025-11-25` 兼容连接；兼容请求 MUST 执行与主协议相同的身份、参数、批准、幂等和结果核实规则。

#### Scenario: Older client connects
- **WHEN** 兼容客户端通过合法身份发现和调用工具
- **THEN** 返回兼容协议结果，同时保留完整业务约束和协议版本证据

#### Scenario: Legacy write without approval
- **WHEN** 旧协议客户端提交仍待人工批准的操作
- **THEN** 操作保持待确认，工单、历史和业务回执均不发生写入

### Requirement: Trusted versioned service catalog
服务地址、业务工具白名单、参数与结果 Schema、影响及确认策略 MUST 来自可信注册契约。模型或 Skill MUST NOT 添加服务地址或能力。发现目录与该员工获授权的注册工具发生版本或 Schema 漂移时 MUST 停止调用。

#### Scenario: Catalog drift
- **WHEN** 已注册工具在服务端改变参数或结果契约
- **THEN** 任务保存契约不一致原因，不采用新 Schema 继续旧动作

#### Scenario: Permission-filtered catalog
- **WHEN** 目录因员工权限过滤不可调用工具
- **THEN** 客户端仅暴露该身份的授权子集，不把正常权限过滤误判为服务版本漂移

### Requirement: Schema-derived structured capabilities
每个工具 MUST 声明可机器校验的输入和输出；调用前 MUST 校验参数，调用后 MUST 验证结构化结果。参数契约 MUST 与模型业务参数定义同源；工具注解和自然语言描述 MUST NOT 被用作授权依据。

#### Scenario: Extra authorization argument
- **WHEN** 调用携带未声明的 `approved`、`role` 或调用者身份字段
- **THEN** 参数校验拒绝请求，不授予额外权限

#### Scenario: Invalid returned data
- **WHEN** 返回值不符合声明结果 Schema
- **THEN** 不宣布成功；可能已提交的写入进入核实流程

### Requirement: Bounded credential-isolated connections
客户端 MUST 按服务和员工凭据隔离连接，并具有明确创建、复用、关闭和失效处理。请求 MUST 有时限及输入输出上限；超时或取消 MUST NOT 被视作业务撤销，凭据 MUST 不进入模型参数或日志。

#### Scenario: Two employees reuse the application
- **WHEN** 两个员工在同一应用进程中分别调用工具
- **THEN** 请求身份和凭据不会跨员工复用，停用身份不能借已有连接执行

#### Scenario: Write response interrupted
- **WHEN** 写请求派发后超时或响应断开
- **THEN** 保存结果不明及原操作 ID，并允许查询核实，不自动产生新写入

### Requirement: Unified direct business execution
Worker、业务工作台读取、批准前重评和新业务评测 MUST 通过 MCP 调用同一业务服务。服务 MUST 直接调用持久化业务核心，不得将业务工具转发给 CLI 子进程。运维命令行 SHALL 保留。

#### Scenario: Business command contains shell syntax
- **WHEN** 工单描述包含 shell 元字符
- **THEN** 内容作为业务数据保存，执行链路不产生业务 CLI 或 shell 调用

#### Scenario: Control plane reads before approval
- **WHEN** 工作台核实资源当前修订号
- **THEN** 读取经过相同 MCP 身份和资源授权，不使用隐藏的 CLI 或直连工单绕行

