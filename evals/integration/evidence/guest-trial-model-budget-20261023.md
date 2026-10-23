# 游客模型预算派发与结算验收

最近复验日期：2026-10-23（Asia/Shanghai）

## 验收范围

核验 OpenSpec task 3.2：游客模型 attempt 是否在派发前原子预留 task 与全站试用额度，并在响应、未发送或 unknown 后按证据结算；unknown 是否跨重启保留；费率或汇率没有可靠上界时是否拒发；Jev 是否只能通过固定且有预算上界的 profile 派发。

## 环境与结果

- 使用专用 PostgreSQL 集成测试容器 `decagent-conditional-postgres`。该容器在本次运行前已存在并运行，标签为 `decagent.purpose=conditional-tests`，仅映射到 `127.0.0.1:55433`；镜像 digest 为 `sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24`。测试创建并清理独立数据库；共享测试卷保留，未删除其他容器、卷或数据。
- 连接串从忽略文件 `.decagent/conditional-tests/database-url` 仅在当前进程中载入，没有写入报告或终端输出。
- `evals/integration/test_business_guest_trial_budget.py`：**38 passed，0 failed，0 skipped**，用时 19.58 秒。
- 定向确定性回归 `test_business_fast_path_boundaries.py` 与 `test_guest_jev_pricing.py`：**41 passed**；触及文件的 Ruff 检查通过。
- 覆盖全站费用与 Qwen token 并发限制、游客任务上限、请求幂等与北京时间跨日、预算快照、原子预留/回滚、抵扣前费用和实际付款分离、免费额度仍计 Qwen token、无可靠价格/汇率拒发、provider retry 独立预留、超时与用量 unknown 保留、restart 后 unknown 不释放、未发送证明、workspace 停用竞态，以及固定 Jev profile 的准入/拒绝边界。
- HTTP 调用使用受控 mock transport；本轮没有调用 Qwen、Jev 或 GitHub Actions，也未开启游客入口。

## Jev 与计费边界

早期验收因 OpenRouter Decisions API 没有已验证的硬性 `provider.max_price` 契约而拒绝游客 Jev。现已改为应用侧固定 profile：OpenRouter `typesafe/jev-1.14`，输入 $0.042/百万 token、输出免费、上下文 262,144 token。完整输入上下文按冻结费率为 $0.011010048；每 attempt 至少预留 $0.02，并按准确模型、路由、费率 basis 和序列化输入 token 上限在发送前校验。未知费用/用量保留预留并阻止该任务后续派发。详见[游客 Jev 固定费率边界](guest-jev-priced-bound-20261023.md)。

此上界来自固定费率和上下文限制，并由应用在本地强制校验；它不是 OpenRouter 对 Decisions API 的上游费用硬封顶承诺。实际费用仍需依据返回的 `usage.cost` 结算。该集成验收没有真实供应商调用，因此不能证明线上实际账单与冻结费率长期一致。

游客功能开关仍默认关闭；task 2.5 的工作台真实 Jev 建议和浏览器 E2E 仍未完成。此验收证明账本、准入与失败边界，不证明 Jev 的业务决策质量或费用收益。