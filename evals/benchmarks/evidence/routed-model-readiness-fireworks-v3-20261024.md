# Fireworks 路由模型 readiness v3 结果

**结果：通过模型就绪门禁。** 冻结的 v7 cohort 需要 OpenRouter Fireworks 路由；本批次按该 profile 完成了连接、无副作用工具契约、只读计划编译和流式 usage 四项检查。该结果只证明指定模型 profile 可以用于后续评测准备，不代表业务工作流成功，也不代表 Jev 有质量或性能收益。

## 批次身份与限制

- 授权：`primary-readiness-fireworks-v3-20261024`；独立账本，不恢复或重放历史 readiness/unknown。
- 代码：工作树 `0022bfe026bdee607f93b810546be902cfec9835`；功能代码与通过 CI 的 `69c6c6f0fb7c86ee3a1c31b54d049e1a856c37bb` 相同。该功能提交的 GitHub Actions [run 40106888705](https://github.com/shuaibilx/DecAgent/actions/runs/40106888705) 为 11/11 jobs 成功；`0022bfe` 到该提交之间只有 OpenSpec 文档变化。
- 冻结 profile：`openrouter_fireworks`，模型 `deepseek/deepseek-v4.1-flash`，OpenRouter endpoint `https://openrouter.ai/api/v1`。
- 记录的启动参数（仅作审计，**不要重跑**）：`D:\Anaconda3\envs\Agent311\python.exe -m decagent model-readiness --live --ledger .decagent/routed-model-readiness-fireworks-v3-20261024.sqlite --cohort-profiles evals/benchmarks/manifests/evaluation_model_profiles_v7.json --evidence evals/benchmarks/evidence/routed-model-readiness-fireworks-v3-20261024.json`。四次授权额度已用完；旧 ledger/evidence 不可用来启动新试次。
- 硬上限：4 次请求、300,000 tokens、45 分钟、抵扣前 $0.30。实际 4 次请求、16,282 aggregate input+output tokens、16.426 秒；没有 unknown、重试、超额或阻断。
- 成本：账本按冻结费率（输入 $0.60/百万、输出 $2.40/百万）计算的抵扣前估算为 **$0.0117276**，低于 $0.30 上限；OpenRouter 未提供可用的账单实际值，因此不能当作发票/实扣金额。Qwen、Jev、业务 MCP 和官方评分器调用均为 **0**。
- 限制：机器证据记录总 tokens 和费率估算，但不保留 prompt/completion token 拆分；报告不推断该拆分。上游供应商身份没有独立账单回执字段，因此证据确认的是发往 OpenRouter 的 Fireworks 固定路由 profile 与返回模型标识，不宣称外部账单已验证实际承载节点。

## 四阶段结果

| 阶段 | 结果 | 用量 tokens | 延迟 |
|---|---:|---:|---:|
| 连接与完整响应/usage | 通过 | 1,184 | 3,733 ms |
| 工具参数契约（工具未执行） | 通过 | 378 | 3,358 ms |
| 生产计划 compiler 的只读计划 | 通过，2 个节点，无格式修正 | 13,531 | 5,439 ms |
| 流式响应与 usage | 通过 | 1,189 | 3,363 ms |
| **合计** | **4/4 通过** | **16,282** | **批次墙钟 16.426 秒** |

账本 attempt 全部为 `settled/responded`、`overrun=0`，没有错误分类；流式 attempt 没有服务端 generation ID，其余可用的 server IDs 保存在机器证据中。机器证据通过本地 `validate_live_public_readiness` 校验，并与冻结的 `evaluation_model_profiles_v7.json` provider、model、route 和 profile fingerprints 匹配。

机器可读证据：[routed-model-readiness-fireworks-v3-20261024.json](routed-model-readiness-fireworks-v3-20261024.json)。

## 后续门槛

此 readiness 不派发业务 MCP，也不调用官方评分器。正确的单例开发预检选择 v12/v7 冻结配置中的 CSM `plan_mcp`，但仍是 dry-run。该单例上限为 5 次 OpenRouter Agent 请求、280,000 tokens、抵扣前 $0.30、40 次工具调用和 1,800 秒；用户模拟器、LLM judge 和 Jev 禁用。执行它还需要最新候选的同 SHA CI 和单独授权。任务仍不进入 24-task pilot 分数；闭环通过后才继续冻结和运行正式小规模对照。
