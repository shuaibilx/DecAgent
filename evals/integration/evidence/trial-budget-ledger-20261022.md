# 游客预算账本验收记录

- 日期：2026-10-22（北京时间）
- OpenSpec：`guest-sop-local-trial` 任务 3.1
- 模型/API 请求：无

## 实现范围

- 新增 migration `026_guest_trial_budget.sql` 和独立的 `business.guest_trial_*` 账本表，未复用公开评测 `app.usage_records` / Jev `$1` 账本。
- `TrialBudgetLedger` 以北京时间自然日建桶；每日总费用和 Qwen token、每 workspace 的任务数和 Qwen token 有独立原子上限。
- 每个事务先锁当日全站预算行，再锁该 workspace 额度行；额度更新、任务 admission/request reservation 和账本明细写入在同一 PostgreSQL 事务内完成。每个请求有 workspace 作用域幂等键，重复入队/预留不会重复扣额度。
- 人民币预留使用已冻结 CFETS 报价，按抵扣前费用折算；有效 USD/CNY 率低于官方中间价并向下取整，美元成本向上取到 6 位小数。每个日桶不会因进程或配置改变而修改额度或 FX 快照。
- `.env.example` 增加全站/游客额度与报价来源字段。FX 报价由受信本机配置提供；运行时不抓网页。报价超 7 个日历日后拒绝新的 CNY 预留，须先更新来源日期/数值。
- 账本不依赖 `app.tasks` 外键，清空游客任务数据时不会连带删除财务预留证据；只接受当前 active guest workspace。现在提供 reserve/admission 层，尚未连接模型发送，也未实现实际账单结算、unknown 对账或券后实付字段，均属于后续任务 3.2。

## 汇率核算

本轮可核验的 [CFETS 官方 USD/CNY 中间价页面](https://www.chinamoney.com.cn/english/)显示 2026-10-20 报价为 6.7200。首版留 10% 余量：`floor(6.7200 × 0.90, 2) = 6.04 CNY/USD`。例如 ¥2 的抵扣前 API 费用折为 `$0.331126`，预留向上取整到美元微单位。券后实付不替代抵扣前费用；每笔实际账单和券后支付的持久化字段由 3.2 完成。

## 验证

- `evals/deterministic/test_guest_trial_budget.py` 与浏览器 fixture 路径回归：`12 passed`。
- `evals/integration/test_business_guest_trial_budget.py` 和 `evals/integration/test_database.py`：使用一次性 PostgreSQL 16 容器，`26 passed`。覆盖全站费用/Token 并发上限、游客 Token/任务并发上限、日配置与 FX 快照漂移、日期新鲜度、请求和任务跨日幂等、错误 workspace、未知价格拒绝、停用与预留竞争、数据库拒绝部分 FX 快照、workspace 外键索引及 migration。
- 定向 Ruff：通过。
- `openspec validate --changes --strict`：`6 passed, 0 failed`。
- `git diff --check`：通过。

临时容器只绑定 `127.0.0.1:10200`。测试结束后检查并清理测试 template/database，停止带 `--rm` 的容器；端口 `10200` 已关闭。未连接或改动现有 DecAgent 服务。

## 验收边界

这项验收证明预算策略、持久化和并发预留不会超过配置上限，不证明模型派发、实际账单、结算/退款、unknown 处理或游客业务 E2E 已完成；游客入口仍由 `DECAGENT_GUEST_TRIAL_ENABLED=false` 默认关闭。
