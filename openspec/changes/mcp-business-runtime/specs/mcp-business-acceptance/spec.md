# Spec Delta

## Purpose

通过真实本机 MCP 请求、持久化合成业务与可复现故障验证改造结果。验收区分现代协议、兼容客户端、业务授权和模型表现，保留失败证据，不沿用旧 CLI 成功数或凭理论推断宣布加速与企业落地。

## ADDED Requirements

### Requirement: Modern wire-level acceptance
主路径验收 MUST 使用真实 HTTP MCP 连接并证明实际协议为 `2026-07-28`，覆盖目录、输入与输出 Schema、错误、身份、准备、确认、提交和结果查询。直接函数或内存调用 MUST NOT 代替网络协议验收。

#### Scenario: Server passes only in-memory tests
- **WHEN** 只有服务函数或内存客户端测试通过
- **THEN** 主协议端到端验收仍为未完成，不报告真实连接通过

### Requirement: Independent legacy client acceptance
验收 MUST 使用独立 mcp-cli 客户端通过 `2025-11-25` 路径发现、检查和调用同一业务服务，并验证相同权限及确认语义。凭据 MUST 环境引用，配置 MUST 隔离，业务自动重试 MUST 关闭；业务成功须由服务端证据核实。

#### Scenario: Client prints success-like text
- **WHEN** mcp-cli 输出文本但尚无已核实业务回执
- **THEN** 不以输出或退出码独自判定写入成功，另核实操作与资源

### Requirement: Reproducible multi-role business acceptance
固定验收集 MUST 覆盖员工、处理人、主管的完整生命周期、同名消歧、请求者绑定、不可访问资源和越权动作。结果 MUST 由真实数据库状态和历史核实，未经授权业务副作用 MUST 为零。

#### Scenario: Employee asks for supervisor action
- **WHEN** 任务要求普通员工分配工单或调整已有优先级
- **THEN** 系统拒绝权限不足，验收确认无资源、历史或业务回执变更

#### Scenario: Full multi-role sequence
- **WHEN** 不同合法员工依次完成创建、分配、开始、解决和关闭
- **THEN** 所有目标步骤有对应操作者和回执，不能用一个万能身份冒充多角色验证

### Requirement: Fault and restart acceptance
验收 MUST 覆盖重复准备与提交、并发冲突、响应丢失、提交前后崩溃、待确认重启、取消、租约接管和批准失效。每个故障 MUST 检查真实副作用计数、原操作关联和最终状态，不只断言异常类型。

#### Scenario: Response lost after commit
- **WHEN** 注入提交成功后响应丢失
- **THEN** 恢复取得原回执，数据库只含一次变更，不生成第二个工单

#### Scenario: Old worker resumes
- **WHEN** 新租约接管后旧 Worker 再提交
- **THEN** 旧请求不能写入，已有提交仍可按权限核实

### Requirement: Honest scoped measurements
新报告 MUST 记录版本、协议、客户端、身份、数据集、成功及失败分母、禁止副作用、恢复结果和工具机器耗时，人工等待另计。旧 CLI 与 Jev 报告 MUST 保留历史范围；未完成真实模型或对照压测时不得声称模型收益或 MCP 必然加速。

#### Scenario: New MCP timings collected
- **WHEN** 仅测得本机 MCP 的调用延迟
- **THEN** 报告该环境的实际指标，不声称相对 CLI 的性能倍数

#### Scenario: Scripted planner used
- **WHEN** 端到端案例使用可复现脚本模型
- **THEN** 明示模型替身，仍通过真实业务服务核实，不将其当作真实模型准确率

### Requirement: Installation and deployment evidence
安装包验收 MUST 在 checkout 外核实服务启动、Skill 读取和业务调用；当前 Windows 本机为本 change 的部署验收范围。容器兼容测试 SHALL 与独立 Linux 部署分开记录，既有 Linux 待办 MUST 保留。

#### Scenario: Local container passes
- **WHEN** 本机容器中的兼容检查通过
- **THEN** 仅报告该环境通过，不替代不存在的独立 Linux 主机验收

#### Scenario: Wheel misses runtime resources
- **WHEN** 安装包无法读取 Skill、契约或启动业务服务
- **THEN** 安装验收失败，源码目录可运行不能覆盖该失败

