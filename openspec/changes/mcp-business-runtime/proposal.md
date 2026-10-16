# Proposal

## Why

御龙后端与公司 API 无法运行，现阶段需要一个能独立验收的模拟企业业务系统。将已有持久化工单迁移为 MCP + Skill，并补齐员工权限、统一人工确认与提交恢复，能展示可被多个客户端复用的业务执行架构，而非依赖不可访问的公司接口或仅更换协议名称。

## What Changes

- **BREAKING**：MCP 成为业务执行主入口，替换业务插件的 CLI entrypoint 与逐次子进程执行；保留 DecAgent 的启动、管理和诊断 CLI。
- 新增直接调用工单业务核心的 Python MCP 服务，使用 Streamable HTTP；DecAgent 主连接固定 MCP `2026-07-28`，SDK 固定 `mcp==2.3.0`，单独验收 `2025-11-25` 兼容路径。
- **BREAKING**：新业务任务改用独立模拟员工登录，固定发起员工身份；员工、处理人、主管具有不同资源访问与操作权限，owner 保留运维及历史修复用途。
- 新增服务端统一操作核心：准备、人工确认、提交、结果查询、幂等回执与故障恢复；所有客户端执行同样的权限和批准检查。
- **BREAKING**：写工具准备可复用操作句柄，提交工具只提交已保存且当前获授权的操作；准备成功和 RPC 成功均不代表业务写入成功。
- Worker、控制面查询、批准前重评和业务评测统一走 MCP；Skill 和工具 Schema 从同一版本化业务契约派生。
- 同步使用指南、模拟身份说明、MCP 调用说明与架构、泳道、时序及恢复图。旧 CLI 与 Jev 实测保留历史范围，不计作新 MCP 验收。

## Capabilities

### New Capabilities

- `mcp-business-execution`：可信 MCP 工具契约、最新协议主连接、兼容连接、受控客户端生命周期与执行路径统一。
- `business-employee-identity`：合成员工登录、独立 MCP 凭据、固定任务身份、角色与资源授权。
- `business-operation-governance`：跨客户端共享的操作句柄、具体人工确认、受守卫约束的提交、幂等与核实恢复。
- `authenticated-service-tickets`：持久化工单生命周期、员工职责边界、身份绑定写入、历史与回执。
- `mcp-business-workbench`：员工工作台、外部客户端操作待办、统一网页批准、可信状态与同步图文指南。
- `mcp-business-acceptance`：现代及兼容协议实测、多角色端到端流程、禁止操作与恢复断言、诚实证据范围。

### Modified Capabilities

无。当前 `openspec/specs/` 没有主规格；旧行为记录在未归档的 `business-workflow-agent` change。本 change 以新增能力描述 MCP 业务版本，design 明确旧 CLI 规划的替代边界，本轮不归档或改写旧 change。

## Impact

- 规划根和实际实现根均为本项目根目录；规格保存在 `openspec/`。本轮仅创建本 change 的规划文件，后续 apply 面向 DecAgent，不改 waku 源码。
- 影响 DecAgent 的插件契约、执行器、工单服务、认证、Worker、网页、评测、安装依赖和本机启动配置；复用 PostgreSQL、LangGraph checkpoint、任务审计和既有目标核实规则。
- 新增业务身份、凭据、操作和批准存储迁移；保留已有工单及历史，旧匿名任务不能自动取得员工身份或沿用旧批准。
- 第一版限定单个模拟公司、本机服务 `127.0.0.1:8001/mcp`；御龙仅作参考。不接真实公司 API、不引入企业 SSO、多租户、stdio 双主路线或任意 API 网关。
- 不修改 Jev 问题、选项、尺度、校准和费用策略；独立 Linux 部署继续作为既有待办，不作为本次本机验收通过条件。

