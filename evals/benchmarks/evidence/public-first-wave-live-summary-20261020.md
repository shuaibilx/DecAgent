# 首波公开评测实测报告（2026-10-20）

## 执行边界

- 候选代码：`9bc3ee6654e46630c9ae8238a28eb6e34a088698`。
- 同一候选 SHA 的 GitHub Actions CI：`39442012550`，全部 job 通过。
- 固定 cohort：CSM、ITSM、τ retail、τ telecom 各 1 个任务；每个任务执行 ReAct 与 Plan，共 8 条路线。
- 原始机器报告：[`public-first-wave-live-20261020.json`](public-first-wave-live-20261020.json)。规范账本：`.decagent/business-eval/public-trials/public-smoke-v7.sqlite3`。
- 此批次只运行一次。所有路线已进入终态；unknown 与失败路线没有自动重放。

## 路线结果

| 任务域 | ReAct + Skill + MCP | 一次规划 + PlanRuntime + MCP | 清理 | 官方评分 |
| --- | --- | --- | --- | --- |
| CSM | unknown：`tool_dispatch_error` | failed：`agent_failure` | 两条均 verified | 均未启动 |
| ITSM | unknown：`model_dispatch_error` | failed：`agent_failure` | 两条均 verified | 均未启动 |
| τ retail | unknown：`model_dispatch_error` | unknown：`model_dispatch_error` | 两条均 verified | 均未启动 |
| τ telecom | failed：`environment_failure` | failed：`environment_failure` | 两条均 verified | 均未启动 |

合计 8 条路线：0 条完成，4 条 failed，4 条 unknown；8/8 清理被标记为 verified，8/8 的评分状态为 `not_attempted`。因此，本轮验证了 live 派发、失败状态保存与清理路径，但没有得到任何官方任务分数。这里的 0/8 是路线闭环完成数，不是 EnterpriseOps 或 τ-Bench 的官方成功率。

CSM ReAct 的唯一工具 attempt 是 `find_user`，账本记录为 unknown，并且当时的工具目录将其 effect 标为 `write`。没有结果摘要，因此不能确认调用是否产生业务副作用；清理 verified 只证明隔离环境完成清理，不证明该调用无副作用。

## 模型与费用证据

- 共登记 4 次 Qwen 请求：2 次 Agent primary、2 次 τ user simulator；无 Jev 请求、无 judge 请求。
- 1 次 Qwen 请求成功返回用量：`qwen3.8-flash`，输入 6,821、输出 131，共 6,952 tokens；服务端 request ID 已记入账本。
- 另外 3 次请求状态为 unknown，没有可核实的 token 用量、服务端 request ID 或费用记录。报告中的 116,984 是账本为此批次保留的 Token 预算，不是实际消耗量。
- 当前没有供应商账单记录，已知用量请求的实际费用也为 unknown。代金券仍然属于供应商计费的一部分；不能把 unknown 写成 `$0`，也不能据此比较模型成本。

未知请求保留原账本状态及预留，不重试。工具或模型结果为 unknown 后，对应路线已停止后续调用。

## 结论与后续门槛

本次结果不证明模型任务质量、ReAct 与 Plan 的相对性能、Jev 收益、真实费用或工具副作用。ITSM 与 τ retail 的 model dispatch 仅保留粗粒度错误类别，τ telecom 保留环境失败类别；当前账本没有异常阶段和异常类型证据，无法离线还原根因。为避免猜测和重放，本报告不把这些类别进一步解释成某个具体网络或配置问题。

首波工程验收未通过，10.3、10.4、9.6.F/G 和 10.10.D/E 仍待完成；OpenSpec checkbox 数不变。本批次已结束，不继续派发剩余 40 条 smoke 路线，也不以同一规范账本重跑终态路线。下一批真实请求须在离线修复可验证的问题、增加不含请求正文或密钥的异常阶段诊断、完成必要回归并重新冻结试次与资源额度后单独安排。

执行结束后，CLI 在打印内存报告时遇到 `Decimal` JSON 序列化错误；机器报告已先写入且可读。现已修复终端输出序列化并补充 Decimal 回归测试；此修复没有改写本次不可变报告或规范账本，也没有重新发送模型/工具请求。
