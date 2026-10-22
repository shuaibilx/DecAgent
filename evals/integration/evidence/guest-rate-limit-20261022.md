# 游客限流与受信代理验证记录

日期：2026-10-22（Asia/Shanghai）

范围：游客创建、任务提交、人工回复的共享滑动窗口；受信代理链解析；工作台额度、队列等待与 429 提示。

供应商调用：无 Qwen、OpenRouter、Jev 或其他模型请求。

## 验证结果

- 确定性用例 `evals/deterministic/test_guest_rate_limit.py`：`3 passed`，覆盖未受信转发头、全链可信时回退到 TCP 对端、合法代理链解析、CIDR 配置校验和协议头信任边界。
- 游客限流、持久队列、会话、Gateway、Worker 相关 PostgreSQL 集成回归：`84 passed in 385.16s`。该轮使用一次性 PostgreSQL 测试容器，没有连接现有 DecAgent 数据库或服务。
- 其中新增限流/队列/会话定向组合：`13 passed in 72.58s`。包含清除 Cookie 后的会话限流、任务与回复限额、429/Retry-After、跨请求/进程持久状态及队列交互。
- 工作台浏览器实测：新游客空间成功创建，只读兼容任务进入持久队列；详情显示队列位置、`当前执行并发槽位暂满`、20 项等待上限、每空间 1 项及全站 2 项并发上限。既有排队任务在前，所以新任务显示第 2 位，符合 FIFO 状态。
- 五个新游客会话成功；同浏览器清 Cookie 后第六次请求收到 `429 guest_rate_limited`，工作台展示服务端 `Retry-After`。任务计数与全站/单游客资源额度在页面中同步更新。
- 检查通过：仓库虚拟环境中的 Ruff、JavaScript `node --check`、`openspec validate guest-sop-local-trial --strict`。本机系统 Python 3.8 不满足 `pyproject.toml` 的 Python `>=3.11` 要求；正式验证使用仓库 `.venv`。

## 边界与限制

浏览器 fixture 不加载已发布条件计划，因此打开无关记忆面板会得到预期的 HTTP 409 `contract_changed`；本次没有评估记忆功能。页面没有 Warning；两条 Console Error 均是该记忆面板的 409。未启动 Worker，队列任务没有执行，不会触发模型或业务写操作。

## 临时资源清理

浏览器会话已关闭；fixture server 已停止并删除其一次性数据库；专用 PostgreSQL 容器 `decagent-guest-rate-test-20261022` 已停止。未触碰其他 DecAgent 服务容器、数据库或用户配置。
