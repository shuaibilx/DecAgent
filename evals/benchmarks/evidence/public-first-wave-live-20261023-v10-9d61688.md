# DecAgent 公共评测首波实测报告

执行日期：2026-10-23（Asia/Shanghai）
状态：批次已完成；首波质量门槛未通过。
用途：记录固定四域首波的真实运行结果，不作为完整公开基准成绩或 Jev 收益结论。

## 固定范围

| 项目 | 固定值 |
| --- | --- |
| 候选代码 | `9d61688c52ee66435a22e18c4fa1c8348a70095a` |
| 同 SHA GitHub Actions CI | [run 39784378797](https://github.com/shuaibilx/DecAgent/actions/runs/39784378797)，9/9 jobs 成功 |
| Manifest / profile digest | `1096d0515168eab71b06cb6b9bde558a0fa2a5269ec723aef996886a342b2fed` / `4fa091d2840928bfb6a00dccfbd452a2dee8c42c465512955fd63923eb0a6721` |
| 首波分母 | 4 个固定任务，8 条 ReAct/Plan 路线 |
| 基准完整分母 | EnterpriseOps-Gym 206 + τ-Bench 80；正式基准仍为 0/286 |
| Jev / judge 请求 | 0 / 0 |

## 路线结果

| 基准域 | ReAct + Skill + MCP | 一次规划 + MCP | 官方评分 |
| --- | --- | --- | --- |
| EnterpriseOps-Gym CSM | unknown：`find_user` MCP dispatch `ValidationError` | unknown：Qwen 请求在 120 秒超时 | 未评分 |
| EnterpriseOps-Gym ITSM | failed：`ValueError`，agent execution | unknown：Qwen 请求在 120 秒超时 | 未评分 |
| τ-Bench retail | failed：`ValueError`，agent execution | unknown：Qwen 请求在 120 秒超时 | 未评分 |
| τ-Bench telecom | failed：官方 τ evaluator 判定失败 | unknown：Qwen 请求在 120 秒超时 | 0.0 / 1 条评分路线 |

合计 3 条 failed、5 条 unknown、0 条成功。8/8 环境清理均已验证。未知路线没有重放；其余路线按批次策略继续。7 条路线未获得官方分数，因此不能从这 8 条路线估算完整任务准确率，也不能据此比较两种路线的性能。

## 用量、延迟与费用

| 指标 | 实际记录 | 批次上限 |
| --- | ---: | ---: |
| Qwen 请求 | 30（Agent 21、用户模拟器 9、裁判 0） | 500 |
| 已结算 Token | 220,086（Agent 192,185；用户模拟器 27,901） | 5,000,000 总额 |
| 已结算 + unknown 预留 Token | 477,433 | 5,000,000 总额 |
| MCP 工具调用 | 13（10 个 read settled、2 个按工具元数据标为 write 的 settled 调用、1 个 read unknown） | 1,600 |
| 批次墙钟时间 | 772.246 秒（约 12 分 52 秒） | 14,400 秒 |
| Jev 请求 | 0 | 不在本批范围 |

Qwen 已结算请求的冻结 profile 费率计算值为 ¥0.1884416：Agent ¥0.1633981、用户模拟器 ¥0.0250435。此 profile 固定输入 ¥0.8/M、缓存输入 ¥0.1/M、输出 ¥2.7/M，费率来源为[千问 API 计价页](https://platform.qianwenai.com/pricing/api)。4 次 Agent Qwen 请求的用量未知，账本保留 ¥0.2681368 的费用预留和 257,347 Token 预留。它们不是实际账单金额，也没有被释放。

此次 runner 没有取得供应商逐请求账单凭证；因此上述已结算费用是按冻结费率从返回 Token 计算的抵扣前 API 费用估算，unknown 请求费用仍未知。账单数据缺失不得解释成 ¥0。用户选择的批次成本保护上界约 ¥13.50，与 500 万 Token 上限及最高 ¥2.7/M 输出费率相符；本次运行没有触及请求、Token、工具调用或墙钟停止线。

机器报告复核时发现，成本分组曾把缺失的 provider invoice 记作 `provider_billed: "0"`。已依据同一份每请求 cost records 更正为 `null`，并加入已知部分金额和缺失 attempt 数；没有新增账单证据，也未改变路线、Token、冻结费率计算值或 unknown 费用预留。

4 次 Qwen unknown 的本地 dispatch 时间和输入上界如下（北京时间）。账本没有对应的 HTTP 状态或服务端 request ID，服务端是否收到、完成及计费仍待账号记录核对：

| 域 | dispatch 时间 | 保守输入上界 |
| --- | --- | ---: |
| EnterpriseOps CSM | 2026-10-23 14:12:41 | 51,653 |
| EnterpriseOps ITSM | 2026-10-23 14:15:12 | 60,124 |
| τ-Bench retail | 2026-10-23 14:18:36 | 49,890 |
| τ-Bench telecom | 2026-10-23 14:23:18 | 62,912 |

此上界由 `PublicPlannerCallback` 按序列化请求的 UTF-8 字节数和 schema 开销计算，是安全预留单位，不是服务端报告的 Token 数。它不能证明实际 prompt token 数接近模型上限，也不能证明请求体大小导致了 timeout。四个请求的服务端 usage 均未知。

## 工具效果与副作用

13 次工具调用中，工具元数据将两次已结算调用标记为 write：ITSM/ReAct 的 `find_configuration_items` 和 τ telecom/ReAct 的 `refuel_data`。它们运行在隔离的基准环境中，8/8 环境清理均已验证。另有一次 CSM `find_user` read 调用结果 unknown。官方评分记录没有测得禁用副作用数量（`forbidden_side_effects` 为 null），所以本报告不声称副作用为零；清理通过只证明环境 teardown 完成。

## 故障定位现状

- Qwen 四次 Plan 请求在本机 120 秒 deadline 超时；账本没有 HTTP 状态、服务端 request ID 或 usage。输入上界为 49,890–62,912，但该值按 UTF-8 字节加 schema overhead 计算，不是实际 Token 数。现有证据只能确认本机停止等待，不能确认上游原因、实际输入或账单。
- CSM 的 `find_user` read 调用在 MCP gateway 抛出 `ValidationError`，耗时约 0.055 秒，没有 HTTP 状态或结果快照。attempt 已经 dispatch，因此按 unknown 保留，不能重放。
- ITSM/ReAct 的 `ValueError` 前记录了 `list_users` 和 `find_configuration_items` 两次已结算工具调用；τ retail/ReAct 的 `ValueError` 前记录了四次已结算 read。隔离日志没有保存安全的异常来源行或异常消息，因此当前只能定位到 `agent_execution`，不能确认抛错语句或把失败归因于这些工具调用。

离线检查 CSM 调用链发现：ReAct 与 `_ObservedGateway` 在进入 `LedgeredToolGateway` 前都验证了工具参数；`EvaluationPlanSession.call` 也会在 `environment.call_tool` 前再次验证。原始 MCP 响应返回后，`EvaluationCatalog.observe` 会按冻结的 output schema 校验结果；该路径可产生 `jsonschema.ValidationError`，而账本异常摘要只保存最深异常类名、不保存来源行或 schema 路径。因此**最可能**是 `find_user` 的 MCP 响应未通过 output schema 校验；这是基于代码顺序的诊断假设，不是持久化运行时 traceback 的证明。没有原始响应，无法确定不匹配字段；attempt 继续保持 unknown，不重放。

## 结论与后续门槛

真实执行与账本闭环已被验证到“模型、工具、τ 用户模拟器、一个官方评分结果和环境清理实际运行”的程度；但四个域均未形成有效成功闭环，Plan 路线全部遇到 Qwen 请求超时，ReAct 路线也未成功。因此 9.6.F、9.6.G、10.3–10.6 仍未完成，首波验收失败；剩余 smoke 与全量 206+80 暂停。本批没有执行 Jev，不能得出 Jev 的成本、延迟或质量优势结论。

下一步先对照隔离日志调查 120 秒 Qwen timeout、CSM MCP 的 `ValidationError`、ReAct `ValueError` 与 τ telecom 评分失败；不得重放本批 unknown。修复后需冻结新候选和新 cohort、重新通过同 SHA CI，并另行确定后续批次额度与授权。

## 证据

- 机器报告：[public-first-wave-live-20261023-v10-9d61688.json](public-first-wave-live-20261023-v10-9d61688.json)
- 冻结清单：[comparison_trial_manifest_v10.json](../manifests/comparison_trial_manifest_v10.json)
- 冻结模型 profile：[evaluation_model_profiles_v5.json](../manifests/evaluation_model_profiles_v5.json)
- 本地候选专属账本：`.decagent/business-eval/public-trials/cohorts/c88236d13670c54546ddddbb796e1aac5a32b611975da1c1da90b3a8369cedb4-9d61688c52ee66435a22e18c4fa1c8348a70095a.sqlite3`
- 已运行命令：`D:/Anaconda3/envs/Agent311/python.exe evals/benchmarks/run_public_trials.py --phase first-wave --mode smoke --execute --report evals/benchmarks/evidence/public-first-wave-live-20261023-v10-9d61688.json`

同一命令若再次运行，只能恢复此 cohort/candidate 的规范账本；8 条终态路线（包括 unknown）不会重放。报告中的 API key 未导出或记录。
