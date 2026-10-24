# SOP-Agent OpenRouter 诊断未派发记录

记录日期：2026-10-24（北京时间）

父批次：Qwen diagnostic round 2

父候选：`907017651e72a2a5d295006f4858f0055d1b9cea`

**父报告：** [round 2 诊断报告](sop-agent-diagnostic-round2-20261024-9070176.md)

## 结果

OpenRouter 没有收到真实请求。OpenRouter 阶段只运行了镜像/离线 dry-run，选中 1 个 EnterpriseOps CSM Plan 路线槽，未创建账本，模型请求和 MCP 调用均为 0。Dry-run 通过只证明冻结的单路线计划可以装载；不证明模型可用、路线可执行或业务成功。

| 项目 | round 2 实际值 |
| --- | ---: |
| 固定开发任务 / 路线槽 | 4 / 8 |
| Qwen 请求 | 3 |
| Qwen 输入+输出 Token | 34,054 |
| Qwen MCP 调用 | 1 |
| Qwen 墙钟 | 180.34 秒 |
| Qwen 用量 unknown | 0 |
| Qwen 冻结费率估算抵扣前 API 成本 | ¥0.0433172 |
| 成功路线 | 0 |
| 终结 / pending 路线 | 3 / 5 |
| OpenRouter live 请求 / MCP 调用 | 0 / 0 |

Qwen 的 CSM Plan 与 ReAct 到达 EnterpriseOps verifier，结果都为 `success=false`。ITSM Plan 的旧失败记录发生在 Agent request cap 已耗尽之后，实际 0 模型请求、0 工具请求；不能解释为真实 ITSM 执行失败。其余路线保持 pending。费用为 provider usage 按冻结费率计算的标准化估算，不是供应商发票或现金实付证明。

## 截止时间与预算边界

Qwen round 2 的持久批次记录给出的绝对 deadline 是 **2026-10-24 09:21 UTC（17:21 北京时间）**。本次续跑检查时已超过该时刻。原批次授权最多 12 次通用模型请求、600,000 Token、45 分钟；Qwen 最多 6 次，OpenRouter 最多 6 次。Qwen 实际使用的 34,054 Token 使同一诊断窗口剩余 Token 上限为 565,946。切换 provider 不能重新开始 45 分钟，也不能创建新 ledger 来恢复已经过期的批次。

因此本记录不产生 OpenRouter 用量或费用；既有 OpenRouter `$5` 上限未消耗。若需要再次发送真实请求，必须先获得单独的新批次授权并冻结新的候选、profile、请求/Token/费用上限、开始时间与绝对 deadline。不得把新批次与本轮 Qwen 结果拼成同一 provider 的路线或配对成绩。

## 实施与验证状态

离线修正包括：OpenRouter 角色 Token 配额可以低于批次 aggregate ceiling；零额度角色在当前使用量为零时不触发批次停止；OpenRouter continuation 读取并核验 Qwen 父报告所绑定的原始 ledger 时钟，父 deadline 过期时在新 ledger、环境或模型派发前拒绝；子 ledger 的起止时刻必须与父批次一致；单路线机器执行仍不超过 1,800 秒。

覆盖这些修改的定向测试为 **99 passed、1 skipped**，相关 Ruff 检查通过。随后全量 `evals/deterministic` 为 **2,260 passed、19 skipped（306.40 秒）**；OpenSpec strict 为 **6/6**，全仓 Ruff 和 `git diff --check` 通过。`--phase openrouter-diagnostic --check-images` dry-run 通过，1 task、1 route、0 ledger、0 model calls、0 MCP calls。代码已在本地功能分支提交，但 GitHub git endpoint 连续重置/不可达，远端未更新，同 SHA CI 尚未运行。

## 结论与未完成项

本次诊断没有形成 Plan → MCP → 官方评分 → cleanup 的成功 readiness 闭环，尚无有效的 SOP-Agent 性能对比。正式公开基准仍为 0/286；历史 v10 的 0 success、3 failed、5 unknown 与本 round 2 的失败/pending 均只作开发证据，不重放、不并入性能分母。Jev 本轮未调用，不能判断 Jev 的质量、成本或延迟优势。

后续顺序：本地全量 deterministic、OpenSpec strict、全仓 Ruff 与 diff 检查已通过；GitHub 推送成功后仍需完成同 SHA CI。若继续真实模型诊断，需先通过该 CI，再另行冻结新授权和新 absolute deadline。pilot 必须使用与 readiness 完全匹配的 provider/profile。当前 pilot evidence gate 只支持 Qwen；即使 OpenRouter 诊断将来通过闭环，也不能放行现有 Qwen pilot。若后续选择 OpenRouter 作为正式实验提供方，须先实现并测试匹配的 pilot evidence gate、profile 和账本路径，再执行 24-task/68-route pilot。Qwen 和 OpenRouter 结果不能混合。
