# SOP 业务 Readiness 与故障恢复证据（2026-10-22）

## 验收范围

本记录覆盖 OpenSpec `guest-sop-local-trial` 任务 4.2：API/MCP/业务 Worker 的业务 readiness、专用 Compose 测试空间、Worker 故障检测与恢复，以及不产生供应商费用的脚本模型核心链路。它不代表游客入口开放、私有 CD/备份回滚、Windows Runner 自启动、公网访问或独立 Linux 验收完成。

## 实现行为

- `GET /health` 仅报告 API 进程存活；`GET /readyz` 还要确认数据库迁移完整、内置业务插件已启用且摘要匹配、MCP `/health` 证明为 `service-tickets` 并处于 ready、conditional workflow 开关启用。readiness 报告只包含布尔组件状态。
- API Compose healthcheck 使用 `/readyz`。Worker healthcheck 使用独立只读探针，并检查 PID 1 确实以 `worker --kind business` 启动。
- 宿主机部署探针核验六个服务拓扑、PostgreSQL/MCP/API/Worker 容器健康、migration/bootstrap 成功、API liveness/readiness 及 Worker 类型。仅 API 可访问不能使整栈变为 ready。
- MCP 地址策略保留本机 `127.0.0.1`。容器内部只额外允许固定 `http://business-mcp:8001/mcp`；任意其他 DNS 名称、外部 IP、非规范端口/路径、凭据或 query 均拒绝。服务端接受同一个固定资源标识。

## 验证结果

| 验证 | 结果 |
|---|---|
| 定向确定性测试（MCP endpoint/internal resource、Windows 本机控制、MCP 契约、Compose preflight/readiness、smoke 诊断） | `115 passed`。覆盖固定 Compose endpoint 唯一放行、本机 loopback 兼容、其他 DNS/IP/端口/路径拒绝；API liveness 不掩盖 Worker/MCP/migration 故障；诊断丢弃未授权字段。 |
| 定向 PostgreSQL 集成（`test_api_liveness_stays_ok_when_business_mcp_is_not_ready`、`test_scripted_model_worker_smoke_uses_real_mcp_without_provider_credentials`） | `2 passed, 6 deselected`。使用专用隔离 PostgreSQL；真实 Business Worker、员工凭证、loopback MCP 与只读目录工具串接完成。模型为本地 ScriptedModel，两次 synthetic usage、费用和对外模型请求均为 0。 |
| SOP Compose 预检 | `uv run --extra dev python -m ops.deploy.sop_preflight` 通过；服务集合、端口、内部网络、Worker profile、MCP resource、readiness 健康检查及游客关闭配置符合约束。 |
| 实际 Docker Compose 故障烟测 | `uv run --extra dev python -m ops.deploy.offline_sop_smoke` 通过。最终复验的一次性项目 `decagent-sop-smoke-3cf59c036681` 启动 PostgreSQL、migrate、bootstrap、business-mcp、API、Worker；`model_requests=0`。API liveness 与全栈 readiness 最初均 ready；停止 Worker 后 liveness 仍正常、整体 readiness 变为 not_ready；重启 Worker 后整体恢复 ready。 |
| 资源清理 | smoke `finally` 执行 `compose down --volumes --remove-orphans` 并检查容器/卷/网络/镜像；另行检查未发现本次项目或前次诊断项目的容器、卷、网络与临时镜像残留。既有 `decagent-sop:offline-smoke` 镜像不属于本次临时 tag，未触碰。 |
| Ruff | 本次更改的 Python 实现和测试通过。 |

## 根因与修正

第一次真实业务 healthcheck 揭示旧证据未覆盖的运行时问题：Compose 给 Worker 注入固定服务地址 `http://business-mcp:8001/mcp`，但 MCP 客户端只接受 loopback，Worker 因 `ValueError` 重启。将客户端与服务端两侧统一为“loopback + 唯一规范 Compose 服务地址”后，Worker readiness 与完整故障恢复烟测通过。修正没有放宽到任意网络主机。

此前 `sop-compose-smoke-20261022.md` 记录 4.1 的 topology/config smoke；该 smoke 对 Worker 启动命令与容器配置的检查不能替代本次对实际 Worker 进程及 MCP 连接的 readiness 验收。本文件是 4.2 的运行时证据。

## 限制

本次没有真实 Qwen、DeepSeek 或 Jev 请求；脚本模型只验证传输/编排/工具执行路径，不提供模型质量或 Jev 性能证据。真实公开基准、备份恢复与兼容回滚、私有部署 Runner/重启恢复，以及对外访问仍是独立待办。
