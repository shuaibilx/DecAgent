# 游客限流 HMAC 密钥存储审查与修复

日期：2026-10-22（Asia/Shanghai）

范围：游客限流密钥的信任边界、迁移兼容、缺少密钥时的准入行为与 PostgreSQL 回归。

模型调用：无 Qwen、OpenRouter、Jev 或其他模型请求。

## 审查发现

代码审查发现，migration `029_guest_rate_limits.sql` 曾将用于生成 IP 摘要的 HMAC key 保存在 PostgreSQL。数据库只读快照因此同时包含密钥和摘要；攻击者可离线枚举 IPv4 地址并关联匿名限流事件。这与部署密钥不应进入数据库快照的边界不符。

## 修复

- HMAC key 现在由部署环境变量 `DECAGENT_GUEST_RATE_LIMIT_HMAC_KEY` 提供，要求为 32 字节（64 位十六进制文本）；`Settings` 将它从 repr 中排除。
- 启用游客试用但没有有效 key 时，配置加载失败关闭；限流器本身也拒绝在 key 缺失时检查。
- 新增 forward-only migration `030_guest_rate_limit_secret_external.sql`，在保留 029 历史 checksum 的前提下删除旧 key 表；数据库只保存限流 IP 摘要与不可逆 HMAC key 指纹，不保存 HMAC key。
- 多个 API 实例和重启必须使用相同部署 key。系统在数据库事务中比较部署 key 指纹；若指纹变更且最近两小时内还有限流事件，则不写入新事件，并返回 503 与剩余等待时间。旧窗口过期后才会登记新指纹，防止提前轮换重置限额。

## 验证

- `evals/deterministic/test_guest_rate_limit.py`：4 passed，覆盖缺 key 时拒绝启用、CIDR 配置与可信代理边界。
- `evals/integration/test_business_guest_rate_limits.py`、`test_business_guest_sessions.py`、`test_business_guest_task_queue.py`、`test_business_guest_trial_budget.py`：首次密钥外置修复为 `60 passed in 128.60s`；补上轮换等待实现后完整重跑为 `62 passed in 137.19s`。新增用例覆盖早期换 key 返回 503/Retry-After、不写新事件、旧窗口过期后轮换及旧 key 被拒绝。迁移后 `business.guest_rate_limit_keys` 不存在，数据库状态只保存 HMAC 指纹。
- Ruff 对配置、限流器、Gateway、migration 调用方及相关测试通过。
- 测试使用专用临时 PostgreSQL 容器，未连接用户应用数据库；结束后停止并移除临时容器。

## 状态

修复已通过上述回归；3.4 限流任务保持完成。首次代码审查指出的问题由本文件保留为审计轨迹，不把旧测试结果改写为新结果。游客入口仍默认关闭；本记录不代表公网部署验收。
