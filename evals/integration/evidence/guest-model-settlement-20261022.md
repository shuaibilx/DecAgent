# 游客模型预留与结算验证记录

日期：2026-10-22

范围：游客业务模型的 task/trial 共用账本、provider attempt、币种计价、settlement、unknown 与恢复约束。

环境：Windows 本机项目 `.venv`、一次性隔离 PostgreSQL 测试容器；所有模型响应均为本地 mock，没有访问 Qwen、OpenRouter 或 Jev 服务，也没有产生 API 费用。

## 实现

- 新增 migration `027_guest_trial_model_settlement.sql`，为每个 provider attempt 保存序号、关联的 `app.usage_records`、输入/输出 token、抵扣前本币费用、美元等值、价格依据、server request ID、延迟、错误类别、凭证支持的代金券/现金金额及超出预留标记。Migration `026` 保持不变。
- `UsageLedger` 与游客 trial ledger 在同一个 PostgreSQL transaction 中预留、标记派发、settle 或标记 not-sent。任务预算拒绝会同时回滚 trial 日桶及 attempt 行。每个 attempt 有独立主键与额度预留。
- `TokenPrice` 带本币代码和价格版本。Qwen/DashScope 默认 CNY，OpenRouter fallback 默认 USD；用户可显式覆盖。Qwen 按实际 token 和版本费率结算 `frozen_rate`；Jev/OpenRouter 的确定 `usage.cost` 作为 USD `provider_billed` 结算。
- `BusinessServices`、`ConditionalBusinessServices` 和 memory-write evaluator 使用同一 task ledger factory。只有 guest workspace 接入 trial 账本；试用开关关闭或 workspace 停用时，派发失败关闭。没有可靠价格版本或 CNY 所需的有效 FX 快照时，派发被拒绝。
- unknown 保留原预算日的费用与 Qwen token 预留，并封锁该游客 task 后续模型派发（包含自动重试及 fallback）。进程重启或跨日不会释放原占用。仅可信本地代码能证明请求未发出时，才能原子释放 task/trial 两侧额度。Voucher 与 cash 没有供应商或 invoice 证据时保留空值，不推测为零。

## 汇率核对

CFETS 官方页面发布的 2026-10-20 USD/CNY 中间价为 `6.7200`。按既定 10% 安全余量和向下截断规则，预算换算使用 `6.04 CNY/USD`，所以 `$5` 的单一人民币等值预算上界为 `¥30.20`。官方数据：[2026-10-20 中国外汇交易中心人民币汇率中间价公告](https://www.chinamoney.org.cn/chinese/ccprnoticecontent/index.html?searchDate=2026-10-20)。运行时继续要求 source date 在 7 天内，并把每个北京时间预算日首次写入的 FX snapshot 固定下来；本次验证没有修改实际 `.env` 或数据库预算日快照。

## 验证结果

通过 Ruff 检查。确定性测试 `test_primary_route_retries.py`、`test_runtime_config.py`、`test_model_routing_config.py`、`test_business_conditional_config.py` 合计 `43 passed`。隔离 PostgreSQL 测试 `test_business_guest_trial_budget.py`、`test_business_services.py`、`test_business_conditional_services.py`、`test_business_memory_write_evaluator.py`、`test_model_budget.py` 合计 `63 passed in 204.79s`。

关键覆盖包括：任务/全站/游客预留原子性；CNY 和 USD 本币计价；抵扣前费用与 voucher/cash 证据分离；模型响应的实际 token/费用结算；无费率拒绝派发；零费用 Qwen 仍计 token；Qwen timeout 只派发一次并保持 unknown；unknown 跨日与重新创建 ledger 后仍占额度；not-sent 只能依据本地 dispatch 证据释放；关闭游客功能时模型 dispatch 被阻止；Jev mock 的 USD 成本和 token 进入 task/trial 两侧账本。

该验证证明本机模型记账边界与账本实现，不等于真实 Qwen/Jev 供应商调用验收，也不证明游客入口、队列、限流或公网部署已可用。游客入口仍默认关闭。

## 补充验证：DashScope provider alias 的 Qwen 预留与结算（2026-10-22）

代码审查发现 `UsageLedger` 和运行时配置已将 `dashscope` 作为 Qwen provider alias，并按 CNY 计价、预留 Qwen Token；但 `TrialBudgetLedger.reserve_request()` 的准入校验仍只认 `qwen`，导致该配置在模型派发前被拒绝。新增同一 task/trial 原子路径的参数化集成覆盖，分别走 `qwen` 和 `dashscope` 两个 provider 标识。修复前，`dashscope` 案例按预期失败于派发前策略校验；允许别名后，隔离 PostgreSQL 的完整 `test_business_guest_trial_budget.py` 为 `36 passed in 18.22s`，并验证 reservation 与已知 CNY usage settlement 均计入 Qwen token 桶。测试使用本地固定用量与费率，没有访问 Qwen、OpenRouter 或 Jev，也没有产生 API 费用。游客 Jev 费用上界仍未验证，因此 OpenSpec 3.2 保持未完成。

## 补充验证：provider 错误分类结算（2026-10-22）

`BudgetedPrimary` 发出的 provider 类别（`connection`、`permanent_http`、`transient_http`）和通用未分类异常（`transport_error`）过去没有与 trial 表的有限枚举对齐，导致派发后的 unknown 结算事务回滚。现在应用总账仍记录原始类别与可用的 HTTP 状态，trial 子账映射为受限分类：连接错误为 `network_error`、HTTP 408 为 `timeout`、429 为 `rate_limited`、其他已分类 HTTP 错误为 `provider_error`、未分类传输异常为 `unknown_error`。任务级 usage 和 trial attempt 在一个事务中结算为 unknown，预算预留继续占用。

回归先复现五类失败：结算因 `Invalid provider error category` 回滚；修复后隔离 PostgreSQL 参数测试 `6 passed`，覆盖 connection、401、408、429、503 和未分类传输错误，并核对两侧记录均为 unknown、预留仍在、应用总账保留原始错误类别/状态。完整 `test_business_guest_trial_budget.py` 为 `34 passed in 17.22s`；`test_primary_route_retries.py` 为 `11 passed`；相关 Ruff 检查通过。所有模型均为本地 mock，无外部模型请求或费用。测试使用一次性本地 PostgreSQL 容器，未访问业务容器或生产数据库。
