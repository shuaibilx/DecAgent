# SOP Compose 本机容器冒烟证据（2026-10-22）

## 范围

验证独立 SOP Compose 能初始化 PostgreSQL、迁移及模拟业务数据，并启动 API、业务 Worker 和业务 MCP。使用一次性项目名、独立临时密钥文件和随机 API 端口；主模型与 Jev 密钥为空。此项是本机隔离容器验证，不代表生产部署、公网可达或模型质量验收。

## 执行与结果

| 检查 | 结果 |
|---|---|
| `uv run --extra dev python -m ops.deploy.sop_preflight` | 通过。项目名前缀 `decagent-sop`；PostgreSQL 镜像固定 digest；API 仅发布到 `127.0.0.1`；PostgreSQL、迁移、bootstrap、业务 MCP 不发布宿主机端口；repair executor 不在 SOP 拓扑内；Docker socket 未挂载。 |
| `uv run --extra dev python ops/deploy/offline_sop_smoke.py` | 通过，隔离项目 `decagent-sop-smoke-c502da244feb`。Compose 构建并启动 PostgreSQL、migrate、bootstrap、business-MCP、API、business-worker；API `/health` 返回健康，MCP `/health` ready，Worker 运行为 `business`。 |
| 运行时策略检查 | `DECAGENT_CONDITIONAL_WORKFLOWS_ENABLED=true`；API/Worker 使用相同 MCP resource `http://business-mcp:8001/mcp`；API 仅绑定 loopback；数据库/MCP/Worker 无 host port；游客入口关闭；服务无 Docker socket。 |
| 原 repair Compose 兼容检查：`docker compose --project-directory . --file compose.yaml config --quiet` | 通过，原 repair Compose 文件仍可解析。 |
| Ruff（本次变更的 Python 文件） | 通过。 |
| 定向 CLI/MCP/Compose 回归：`test_windows_local_control.py`、`test_business_mcp_contract.py`、`test_business_mcp_internal_resource.py`、`test_sop_compose_preflight.py` | 94 passed。覆盖默认 loopback CLI 行为、显式内部 MCP resource URL、配置拒绝边界（包括拒绝任何额外服务）和 MCP 契约。 |
| MCP PostgreSQL 集成：`test_business_mcp_server.py`、`test_business_mcp_client.py`、`test_business_mcp_execution.py`、`test_business_mcp_worker.py` | 27 skipped，原因均为未设置 `DECAGENT_TEST_DATABASE_URL` 指向专用测试数据库；未计为通过。Compose 冒烟自身执行了临时 PostgreSQL 和应用启动，但没有替代这些测试。 |
| `openspec validate --all --strict` | 通过，6 个 change 全部通过严格校验。 |

冒烟产生 `model_requests=0`。测试完成后已执行 `docker compose down --volumes --remove-orphans`；检查未发现该次项目的容器、卷、网络或临时镜像残留。脚本也会在结束时逐项验证这些资源已清除。没有调用 Qwen/Jev、执行游客写入或修改现有 repair 实例。

## 可复核范围

- 运行时结果覆盖容器启动、迁移、业务 bootstrap、API liveness、MCP readiness、Worker 类型、网络发布范围和 MCP resource identity。
- 本项本身不验证 Worker 的运行时 MCP readiness、完整业务工具执行、备份恢复、升级/回滚、Windows 登录自启、Runner 或外部域名。Worker/MCP readiness 和故障恢复已由[单独的 4.2 证据](sop-business-readiness-20261022.md)完成；备份回滚、Windows/Runner 与外部域名仍是后续任务。
- 环境未提供独立 PostgreSQL integration test URL，因此报告明确保留 27 项跳过状态。
