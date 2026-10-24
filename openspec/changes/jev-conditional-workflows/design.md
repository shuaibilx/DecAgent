# Design

### 2026-10-24 Per-cohort lower batch ceilings

The first-wave limits remain hard maximum ceilings, while a new live-capable cohort may freeze lower positive integer limits for total Qwen requests/tokens, Agent/simulator/judge allocations, tool calls, and wall time. Role allocations must exactly sum to their aggregate request and token caps. The manifest builder validates and digests these values; the run-plan loader rejects malformed allocations and any value above the approved maxima. Default manifest generation remains byte-for-byte equivalent to the frozen v10 manifest, and custom caps require manifest v9 or newer because historical v7/v8 cohorts cannot be dispatched. This allows a separately approved smaller diagnostic cohort without reusing v10's ledger, changing historical limits, or raising any cap. It does not authorize model dispatch or create the proposed cohort.

## 2026-10-24 first-wave status snapshot (before the 91d1 diagnostic)

2026-10-24 新 cohort 在候选 `5b43a45962ee16bc0f61015d1063ee9ad6c676fe`（同 SHA CI run `40043584670` 通过）上完成 4 任务/8 条 ReAct/Plan 首波路线：3 failed、5 unknown、0 success；清理 8/8 verified。ITSM/ReAct 与 τ telecom/ReAct 到达官方评分器，均为 0.0；CSM 与 τ retail 没有官方分数。共 33 次 Qwen 请求，已结算 233,237 Token，unknown 保留 257,648 Token 预留；工具调用 13 次。冻结费率估算已结算抵扣前费用 ¥0.2045959，unknown 费用预留 ¥0.2683776；没有逐请求供应商账单凭证。Jev、judge 和跨模型降级均关闭。4/23 primary 请求记为 unknown，均无 provider request ID，发送状态不可核实；不据此认定供应商实际计费，也不重放。该 cohort 验证了部分真实调用/评分/清理路径，但没有通过四域质量门槛；公开 smoke 其余任务和 206+80 全量暂停，9.6.F/G、10.3–10.6 仍未完成。按实耗线性外推的剩余 smoke/full 资源量级已记录，但由于路线全部 failed/unknown，不用于预测或新批次授权，见[资源估算](../../../evals/benchmarks/evidence/public-evaluation-resource-estimate-20261024-5b43a45.md)。详见[首波报告](../../../evals/benchmarks/evidence/public-first-wave-live-20261024-5b43a45-100qwen.md)及[机器报告](../../../evals/benchmarks/evidence/public-first-wave-live-20261024-5b43a45-100qwen.json)。

四条 unknown Plan request 的本地输入预留上界为 50,794–61,959 Token，均预留 8,192 输出 Token；该数字不是 provider usage。19 次 settled primary 请求的实际输入范围为 6,821–15,133、均值 10,154。日志同时出现 `langchain-openai` 关于 custom HTTP transport 与系统代理自动发现的警告。为同一 `OneShotPrimaryModel` 注入本地 HTTP proxy 做离线探针后，mock 请求仍通过 proxy 到达本地假端点并正常解析；警告本身不能证明 Qwen 请求绕过代理。请求大小和 120 秒 upstream timeout 仍是待证实假设；缺少 provider request ID/usage，unknown 状态和预留保持不变。

本轮只更新实施状态、风险判断与可复现证据，没有改变 OpenSpec 的规范性行为要求；相应验收任务继续保持未勾选。

### 2026-10-24 一周评测授权、预检墙钟与闭环证据绑定

评测批次必须从开始准备环境时就消耗其冻结的 wall-clock 额度。`execute_live_public_phase()` 现在在进入环境预检前记录 UTC 开始时刻，并先打开规范 phase ledger，再初始化 τ runtime 和预检四个隔离环境；ledger 中的 deadline 因此包含镜像/运行时与环境准备时间。调度器在路线参数构造前复查剩余时间；过期会持久化 `wall_seconds`，跳过未派发路线。若 deadline 恰在最终检查与原子 route claim 之间到期，ledger admission 的停止标记也会转成可审计的 skipped route，而不会派发。

固定预算 ID `sop-pilot-20261024-v1` 的授权数据库位于 checkout 外的用户级 `~/.decagent/evaluation/authorizations/`，身份由 Git remote、预算 ID 和固定项目路径绑定。授权只允许唯一候选 SHA、一个 diagnostic cohort 及其由报告 SHA 绑定的 pilot cohort；更换候选/cohort/账本路径、删除已绑定 phase ledger 或重复完成均失败关闭。每个 phase 的详细 request/token/tool/wall 和费用预留仍由候选账本保存；pilot 使用诊断报告的实际用量、unknown 预留及 elapsed 计算剩余额度。此机制阻止正常工作流通过新 checkout、候选或 cohort 重领额度；不声称抵抗本机所有者蓄意删除用户级授权数据库。

真实就绪 gate 校验 report map key、冻结 task ID、route ID 与回执身份一致；闭环还要求同一条路线存在已结算 Qwen primary attempt、MCP tool 调用、官方评分和 verified cleanup，不能从不同路线拼接。对应 regression 覆盖模型与评分跨路线、路线 ID 错绑、预检 deadline 停止、candidate/cohort/state 重置及 phase ledger 删除。

2026-10-23 的 v10 cohort 是历史结果：4 个任务/8 条路线为 3 failed、5 unknown、0 success，清理 8/8 verified；仅 τ telecom/ReAct 获得官方评分 0.0。Jev 与裁判均未调用。历史 unknown 不重放，公开 benchmark 当时为 0/286 个已评分任务。详见[历史人读报告](../../../evals/benchmarks/evidence/public-first-wave-live-20261023-v10-9d61688.md)和[机器报告](../../../evals/benchmarks/evidence/public-first-wave-live-20261023-v10-9d61688.json)。

2026-10-24 在代码 HEAD `edd30f1e60a10a21eebd3d41bced20d6bc8b4063` 重跑固定 v10 `first-wave --check-images`，预检通过并确认 4 个首波任务/8 个 ReAct/Plan 路线槽、固定镜像已就绪、0 ledger、0 模型/MCP 派发。该 dry-run 仅核对冻结输入和本机镜像，不代表 live route 通过，不重放旧 unknown，也不构成新批次授权。详见[预检说明](../../../evals/benchmarks/evidence/public-first-wave-preflight-20261024-edd30f1.md)与[机器报告](../../../evals/benchmarks/evidence/public-first-wave-preflight-20261024-edd30f1.json)。

### 2026-10-24 CSM `find_user` schema/wire compatibility

固定 CSM 镜像的真实只读查询显示：原始工具 schema 将 `active` 声明为 boolean，而 MCP JSON 将数据库 bit 字段编码为整数。严格校验因此会拒绝一个成功响应。评测适配器仅在内联 `properties`、schema-valued `additionalProperties` 和对象 `items` 中声明为 boolean 的节点，把精确整数 `0`/`1` 规范化为 `false`/`true`；保留原始 envelope，并记录变更 JSON Pointer。它不隐式展开 `$ref` 或组合关键字；这些节点继续按冻结 schema 严格校验，失败则失败关闭。其他整数和允许 integer/number 的 union 不转换。ReAct 的对话轨迹和 Plan 节点使用同一个规范化业务值，评测结果封装版本升为 `enterprise-mcp-observation-v2`。新回归、真实 CSM 只读 MCP 集成和清理验证通过。详情见[诊断与修正证据](../../../evals/benchmarks/evidence/csm-find-user-output-contract-20261024.md)。

该证据只确认当前 pinned 输出契约的兼容问题，不能证明缺少原始调用数据的 v10 历史 `ValueError` 一定由它造成；不修改历史 attempt、评分或预算，也不以此宣称四域评测闭环完成。9.6.F/G 与 10.3 仍待真实模型、工具、官方评分和新批次授权。

### 历史执行结果（2026-10-22，manifest v9 / candidate f1a4a70）

候选 `f1a4a704fe2c36b22127006fe0bc9860733923d5` 的同 SHA GitHub Actions [CI run 39731158534](https://github.com/shuaibilx/DecAgent/actions/runs/39731158534) 全部通过，随后执行冻结 manifest v9 的 4 个任务/8 条 ReAct/Plan 路线。8 条路线均终态：4 failed、4 unknown、0 succeeded；只有 ITSM/ReAct 与 τ telecom/ReAct 到达官方评分器，二者均为 0.0；环境清理 8/8 verified。3 个 Plan primary 请求约 60 秒超时且用量/服务端请求 ID unknown，保留 198,176 Token 与 ¥0.2052352 冻结费率预留；τ retail 两条路线因用户模拟器模型客户端本地预检失败而标记 not_sent；CSM/ReAct 的 MCP attempt 为 unknown。已知用量 101,110 tokens，按冻结费率估算抵扣前 API 费用 ¥0.0853511；没有 provider invoice 证据。Jev/judge 0 次。详情见[首波执行报告](../../../generated/261022/1426_公开评测首波执行报告_f1a4a70.md)及[机器数据](../../../evals/benchmarks/evidence/public-first-wave-live-20261022-f1a4a70.json)。该 cohort 未通过成功/评分门槛；不重放 unknown，不开展剩余 smoke 或全量，也不据此推断 Jev 收益。

失败阶段只能由当前保存的安全字段描述；现有证据不能归因于网络代理、提示词大小或供应商故障。后续先做离线根因分析和隔离回归，再为任何新真实批次冻结新候选、CI 与额度。

### 2026-10-22 Evidence-led primary timeout recovery cohort

历史 v9 的三次 Plan primary 请求分别耗时 60.291、60.299、60.291 秒；冻结 profile v4 的 primary timeout 为 60 秒。这个一致性说明本机 timeout cap 是值得单独验证的因素，但不能证明请求未到达服务商，也不能确认用量或账单。历史 attempt 的 usage unknown、198,176 Token 预留、¥0.2052352 冻结费率预留和 route terminal state 均保持不变；不重放、不释放预留。

新 profile set v5 与 manifest v10 只把 `primary.timeout_seconds` 从 60 调为 120。用户模拟器和裁判仍为 60 秒，Jev 仍为 10 秒；其余模型 identity、请求参数、价格/计费、任务清单、暴露分组、资源预算及实验路线不变。profile fingerprint 为 `4fa091d2840928bfb6a00dccfbd452a2dee8c42c465512955fd63923eb0a6721`，manifest digest 为 `1096d0515168eab71b06cb6b9bde558a0fa2a5269ec723aef996886a342b2fed`。新时限仍与路线/批次剩余 deadline 取最小值，不会延长路线或批次预算。

loader 默认选择 v10，同时仍可显式读取历史 v9/v4；builder 将 v10 限定为使用含 Jev 的 v4 profile schema。新增回归验证版本身份、120/60 秒角色分界、206/24/4 固定分母和 v9 历史 cohort 可读取。v10 是 `pre_registered_not_run`；生成和 dry-run 不创建 ledger、不发送模型/MCP 请求。实际四域首波还需干净候选、同 SHA CI、image/runtime 门禁及新的独立批次授权；若 120 秒请求仍失败，应按真实证据分类，不能仅靠再延长 timeout 来掩盖根因。

2026-10-22 的最终候选 `d3e000692999c813c2ea084fe4dfa17c4f08aa05` 已通过同 SHA GitHub Actions [run 39782622299](https://github.com/shuaibilx/DecAgent/actions/runs/39782622299)，9/9 jobs 成功。CI 后重新检查固定镜像的 v10 dry-run，仍为 4 个首波任务/8 个路线槽、0 ledger、0 模型请求和 0 MCP 调用；报告见[机器数据](../../../evals/benchmarks/evidence/public-first-wave-preflight-20261022-d3e0006.json)和[解释](../../../evals/benchmarks/evidence/public-first-wave-preflight-20261022-d3e0006.md)。这只完成离线候选验收，不改变 `pre_registered_not_run` 的 live 状态，也不构成新批次授权。

离线使用 τ retail train case 0 和本地 mock callback，确认用户模拟器可在没有用户侧工具时请求文本回复，τ worker 因而发送 `tools=[]`。旧 `_schema_only_tool_validator([])` 返回 `None`，而 `PrimaryModel` 明确要求非 None 工具目录与 validator 成对注入，one-shot 客户端因此在网络派发前拒绝请求。现已将“显式空列表”与“未提供工具目录”区分，空目录由 validator 拒绝所有 tool calls。新增 loopback 测试与注册工具现有测试通过；定向 `test_public_cancellable_model.py` 为 `25 passed`，完整 deterministic 为 `2,025 passed, 18 skipped`（288.37 秒），Ruff 通过。该修复发生在历史 candidate f1a4 后，没有重写其 `not_sent` 记录；三条 Plan timeout 与 CSM MCP unknown 仍未解释。该段保留修复时的工作区测试记录；后续 v10 候选 `d3e0006` 的最终 CI 与 dry-run 证据见前文。

## Earlier execution result（2026-10-22，manifest v9 / candidate 126fe5a）

候选 `126fe5a3dc9edcb3d595d0aa40fa1a5d8713af41` 已通过同 SHA GitHub Actions [CI run 39639666250](https://github.com/shuaibilx/DecAgent/actions/runs/39639666250)，其后执行冻结 v9 的 4 个任务/8 条 ReAct/Plan 路线。8 条路线均终态：2 failed、6 unknown、0 succeeded；只有 ITSM/ReAct 与 τ telecom/ReAct 经原始 scorer 评分，两者均为 0.0；清理 8/8 verified。Qwen 14 次请求中 5 次 usage unknown，保留 252,296 Token 及 ¥0.2796608 冻结费率费用预留上界；Jev/judge 0 次。完整结果、路线级证据和 unknown 时间窗见[公开评测报告](../../../generated/261022/0133_公开评测首波报告_v9.md)和机器证据 `evals/benchmarks/evidence/public-first-wave-live-20261022-v9-126fe5a.json`。

worker 诊断仍不能把两个 τ retail ValueError 区分为本地构造阶段错误或提供方收到请求后的错误；三个 Plan timeout 也没有 provider request ID/usage。`dispatched` 仅说明本机进入派发尝试，不证明提供方已接收。不得重放这些 attempts。四域首波质量门槛没有通过，9.6.F/G 与 10.3–10.6 继续未完成；公开完整比较仍 0/286，尚无 Jev 收益结论。下一步先做不含请求正文的离线根因分析及安全阶段诊断回归，再决定是否冻结新 cohort。

后续离线复核新增完整 `PublicTauModelCallback` 本机 HTTP 回环，包含真实 one-shot 子进程、工具 schema/参数校验、回复用量与 model/request identity 解析；公开 callback、route、ledger 四组定向回归 `128 passed in 25.90s`，Ruff 通过，未调用模型/MCP/官方评分器。该回环验证当前 callback 的可执行路径，但不能解释历史 `model_worker_target/ValueError`，也不能勾选 9.6.F；真实模型和官方评分闭环仍未通过。

### 2026-10-22 HTTP 请求标识优先级回归

loopback 客户端和 ledger 集成测试同时返回 `x-request-id` 与兼容响应体 `id` 后，新增断言先复现旧逻辑把响应体 ID 记成 `provider_request_id` 的问题，再修复为优先采用 HTTP 请求头、缺少请求头时回退响应体 ID。Qwen Chat Completions 没有可核验的 Jev-style `generation_id` 时保持空值。四个公开 callback/route/ledger 定向套件重跑 `128 passed in 25.43s`，Ruff 通过，没有调用提供方或官方评分器。该修复只提高未来 attempt 的身份可追溯性，不回填或改写历史 v9 账本；9.6.F 仍需真实模型、MCP 和官方评分闭环。

### 2026-10-22 后续离线诊断分层

`_one_shot_primary_dispatch()` 现在将 `PrimaryModel.from_evaluation_profile()` 的构造期异常映射为 `not_dispatched/input_invalid`，因为该边界在调用 `respond()` 前，不会发送模型请求；`respond()` 中未分类异常映射为 `worker_outcome_unknown/model_call`，父进程转成诊断白名单阶段 `model_worker_model_call`；模型调用已返回但本地结果封帧失败则映射到 `worker_outcome_unknown/response` 和 `model_worker_response`。后两类仍保留未知 usage/业务终态。异常文本、请求体和凭证不跨进程返回。已知的响应拒绝、未派发和提供方失败仍由原 typed contract 处理。

四个新回归场景均先复现失败，再实现最小修复。v9 账本显示三次 Plan 输入上界分别为 51,653、60,124、62,435 Token，输出预留均为 8,192；三者低于 profile 的 65,536 输入上限，最大输入约为上限的 95.3%。这些请求均在约 60 秒超时；仅凭上界不能归因到输入大小，且不证明服务商是否收到请求。完整 deterministic `1,982 passed, 18 skipped`，Ruff 和 OpenSpec strict 通过。v9 两个 τ retail attempt 的旧 `target/ValueError` 无法由事后代码改写来追溯；历史 route、attempt、预留和报告保持不变。代码 commit `812bf62359c2b8dc11658fbb66cc9874e6a6ea80` 已在远端，同 SHA [CI run 39649117340](https://github.com/shuaibilx/DecAgent/actions/runs/39649117340) 五个 job 全部通过；总耗时 22 分 34 秒。没有重跑公开评测。

### 2026-10-22 已核验模型请求未发送的原因记录

构造期或输入校验失败可由 one-shot 边界证明请求未离开进程，但原先只留下通用 `verified_model_preflight_failure`，没有可用于离线诊断的异常类型。现在子进程只回传经白名单校验的异常类名，模型 attempt 记录 `model_worker_request_pre_send`、有限错误类别和安全异常类型；`http_status` 保持 null，预留仍按已核验的 `not_sent` 释放，route 不进入终态。消息、请求内容和凭证不跨进程或进入账本。定向测试先确认两处缺失，再验证修复；历史账本不回填，未触发真实模型请求。具体结果见[安全预检诊断证据](../../../evals/integration/evidence/public-preflight-diagnostics-20261022.md)。

### 2026-10-22 Jev/Qwen 语义输入配对回归

新增 `test_jev_and_primary_callbacks_receive_identical_semantic_state_and_options`，在同一个冻结请求上分别执行两种 callback，并直接比较 Jev Decisions wire payload 与 Qwen function-call 消息正文中的 `state` 和 `questions`。测试证明候选、`no_match`、问题说明及实际工具状态相同；provider worker 均为本地 mock，不发生模型或业务工具请求。语义 callback、路线执行与比较三个定向文件共 `101 passed`；Ruff 通过。该测试验证工程层的输入配对，不证明模型输出相等、真实配对质量或 Jev 性能收益；9.6.D 仍等待获得授权的真实配对试次。

## 执行边界快照（2026-10-21 02:43 UTC；历史状态）

真实评测的首要目标是同一固定候选上的 4 个任务/8 条 ReAct-Plan 路线闭环。任何新试次都须绑定干净候选 SHA、该 SHA 全绿 CI、唯一 supervisor 与规范账本，并逐次 reserve → dispatch → settle/unknown。历史终态路线不重放；Qwen usage unknown 终结当前路线，Jev usage 或费用 unknown 停止新增 Jev 派发。价格 unknown 不伪装成零费用，供应商没有给出抵扣前账单、代金券抵扣及现金支付时不得推算分摊。当前公开基准 0/286，首波结果不能证明质量或 Jev 收益；因此本地工作只推进可离线验证的边界。

本节“当前”等词均指 2026-10-21 02:43 UTC 的历史时点；后续进度以 proposal 顶部和 tasks.md 当前勾选为准。

## 批次身份与预算账本离线验收（2026-10-21）

10.10.B 已由现有受控执行边界和隔离回归验收。公开首波只能通过 `open_first_wave_public_trial_ledger` 使用 canonical cohort/candidate SQLite 路径、项目级监督锁及已存在的 `.decagent/business-eval/global-jev-ledger.json`；缺失 Jev 账本拒绝创建，CLI 初始化和路径校验不能改用替代文件。SQLite metadata 固定 manifest/cohort/profile/batch-limit 摘要，resume 对身份漂移拒绝继续；恢复时 pending/dispatched Jev attempt 转为 unknown 并保留原累计预留。

10.10.D 的工程接线已完成：`execute_live_first_wave()` 组合规范 supervisor/ledger、`LedgeredModelAdapterFactory`、`LedgeredToolGateway`、`ControlledRoutes`、官方 scorer 与 session/environment cleanup。模型 request timeout 取模型 profile、route 与 batch 截止时间的最小值并传至一次性 provider worker；MCP/τ 适配器分别使用请求级 timeout 与拥有的 τ 子进程桥。Qwen usage unknown 终结当前 route；Jev unknown/费用 unknown 通过共享累计账本阻止所有后续 Jev reservation。该项是受门禁执行链的实现和隔离回归验收，不表示真实 API、首波或四域环境已重新运行。真实锁定 τ runtime、完整故障矩阵、候选 CI 与首波仍由 10.10.E/9.6.G/10.3 验收。

每条模型 attempt 在同一 SQLite 事务内执行批次/route admission 与费用、Token、request reserve；Qwen Agent、模拟器、裁判有各自与总批次 request/Token 上限，工具与 wall-clock 上限持久累计。Qwen 费用依冻结 profile 按原币种计量并区分 provider bill、冻结费率和 unknown；本批未授权独立 CNY 现金总额上限，不推定兑换率。Jev 使用已知 USD 单次预留并原子绑定共享 `$1` ledger；unknown/pending 阻止新的 Jev 预留。

`test_business_public_trials.py`、`test_public_supervisor.py`、`test_public_model_dispatch.py` 共 85 passed；涵盖 canonical ledger、缺失拒绝、候选切换保留余额、请求/Token/费用记录及跨进程 lease 恢复身份。项目 Ruff 与 OpenSpec strict 校验通过。该结果只验收离线账本和 admission，不等同于四域真实评分/清理，也不构成新基准试次。

## v8 公开评测实测（2026-10-21，历史）

候选 `c586f7a2b31e1d68741f64736c8877063e0e03f7` 的 GitHub Actions run [39512609133](https://github.com/shuaibilx/DecAgent/actions/runs/39512609133) 全部成功。Python 3.11/3.13 各确定性 `1936 passed, 18 skipped`、PostgreSQL 集成 `605 passed, 30 skipped`，Ruff、Workbench browser、wheel/MCP `2026-07-28` 与兼容协议 `2025-11-25`、镜像和 Compose smoke 均通过；PostgreSQL 服务健康。集成 skip 中 17 个 executor Docker/SQL 与 4 个 Docker repair fixture 尚未运行，故 11.1 继续待办。此 CI 同 SHA 验证了 Linux `/proc` fail-closed 进程树回收修复。

早期候选 `38447609df10e602b8a29ef8d58941ce9ebb337e` 上的首波真实试次结果为 5 failed、3 unknown、0 completed；仅 1 条 EnterpriseOps 路线得到官方 `0.0` 分数，8 条 cleanup 均 verified。Qwen 部分 usage unknown、实际费用全部未核验；Jev 未调用。较新的 `c586f7a2b31e1d68741f64736c8877063e0e03f7` CI 通过，不代表首波报告是在该 SHA 上执行。两个首波适配错误及 Linux 进程树退出竞态已有修复，但该小样本未通过任务完成闭环，公开基准仍 `0/286`，不能得出 Jev 收益。所有 unknown 不重放；继续暂停新增公开路线，直至预算与账单可核验并重新冻结批次。

具体指标、错误阶段、固定任务 ID、离线复现与首波 skip 见 [首波公开评测报告](../../../generated/261021/0545_首波公开评测报告.md) 与机器证据 `evals/benchmarks/evidence/public-first-wave-live-20261021-candidate-3844760.json`。同 SHA 的 Python 3.11/3.13 已通过完整 CI；21 个真实 Docker executor/repair 用例仍待隔离夹具验收。其余 smoke/full 需依据可确认实耗和账单另行估算并冻结资源。

## Jev 语义 callback 的受控接线（2026-10-21）

Jev 语义判断沿用与生产 PlanRuntime 相同的 `ControlledRoutes` 和 `LedgeredModelAdapterFactory`，不另建执行器。仅在单独构造 Jev 配对适配器时显式开启 semantic callback；默认 first-wave factory 不提供该 callback，因此不会改变冻结的四域 ReAct/Plan 八路线 cohort。

回调将路由输入重新投影为封闭字段集合，并按评测 session 的 `QuestionRegistry` 重新渲染模板，逐项核对 question ID/version、问题内容、状态来源以及 trial actor/permission 摘要。可信问题转换为 TypeSafe Decisions `DecisionRequest` 后，使用冻结的 OpenRouter Jev profile 请求单个 Choices 判断。网络请求在一次性 worker 中执行，沿用 supervisor worker guard 和 route/batch timeout；没有 SDK 隐藏重试。用量缺失、响应不满足 typed decision 契约或派发后连接结果不确定时保留为失败/unknown，不能重放；能够核验的用量和费用、resolved model、provider request ID、generation ID 与 HTTP 状态通过标准模型 callback 写入路线账本。

公开 profile 的 `~typesafe/jev-latest` 是请求别名，允许响应返回带日期/版本后缀的实际 Jev 模型身份；判断该响应是否匹配 profile 时使用专用 alias matcher，而不是字符串相等。ledger 每次保存真实返回身份；同 cohort 一旦观察到多个不同 resolved Jev model，持久停止原因并拒绝继续拼接。实际模型身份缺失仍为 null，不从 alias 推断。

此接线与已审核的 `csm_knowledge_article` 模板和自然语义节点登记兼容，但只提供离线组件和受控响应测试。它没有 live dispatch、没有新增 first-wave 路线，没有同状态 Qwen/Jev 评分，也没有证明 Jev 的准确率、延迟或成本优势；这些仍需独立冻结的配对 cohort 与真实账单/用量证据。

本次验证结果：callback 文件定向测试 `8 passed`，含公开模型派发、PlanRuntime 与试次账本的相关回归合计 `152 passed`；全量 deterministic `1947 passed, 18 skipped (239.28s)`，Ruff 检查通过。最终 `actual_provider=null` 边界修正后再次运行 callback 定向集 `8 passed`。代码提交 `1f14da73480218d431040fb11a194f326dd4086a` 已推送；GitHub Actions CI run `39532141696` 于 2026-10-21 02:43:15 UTC 全部通过，两个 Python job、Workbench browser 和 build/package/container jobs 均成功。工作区仍有其它未提交的 guest 变更，live candidate gate 保持关闭。

身份记录边界：profile 保存请求路由 `openrouter`，但只有响应明确提供且经校验的实际上游身份才写入 `actual_provider`。当前 Decisions callback 能核实返回模型版本、request/generation ID、usage 与 response cost，不能核实物理推理服务商，因此 `actual_provider=null`；不得把请求路由端点复制成实际上游提供方。最终字段修正后，Jev callback 定向套件仍为 `8 passed`、Ruff 通过。提交 `1f14da73480218d431040fb11a194f326dd4086a` 的远端 CI run `39532141696` 已于 2026-10-21 02:43:15 UTC 全部通过。

## 计费映射核查（2026-10-21）

阿里云 Model Studio 的官方 [`qwen3.8-flash` 定价页](https://help.aliyun.com/en/model-studio/qwen3-8-flash)确认该模型调用 ID，并按区域公布未促销列表价：北京输入/输出 ¥0.8/¥2.7 每百万 token，新加坡 ¥1.094/¥3.427 每百万 token。项目实际 endpoint `maas.qianwenaiapi.com` 是转发地址；现有配置不能证明其目标区域、缓存命中类别或请求实际计费档位，账户账单中的券抵扣/实付也没有逐请求证据。因此 profile 保持 `pricing_status=unknown`，不把某一区域费率当成已核验值，也不把代金券视作免费调用。

## Context

动机与范围见 [proposal.md](proposal.md)。本设计以 2026-10-15 的源码为依据；源码路径均相对 DecAgent 项目根目录，OpenSpec 也位于该项目的 openspec/。

| 现有位置 | 已核实行为 | 设计影响 |
| --- | --- | --- |
| business/services.py:175、432 | 普通工具队列；primary 模式队列耗尽再次规划；当前参数必须是最终值 | 独立条件计划解释器，不在旧队列上附加任意表达式 |
| business/questions.py、calibration.py | 可信 Choice/Noul/Score、模型版本与策略匹配 | 保留语义来源，扩展注册表与按节点消费 |
| business/graph.py、worker.py | business-2、持久 checkpoint、版本匹配 claim、租约 | 不能简单把常量改为 business-3；需要按版本分派 |
| business/operations.py、execution.py | prepare/原员工确认/commit、幂等回执和未知提交核实 | 计划节点只引用现有治理服务，不再实现第二套业务写入 |
| storage/usage.py | 混合请求上限；工具 guard 也检查模型调用次数 | 分离模型请求额度和执行动作额度，仍保留整体安全预算 |
| memory/、runtime/services.py | 旧项目/Git/PR 记忆与上下文 | 业务记忆新增独立契约，不套用 PR 合并激活 |
| gateway/business.py、static/business.* | 员工工作台、审批/操作收件箱 | 扩展计划与判断面板，保持身份与 CSRF 边界 |
| evals/integration/conftest.py | 独立测试数据库派生新数据库 | 新测试仍使用隔离库，不清理或复用用户业务数据 |

主规格目录为空，相关历史要求位于 change specs。对应关系：

| 历史契约 | 本次处理 |
| --- | --- |
| business-decision-routing 的可信问题与校准绑定 | 保留；LLM 仅组合模板和参数来源 |
| 低分/无校准可 primary fallback | 新计划版本改为持久人工决策；不能因低分自动追加 primary |
| business-task-lifecycle 的队列/总体预算 | 新版本增加计划节点与独立额度；旧任务保持原语义 |
| business-operation-governance | 全部保留；人类语义选择不等同于写入确认 |
| project-memory 的 PR 合并激活 | 历史修复保留；新业务记忆按自身证据及员工确认规则激活 |
| 旧 business-evaluation 的数据量及性能记录 | 保留历史结果；本次使用新冻结协议，不追改旧成绩 |
| mcp-business-acceptance 的协议/权限/恢复 | 继续回归；Windows 本机通过仍不等于独立 Linux 通过 |

## Goals / Non-Goals

**Goals:** 将业务编排表示为有界、可验证的数据；DecAgent 掌握状态、预算、身份、审计与恢复。规划器只是可替换的条件计划生产者。质量与性能可以用固定数据和配对执行复现。

**Non-Goals:** 本次不生成或执行任意 Python/JavaScript/SQL/shell，不将 Skill 文本当执行权限，不引入必须的 Pi/向量库/远程 tracing 服务，不做多租户 SaaS 或真实公司 SSO。首版运行时顺序执行业务副作用；独立 Jev 问题可批量，同批不作答案间依赖。

### 公开评测的单机监督与规范账本装载（2026-10-20）

公开首波不能只依赖 SQLite lease 判断发送者已死亡：lease 过期仅允许账本 fencing，真实出站权限还需要本机监督进程生命周期锁。`PublicSupervisor` 在固定项目路径 `.decagent/business-eval/public-evaluation.lock` 上持有 OS 文件锁（Windows `msvcrt`，POSIX `flock`）；进程退出由 OS 释放锁。只有持有该规范锁的进程可以调用 `open_first_wave_public_trial_ledger`。它只接受冻结的 smoke cohort 和已通过同 SHA CI 门禁的候选；当前 route ledger 路径由 cohort digest 与 candidate SHA 在 `.decagent/business-eval/public-trials/cohorts/` 下确定，同一候选恢复原账本，不同候选使用独立文件。历史 `.decagent/business-eval/public-trials/public-smoke-v7.sqlite3` 不迁移、不覆盖；Jev attempt 仍只读打开既有 `.decagent/business-eval/global-jev-ledger.json`，不允许 CLI/state path 重新选择或在缺失时初始化。换 phase/state path 或并发进程不能因此重新获得额度。

OS 锁现已由受门禁保护的 first-wave dispatcher 在环境预检后、打开规范 ledger 时持有，并通过跨进程排他性单测。端到端“已标记派发后挂起、lease 过期、第二进程等待、旧进程恢复”演练仍未完成；当前工作树不满足 clean candidate gate，尚无真实模型派发。只有 dispatcher 持锁并独占模型凭据并完成该故障演练后，才能宣称真实派发竞态已解决。

一次性 provider worker 的 POSIX deadline 清理使用独立 session/process group。Linux 下 supervisor 等待 worker exit sentinel、再次发送 group signal，并在 reap worker 前扫描 `/proc`：跳过已退出的 PID 和 zombie，仅在完整进程表扫描确认 group 无活动成员时报告回收成功。扫描不完整或 2 秒 drain deadline 到达时返回 `ProcessTreeDidNotExit`，上层保留 attempt/route unknown，不将不确定清理写成成功。该门禁用于避免 SIGKILL 后仍处于不可中断内核等待的后代被误判已清除。

公开路线的业务终态、官方评分终态与环境清理终态分别保留。任何已派发的工具结果变成 unknown 时，路线保持 unknown，跳过后续模型/工具及评分；监督者可补写评分/清理状态，但不得把 unknown 改写成 completed。清理证据使用摘要保存，避免把私有任务数据复制进公开 ledger。

## Decisions

### 1. JSON 条件计划 + 静态 LangGraph 解释器

采用固定运行图解释数据计划，而不是由 LLM 生成图代码。继续利用现有 PostgreSQL checkpoint 和租约；计划独立有 schema_version、plan_id、revision、content_digest、registry_fingerprint、goal、nodes、entry 和完成证据定义。

节点类型：

| 类型 | 内容与责任 |
| --- | --- |
| read_tool | 引用已注册只读 MCP 工具，保存真实结果及资源版本 |
| transform | 使用注册的纯函数做选择、复制、格式化和计算；禁止 eval |
| decide | 引用可信 question_id/version、状态绑定、候选来源和 policy_id |
| branch / join | 依据已接受决策或类型化事实走有限边；显式处理缺值及无匹配 |
| ask_user | 缺信息、计划失效或语义不确定性待办；不能借此自动批准写入 |
| governed_write | 使用已有 prepare→确认→commit 协议；保存 operation_id 和实际回执 |
| complete | 按受信目标契约及已核实证据判定完成；默认确定性渲染结果 |

计划中的 branch 仅使用封闭操作集：eq、in、exists、all、any 及已注册数值比较。输入绑定为 literal、task_input、node_output、accepted_decision、verified_memory；记忆绑定只能声明 `memory_kind + subject` 稳定槽，员工记忆 ID 仅在运行时解析后用于受限审计，不能进入发布模板或用户输入。引用使用受限字段路径，不允许任意属性访问或可执行字符串。每个绑定保留来源、版本、类型、可空性和敏感级别。

初始主模型输出并不需要包含未来值。比如先创建工单获得 ID，再读取部门目录，由 Jev 选择实际候选 ID，最后准备分派。工具 schema 的最终参数校验在绑定完成后执行；静态检查先验证引用类型和路径可达性。不能把占位文本当真实 ID。

编译器拒绝未知工具/模板、重复 ID、悬空引用、类型不匹配、读取未来节点、未定义分支、无界循环、试图覆盖执行员工身份的常量、被权限过滤掉的候选来源、不可验证的完成条件。合法处理人仍可来自当前授权候选。合流只等待实际激活的前驱；引用仅在另一分支存在的数据必须通过显式合流映射或缺值出口处理。计划结构合法不代表有当前执行权限，运行时仍重新核验。

首版循环采用编译前有界展开或注册重试策略，展开后为 DAG，不支持任意 while。默认展开后至多 32 节点、分支嵌套 4 层、控制路径静态分析至多 4,096 项、只读请求最多 2 次尝试。写操作重试服从未知结果核实协议，不能套通用重试。超过限制返回结构化编译错误并受控修订或请人缩小任务。

备选：每到分支重新调用 LLM 会重复支付规划成本；首调列出所有具体参数无法预测未来事实；自由代码编排难以静态验证。数据计划在这三者之间保留可检查的边界。

### 2. 可信判断注册表与批量阶段

question 模板包含 primitive、版本、判断说明、输出类型、允许的选项来源、Score 各档含义、Noul 正反语义、无匹配出口、允许消费的字段、最大状态长度和风险类别。发布文件纳入包资源并绑定评测记录。LLM 只能引用模板及其允许的插槽；不能传入新的 instructions、criterion、score scale 或权限。

动态候选先由真实 MCP 结果和当前权限过滤，保留候选 ID、显示名、来源与版本。没有合法候选时走无匹配或补充信息，不能制造候选；单一候选可在可信规则明确允许时直接绑定并记录 rule_decision，不能伪造 Jev confidence=1。

计划预先排列所有已知阶段，但只将依赖就绪、状态摘要相同且互不依赖的问题合入一个 Jev 请求。默认每批最多 8 问题、候选最多 256 项、状态最多 8,000 输入 token（采用已配置计数器的保守估计，实际 usage 另记）。超限先使用注册的范围筛选/拆批，不能截断候选后假装全集；仍无法覆盖则请人缩小范围。

同一批内问题独立求值，不能让后一个问题引用本批前一个答案。可选推测默认关闭；启用时只记录未消费答案及费用，实际路径激活后才做阈值门控。数据、候选、模板、模型或权限版本变化使缓存失效。[TypeSafe fan-out](https://docs.typesafe.ai/patterns/fan-out)

### 3. 规划器接口和最少模型调用

新增 Planner 协议：输入结构化 PlanningContext，返回候选计划或未执行片段修订，外加真实 provider usage/request IDs。首个适配器继续使用 models/primary.py 的现有直接模型连接。将 schema/prompt 与 transport 解耦，方便以后接 Pi；当前不安装 Pi。

首次规划包括目标、已注册工具、可信模板、允许参数来源及边界；在 schema 校验失败时最多一次格式修复重试，仍计入任务总额度。普通已知分支、成功工具响应、低分判断和队列自然走完均不构成再次规划的理由。低分请求人；只有新事实超出计划、明确新增目标、不可用能力或合同版本失配才申请局部重规划。

局部重规划提交 parent_revision、触发证据、可变节点集合和新片段。已执行节点、已准备操作、待确认项、未知结果及其依赖为冻结区；补丁校验后 CAS 发布新 revision。需要改变已准备操作的参数时，先明确使旧操作失效/终结，再创建新准备项，绝不能修改原操作记录。已提交操作只能由显式授权的补偿业务处理，不能从历史中抹去。

默认不请求主模型生成最终套话；使用目标与真实回执模板输出。确需自由文本生成，必须经同一模型请求记账接口。未来 Pi 适配器必须能观察其内部所有请求、重试、摘要、取消与费用；不能满足则不得作为受限预算规划器。

### 4. 持久节点日志和副作用协议

新增业务计划、节点运行和人工决策的持久记录。checkpoint 保存当前位置与引用；业务事实、模型用量、已准备/提交操作由持久表负责，不以模型聊天历史为事实源。

节点运行键为 task_id + stable_node_id + execution_generation；plan_revision 记录当前计划，不能仅因重规划改变已有副作用的幂等键。准备前在同一租约/任务锁约束下登记运行记录；调用 MCP 后保存结果。进程在远程提交之后、本地保存之前崩溃时，恢复用原 request_key/operation_id 查询回执，不重新创建操作。已知未提交且无在途执行的重试复用协议认可的身份。

模型请求也先持久预留，发送标记与请求关联写入账本。崩溃后无法证明未发送的调用保留未知计费/结果约束，不能通过重新编号绕开上限。明确本地校验失败且从未发送才可记零费用。

LangGraph interrupt 只负责等待与恢复，权限由服务端数据重新检查。等待有明确 kind：decision、missing_input、write_confirmation、budget、reauth、reconciliation、plan_invalid。未知提交不能被普通人工回复、阈值调整或重新规划变成“已失败可以重试”。

~~~mermaid
sequenceDiagram
    participant U as 员工
    participant R as DecAgent 解释器
    participant J as Jev
    participant DB as 持久状态
    participant M as MCP 业务服务
    R->>J: 当前状态 + 可信问题
    J-->>R: 答案、分布和 usage
    R->>DB: 决策、阈值策略、费用
    alt 当前节点低分
        R->>DB: 原节点人工待办
        U->>R: 选择或补充信息
        R->>DB: 身份/版本校验后消费回复
    end
    R->>M: prepare(真实参数, 原请求键)
    M-->>R: operation_id 与确认摘要
    R->>DB: 保存 operation_id
    U->>M: 服务端原员工确认
    R->>M: commit(operation_id)
    alt 响应已核实
        M-->>R: 真实回执
    else 响应丢失或重启
        R->>M: 查询原 operation_id
        M-->>R: 已提交 / 已知未提交 / 仍未知
    end
    R->>DB: 节点证据或 reconciliation 等待
~~~

### 5. 将模型额度与业务执行额度分开

在现有任务锁下保留原子预留，新增 model_family、purpose、plan_revision、node_id 和 attempt 的归属。provider 路由与模型类别分开，避免 OpenRouter/千问字符串推断主模型或 Jev。

| 默认值（本次设计选择，可配置且版本化） | 含义 |
| --- | --- |
| primary target 3 / hard limit 6 | 1–3 为普通任务目标；第 4–6 次必须有异常原因，6 次后等待，不能自动扩额 |
| Jev hard limit 24 / total model limit 30 | 所有实际请求含重试；同请求 8 问计 1 请求、8 问题 |
| plan revisions 4 | 初始版本 + 最多 3 次局部修订；校验失败的模型请求也耗调用额度 |
| expanded nodes 32 / node attempts 128 | 节点执行次数终身累计，不因重启或修订归零 |
| max tokens 100,000 / active seconds 1,800 | 延续现有默认总量；人工等待不计活跃时间但计总完成时间 |
| graph transitions 1,024 | 新解释器内部控制步骤的持久上限，与 128 次节点执行分开；旧图仍遵守旧限制 |

primary/Jev/total model 次数与 token 额度只约束新的模型请求。已完成计划在这些额度耗尽后仍可执行不再需要模型的合法节点；primary 耗尽但 Jev/总模型额度仍足够时允许 Jev。模型额度不能绕过总体费用未知约束、任务总费用、活跃时间、动作上限、取消或租约。非模型动作仍检查这些整体边界，不能因这次拆分扩大原授权。

查询已有操作的真实回执沿用原有核实例外，不发新业务授权。预算增加需要已认证的现有预算流程，并写事件；不由计划或记忆改变。

### 6. 分类型确定性与人工接管

原始概率、原始 confidence、primitive 类型和模型解析版本完整保留。Choice/Score 使用服务返回 confidence；Noul 记录 p_yes，并提供名称明确的 derived_certainty=abs(2p_yes-1)，正反答案按 0.5 分界，p=0.5 直接视为未决。Score 值和 certainty 分开，不拿分数本身当把握程度。[TypeSafe confidence 定义](https://docs.typesafe.ai/confidence)

策略包含 question/version、实际模型版本、候选来源/规模适用范围、数据版本、风险、normalization_version、阈值、有效期与验证记录。匹配且 certainty >= threshold 才能消费模型答案；等号边界测试固定。Noul 0.95 表示 p>=0.975 或 p<=0.025，不能误设为 p>=0.95。

没有有效策略、模型版本漂移、值异常或当前关键节点低分时进入人工/策略不可用状态，不静默 primary fallback。记忆写入提议未达到有效阈值时保持待复核，不创建或激活记录；记忆读取不走语义筛选，也不调用 Jev，由已发布工作流稳定槽和运行时权限/新鲜度校验决定。用户可在已验证范围内提高阈值，管理员最小阈值不能被任务参数降低；未评测的新阈值只能在隔离实验模式扫描，不宣称是合格默认策略。阈值降低到测试网格的行为只发生在评测，不关闭硬权限与写入确认。

人工待办绑定 task、node、plan_revision、question、state/candidate digest、actor 和 policy。显示必要证据、合法选项与建议；允许选择合法候选、无匹配、补充、拒绝，不能只有“同意 Jev”。Noul 可选是/否，Score 展示每档含义。回复由原员工在 UI/API 经认证提交；服务端重新过滤权限和核对状态，CAS 消费一次。过期、不同员工、旧 revision 或候选失效的回复不能推进，刷新证据或生成新待办。

低分待办不是写入审批。人工选定“分派给 A”后，prepare 的具体参数仍需原员工在现有确认页面确认。无回应保持等待；不设置静默超时同意。只对实际路径关键决定产生待办，旁支推测结果不触发。

~~~mermaid
flowchart TB
    subgraph MODEL["规划与判断"]
        P["少量 LLM 编排"]
        J["Jev 当前节点判断"]
    end
    subgraph RUNTIME["DecAgent 运行时"]
        V["编译与当前权限校验"] --> J
        G{"有效策略且达到阈值？"} -->|是| B["选择实际分支与绑定参数"]
        B --> W["prepare 写入"]
    end
    subgraph HUMAN["原员工"]
        H["查看证据并选择 / 补充 / 拒绝"]
        C["具体写入确认"]
    end
    subgraph SERVICE["MCP 与持久存储"]
        E["提交并核实真实回执"]
    end
    P --> V
    J --> G
    G -->|否| H
    H --> R{"合法选择且版本有效？"}
    R -->|是| B
    R -->|否| X["补充、拒绝或计划失效处理"]
    W --> C --> E
~~~

### 7. 结构化上下文和独立业务记忆

采用三层：

| 层 | 数据 | 使用规则 |
| --- | --- | --- |
| 审计与事实 | 原始决策、权限快照、工具结果、回执、人工响应、费用 | 完整但访问受限；事实与推断分别标注；不全量复制给模型 |
| task working memory | 当前目标、有效计划、已完成/待做节点、决策引用、未知结果和预算 | 以持久事件/事实投影更新，可重建；不会覆盖历史记录 |
| business memory | 已确认偏好、经真实执行验证的流程经验 | 原员工范围、证据引用、版本、TTL、撤销与替代关系 |

新模块 business/context.py 负责 PlanningContext / DecisionContext。通用规划上下文优先保留目标、约束、冻结节点、已核实副作用、待审批/未知结果、触发重规划的观察与人工更正，并包含必要的 Jev 判断；它不自动检索或注入跨任务记忆。只有已发布工作流在某个节点绑定 `verified_memory` 时，执行器才按稳定槽解析记忆；稳定槽由 `memory_kind + subject` 标识，不包含员工记忆 ID，也不能由任务输入覆盖。结构化事实用确定性方式压缩；普通输入预算默认 12,000 token。预算不足以容纳硬约束时暂停/缩小范围，不截断授权边界。若未来使用 LLM 摘要，走同一额度与计费接口。

不把 Jev “建议分派”记录成“已分派”。每条观察标注 verified/inferred/user_confirmed 与 source_event/receipt。工具结果、Skill 附加文本与记忆内容当作不可信数据渲染，不能变成系统指令。凭据、环境密钥与完整认证 token 不进入模型或一般 trace。

业务记忆独立表使用可信 `workspace_id`、`employee_id`、kind、subject、content、evidence_refs、scope_revision、contract/question/plan fingerprints、status、created/verified/expires timestamps、supersedes 和 content_digest。workspace 从认证身份解析；候选、来源、规则与撤销均在该空间内校验，稳定槽仍由已发布工作流声明 `memory_kind + subject` 并按当前员工解析。默认偏好有效期 30 天，流程经验 7 天；授权撤销、证据撤回、契约变化或过期立即失效。活动工单状态、当前人员权限和审批许可不得长期当事实缓存。

写入：显式偏好经当前员工确认；流程经验需真实完成证据和已发布的提炼规则，自动整理不能捏造因果经验。Jev 可用已评测模板筛选是否值得提出写入，拒绝时不写；它的“是”不能替代来源证据、本人确认、去重或 ACL。读取不做通用语义搜索，也不调用 Jev 决定是否读取：工作流只有显式引用一个稳定槽时才读取。每次节点分派前，执行器在数据库中按当前员工、公司范围、业务 scope/account revision、记忆类型和 subject 精确解析唯一的活动有效记录，并复核来源、版本与有效期。无匹配或多条匹配时暂停该依赖并请求修正/澄清；不任意挑选，不调用主模型或 Jev 猜测记忆。工作流未发布或任务不属于已发布工作流时，记忆绑定不可执行。

记忆删除/更正立即排除于后续上下文；依赖其稳定槽的待执行节点重新解析，失效或歧义时暂停并请求修正/澄清，必要时局部重规划。已分派或完成节点保留当时的审计证据，不重放操作。历史审计保存原引用和撤销事件但不继续提供失效内容。旧 repair/project-memory 的 PR 合并激活保持隔离。

备选：全量重放聊天会扩大成本且混淆推断和事实；引入向量库不能替代 ACL、新鲜度和证据，首版采用结构化索引检索即可。

### 8. 已验证流程复用

新增 workflow_registry，状态 draft→validated→published→revoked；发布是受控管理操作，不因一次任务成功自动晋升。条目绑定计划内容、适用目标/输入 schema、工具与问题版本、策略兼容范围、验证报告、作用域和无具体用户数据的参数槽位。

首版至少提供：创建工单；按真实目录和规则分派；查看工单后按需要调整优先级三个现有工具可承载的流程模板，分别使用 create、directory/assign、get/set_priority 等现有命令。支持范围以当前注册工具为准，不假设存在通用 update 工具，也不新造公司 API。

匹配优先使用用户选择的明确流程 ID、结构化输入及可信确定性条件；可用已评测 Jev 选择已发布流程，但这仍计为 Jev 调用。任意自由文本不能仅靠相似度宣称匹配；不唯一/缺参数则询问，未覆盖则调用规划器。不把“有缓存”当作适用性证明。

复用前检查版本、scope_revision、当前权限、必要参数和前提，运行时重新读取目录/资源事实。存储参数引用而非上次工单 ID、操作 ID、审批或员工令牌。实例有新 task_id 与自己的节点/操作身份。模板发布以多分支回归、负面案例与真实收据约束为依据。

命中有效模板的成功任务必须实际 primary_requests=0，包含摘要和最终回复。冷启动/未命中/异常重规划单独报告。失效模板保留历史可追溯但不命中；自动回落到初次规划仅限其预算与触发条件允许时。

### 9. 模块边界与持久数据

建议新增（实现时可以小幅调整文件名，契约不变）：

| 模块 | 职责 |
| --- | --- |
| business/plans/contracts.py、compiler.py、bindings.py | schema、封闭表达式、类型/路径校验、运行时参数来源 |
| business/plans/runtime.py、repository.py | 就绪节点、分支/合流、CAS revision、持久节点事件 |
| business/plans/planner.py、registry.py | Planner 协议、发布模板与复用 |
| business/decision_policy.py | 分类型 normalization、匹配策略与门控 |
| business/context.py、memory.py | 证据投影、有限业务记忆 |
| business/legacy.py 或隔离 legacy 包 | 原 business-2 恢复/基线入口；不再承担新任务默认执行 |
| evaluation/business/conditional/ | 数据 manifest、采样、预测缓存、阈值、配对流程评测与报告 |

现有 execution、operations、grants、employee_auth 与 MCP transport 保持职责。services.py 改为协调器，避免新增计划逻辑继续堆入同一大文件。使用项目已有 Pydantic/jsonschema、LangGraph、psycopg、httpx/mcp 与测试规范，不为此更换依赖栈。

数据库在当前 schema 11 后追加下一个可用迁移号；应用时重新核对编号。计划修订、节点运行、业务记忆和发布模板分表；待办可复用 app.approvals，但必须增加明确 decision kind 和不可混同于 operation confirmation 的校验。账本增加可空分类字段供旧记录兼容，不能猜造历史细分用量。新增 API 只供当前员工读取计划/回复语义待办/调节允许阈值/管理自己的记忆；发布和最低策略由管理权限控制。

### 10. 评测协议与默认阈值

#### 数据和冻结流程

主要端到端评测使用 EnterpriseOps-Gym 的 CSM/ITSM 任务。每次实验固定仓库版本或提交、任务版本与 ID、许可、容器镜像摘要、数据库快照、工具目录、配置和初始环境指纹；每个任务从其规定的初始状态重置，并使用 benchmark 自带的最终状态 verifier。优先保留原始完整任务，不改写目标、工具结果或成功条件。若预算或运行资源只允许执行固定子集，必须公开选择规则、全部任务 ID 和覆盖范围，不得宣称跑完全量 benchmark。

当前可复现冻结选择记录在 `DecAgent/evals/benchmarks/manifests/source_manifest.json` 和 `task_inventory.json`：EnterpriseOps-Gym 固定到数据集 revision `c8e538eae8a6205294f0a86675fefdc1fac408f6`，主流程选择 CSM/ITSM 两域全部 oracle 行（各 103，共 206）；τ-Bench 固定到 commit `96a3ef92329baf908ffe9dfe188cea34e8701d2a`，补充选择 retail 与 telecom 官方 test 划分（各 40，共 80）。完整官方 ID/划分与确定性 smoke 子集也保存在 inventory。原始数据只保存在本机忽略缓存，无需用户另行下载或将数据重新提交到项目仓库。正式记分前必须通过来源清单校验并具备 manifest 固定摘要的 CSM/ITSM MCP 镜像；固定镜像已于 2026-10-18 按摘要拉取并通过本机来源校验；该就绪检查不代表正式流程完成，未经实际执行与官方 verifier 仍不得发布正式分数。

τ-Bench 作为对话与澄清补充集，使用固定仓库版本、域、task IDs、数据库与政策文件。其用户模拟器用于交互压力测试，模拟器自身的模型调用、token、费用和延迟单独记账；模拟用户结果不能冒充真实员工审批或接管数据。τ-Bench 的环境工具通过其 Tool/ToolKit 接口提供，评测适配层须保留原工具语义和状态校验；若经 MCP 包装，所有执行策略必须走同一适配层。版本升级后分开报告，不跨不兼容版本合并成绩。

两个 benchmark 都保留官方任务与划分；无官方划分的实验子集以业务流程/任务族为单位固定切分并公开随机种子。任务盘点标注步骤数、工具依赖、状态依赖、结构化分支、语义运行时分支、对话澄清和异常恢复类型。gold state、expected actions、标签和 verifier 私有内容不得进入待测 Agent 输入。Jev 相关统计只在预先标注的语义运行时决策上计算；结构化规则任务与完整工作流结果分别报告。

DecAgent 自有的业务契约专用集保留为针对性补充，至少 40 个开发/验证场景和 80 个冻结验收场景，覆盖中文、全部 primitive、动态候选、权限、故障、人工与记忆。开发与留出必须按完整场景族隔离，同一模板或其受控变体不得跨集合。`evals/benchmarks/business/frozen-v2.json` 按场景族划分 40/80，覆盖 Choice、Noul 与 Score 的工作流判断；`memory-decisions-v1.json` 另提供 40/80 记忆决策场景，覆盖当前发布的 Choice/Noul 记忆写入、读取必要性和相关性模板。旧 `frozen-v1.json` 保留作历史记录，不得用于泛化结论。两份当前数据集的标签均为 synthetic reference，人工复核清单仍待完成；未复核项不得标记为人工验证、不得用于宣称实测准确率或选择运行策略。自有集只作针对性补充，不得替代 EnterpriseOps-Gym 主流程成绩，也不得被标成外部 benchmark 结果。各默认决策族若没有达到下述有效自动样本量，则继续标记待验证；模型生成用例标为 synthetic，关键标签需人工核对。

#### 比较对象

#### 真实最终回复（2026-10-19 确认）

公开计划执行完成后，从当前试次 journal 的实际工具返回生成确定性回复，包含来源 call ID、工具、实际状态与原始观察；只读取已返回的证据，不读取任务 gold、verifier 配置、SQL 查询或裁判结果。计划 goal.description 只描述意图，不可当作已完成事实。存在未知写入时不生成成功回复，也不调用总结来猜测结果。

最终回复策略在运行前冻结并绑定 ComparisonTrial 摘要，所有待比较路线使用相同策略。默认优先使用有界的确定性证据渲染；公开输出契约事先声明必须生成叙述，或完整证据不能在输出上限内表达时，才调用一次显式注入的 Qwen 总结回调。无法在输入预算内完整提供所需证据时停止，不静默截断。总结输入仅有公开任务、政策与本试次实际观察，工具文本视为不可信数据；输出必须引用有效证据，不改变工具状态、权限、完成证明或 verifier。

总结请求经同一预算与 ModelReply/ObservedUsage 计量，属于 Agent primary 请求而非免费的 harness 工作或官方 judge；失败和未知用量保留，不重试。真实评测的主模型就绪门禁仍适用于总结。命中流程复用却追加总结的试次如实报告非零主模型调用，不能据此宣称零 LLM；正常确定性可表达的复用任务仍保持零调用。回复一旦交给官方 verifier 不得据评分改写或重试任务。

该结果回复机制只适配原始公开工具证据，不改变 service-tickets 生产完成校验或将官方任务缩减为纯数据库子集。

#### 比较路线

2026-10-18 执行边界补充：自有数据只作辅助回归，不再作为阈值定策候选。保持冻结集合不扩增、不读取 holdout 调参；工作流六个判断族各 40 条，记忆写入/读取必要性/相关性分别 20/10/10 条，即使全部预测正确也不足每族 100 条门槛。没有独立且复核的验证队列时只报告诊断、保留人工默认；实际准确率和阈值曲线没有匹配预测时均不填造。

主模型保留 `qwen3.8-flash`。先离线诊断请求大小、超时、schema 和参数构造，再在新的就绪授权内最多执行 3 次真实请求：最小只读连通性请求、真实规划接口生成并编译只读计划、仅在前一步收到明确格式错误时做一次格式修复。格式修复包含在 3 次上限中；任一请求超时、派发状态或费用用量未知即停止，不自动重试未知请求、不更换模型；此前未知用量继续保留。就绪失败后暂停依赖主模型的真实路线对照，但可以继续不依赖模型的隔离器、评分器、测试和文档工作。τ-Bench 官方用户模拟器及确有必要的 LLM 裁判也使用该千问服务，分别记录请求、输入/输出 token、延迟和费用，不采用上游默认 GPT/Claude 或隐式重试。Jev 累计 $1 账本保持独立。人工探索由项目使用者完成六个合成工作台场景，覆盖 Choice/Noul/Score、无匹配、补充、拒绝及独立写入确认；仅为单人探索性证据，实际公司员工研究仍待办。

| 层 | 必需基线与实验 |
| --- | --- |
| Agent loop | ReAct + Skill + MCP，记录每个主模型调用及工具调用 |
| 计划执行 | 一次主模型规划 + 确定性执行器 + MCP；工具参数通过已验证的数据绑定映射，后续 MCP 调用不再请求模型；结构化条件只用代码规则 |
| 运行时语义决策 | 同一有效计划与执行器，在语义运行时分支上分别调用主模型二次判断或 Jev；使用完全相同的状态、可信问题和候选 |
| 确定性决策 | 对结构化条件单独测量代码规则；不要求 Jev 胜过 if/else，也不允许在该类条件上无必要地调用 Jev |
| 历史兼容 | 冻结既有 business-2 MCP hybrid 作为补充历史基线；使用 schema 11 的独立历史数据库与当前 schema 的独立数据库，以同一版本化服务台语义种子和规范化摘要验证配对；不能替代或混入上述三条主路线及公开基准分数 |
| 复用消融 | 新条件计划冷启动 vs 已发布模板复用，报告前置验证成本 |
| 记忆消融 | 同一冻结案例启用/禁用经验证记忆，控制初始状态且不跨测试样本学习 |

静态可预判分支在计划阶段确定；运行时结构化分支由代码规则选择；只有依赖真实工具结果或用户文本且无法由受信结构化规则判定的语义分支才进入 Jev/主模型配对试验。Jev 不负责权限、写入批准、回执核验或明确的 SLA/状态比较。未覆盖情况使用有界局部重规划或人工等待，不能静默执行最相近分支。

全组使用相同任务目标、工具实现、权限、身份、必要确认、模型配置和隔离初始状态。每个策略都从同一数据库快照独立重置。计划执行器直接解析前序节点输出并按 MCP schema 校验参数，MCP 工具调用本身不要求 LLM 生成 function call。原 business-2 路线通过受测兼容适配器/冻结包运行，不能以旧 CLI 成绩冒充当前 MCP 基线。

冻结的 business-2 归档记录 schema 11，且其 `Database.check_version()` 要求数据库迁移版本精确为 1–11；当前 DecAgent schema 为 17，不能在同一数据库中直接运行历史包。历史对照因此使用单独的 schema-11 临时数据库，当前路线使用当前 schema 的临时数据库。两边从同一个版本化服务台语义种子各自构造初始状态，并在开始前核对规范化业务行、员工身份/角色/权限、MCP 命令 schema 与治理规则摘要。schema 差异不得通过修改历史源码或在历史库中应用新迁移来消除；摘要或治理行为无法对齐时仅报告兼容性结果，不将其称为公平的性能配对。该补充历史基线与 EnterpriseOps/τ 的公开基准分开报告。

τ-Bench 的用户模拟调用单独计量；非性能故障场景与常见业务性能集合提前标明，不因结果差而事后删除。

#### 阈值、质量与统计

网格固定 0、0.50、0.70、0.80、0.90、0.95、0.99，另列全人工参考。单节点可对同一 raw prediction 离线重算所有阈值；缓存键包括数据样本、state、question、候选及权限摘要、实际模型/normalization 版本。分支因人工更正发生变化时，重新执行那条路径，只复用完全相同输入的预测，不将一条轨迹当所有阈值的真实任务结果。

每个适用决策族在验证集上，选择自动部分点估计正确率 >=0.95 且有效自动样本 >=100 的阈值中覆盖最高者；并列选择更高阈值。100 是本设计的最低报告门槛，不是用户声称的统计保证。没有候选达标、无自动样本或样本不足则不发布合格默认值，保留人工/未验证状态，不放低标准。0.95 阈值只是候选，默认值不是预先指定的常数。

冻结策略后跑测试；分别报告总体与各族/语言/风险/候选规模，自动部分正确率须 >=95% 才能声称该范围达到质量目标。给出比例的 Wilson 95% 区间、分子/分母；多节点流程的差异/延迟使用以任务/数据组为单位的配对 bootstrap 95% 区间（固定 seed，默认 2,000 次重采样）。不宣称区间下界 >=95%，除非实测确实如此；未达最低样本量的结果标证据不足，不发布正式策略。

自动覆盖=未因语义判断求助的决策数/全部合格决策数；全任务无人介入完成率另计，强制写入确认也算人工。节点准确率与整任务目标达成率分开。提供误接管（本来正确）、漏接管（错误仍自动）、范围外误执行、人工中断次数、人工等待、真实副作用错误数、调用/问题/token/费用、重规划率、复用率和机器耗时 p50/p95。confidence 的分桶正确率可报告；ECE/Brier 只用于有明确定义的概率或额外校准结果，不能把原始集中度直接当正确概率。

理想人工可用标签模拟并明确命名 oracle；实际员工交互另跑代表性案例，至少覆盖 Choice/Noul/Score/无匹配/补充/拒绝，记录完成与等待。不得用 oracle 成功率证明真实员工效率。

#### 成本、延迟与通过标准

实际模型请求经账本，缓存命中/脚本夹具/实网分别标记。评测仍受此前累计 Jev 1 美元授权，执行前核对既有 ledger 和未结算项，按批预留，未知费用不自动释放。Qwen API 仍产生供应商费用；代金券是账单抵扣，不是零费率。成本比较使用抵扣前 API 费用，另行记录代金券抵扣额与账户实际支付额。不从一次 CLI 调用推断一次模型调用。

时间口径分列评测环境准备、Agent 执行区间、私有评分、清理与总墙钟时间。Agent 区间从冻结任务/工具上下文就绪到终止，包含规划、编译、参数映射、MCP 和运行时检查；机器耗时从该区间减去观测到的人工与用户模拟器等待。Docker 创建/快照恢复和官方评分器时间不混入 Agent 延迟，但保留总耗时供复现。初始化失败须标记 Agent 未启动，不能将其零执行耗时当作快速成功证据。

质量达到目标、禁止副作用为零、所有恢复与隔离断言通过，是发布前提。普通成功非复用任务目标至少 90% 落在 1–3 次 primary 内；适用成功复用任务必须零 primary。90% 是本次验收设计值，需报告所有失败/异常任务及其请求量，不能只挑成功样本解释总体成功率。

流程效率结论按冻结任务配对：新路线任务成功率点估计不低于 ReAct + Skill + MCP 基线，primary 请求中位数下降；机器耗时 p50 下降、p95 不回退，模型实际计费/同价估算成本不增加。对语义运行时决策，只有在质量可接受且 Jev 相比主模型二次判断降低成本或延迟时，才声称 Jev 带来收益；结构化代码规则作为独立基线，不要求 Jev 与其竞争。业务混合集总体成本也不增加。差异附区间；区间不能支持改善时写“未证明”，不声称倍数。比较不达标不阻止交付实现和报告，但相关性能验收任务保持未通过，不把安全通过当性能通过。若预算耗尽导致未跑齐，保留明确待办。

#### 主质量对照与生产预算副评

主质量对照给三条路线相同的宽松、评测专用边界：每任务最多 200 次 Agent 模型请求、200 次工具调用、1,000,000 个模型 token、30 分钟 Agent 机器时间；τ-Bench 另遵守官方 200 步上限。该边界用于观察完整成功率与失败行为，不能把生产 primary hard limit 6 误作所有路线的公平停止条件。生产副评单独保留现有 primary target 3/hard 6、Jev 24、总模型请求 30、100,000 token 与 1,800 活跃秒等预算。两类结果分别汇总；用户模拟器和必要裁判的模型资源不混入 Agent 请求数，但单独报告其 token/费用，并计入整个实验的总成本与总墙钟时间。默认并发 1、最多每批 10 个任务，每种路线独立重置状态；Jev 批次开始前核对并预留累计 $1 账本，费用未知立即停止。

首个固定真实评测批次由四域各 1 个任务、ReAct/Plan 两条路线组成，共 8 个路线试次。该批次另受全局累计停止线约束：最多 500 次 Qwen 请求、5,000,000 输入与输出 token、14,400 秒总墙钟、1,600 次工具调用；Agent 最多 300 次/3,000,000 token，官方用户模拟器最多 160 次/1,600,000 token，官方裁判最多 40 次/400,000 token。所有重试、格式修复和受控总结按实际角色计入。每路线原有额度继续生效；unknown 不释放预留，恢复或换进程不能重获批次额度。达到任一上限时保留未开始路线和未完成配对。后续 24 例、全量和额外语义/消融试次另行根据实耗制定预算，不复制首波授权。

#### 开发暴露范围

EnterpriseOps 固定 CSM/ITSM 206 条始终保留在总分母；暴露分组按审核过的精确 case 证据，不按未审核任务族启发式扩散。报告全体、暴露组和未暴露组的准确分子分母；未暴露组只能称为未暴露子集，不能宣称为官方或独立 holdout。τ-Bench 每个域从官方 train 按固定 ID 顺序取前 2 条冒烟，后续调试只使用 train；官方 test 80 条只有在计划、模板、模型和环境配置冻结后才运行，不在查看 test 结果后调参。

2026-10-22 暴露审阅将历史任务族启发式收窄为精确 case 规则并冻结进 manifest v9：用户确认仅 CSM `task_20260106_161859_265_00364ece_0e836ce8` 与 ITSM `task_20260115_201951_323_391f1eff_659b9dae` 进入项目实测；其余 204 个 EnterpriseOps case 的内容未在项目外查看，仓库记录中仅有任务 ID。用户也确认未在项目外查看 τ-Bench 官方 test 80 条，项目开发使用 train。v9 将两个已测 EnterpriseOps case 标记为 `development_exposed`，剩余 204 条与 τ 官方 test 80 条标记为 `not_exposed`；不向相似任务族传播启发式标签。τ train 仍仅用于开发/冒烟。`not_exposed` 表示没有项目任务级暴露，不等价于独立 holdout；所有 official denominator 保持不变。依据见 [exposure review](../../../evals/benchmarks/evidence/exposure-review-20261022.md)。v8 不回写。

v9 的 manifest digest 为 `f984ec74c9776efef0b6ba808ff035198500f1997537e01776dc2a895173a604`，保留 286 个 full、24 个 smoke、4 个首波案例，并固定 v4 角色 profile、逐路线/全批资源、冷/热流程复用及员工绑定记忆消融。冻结时的 dry-run 选择首波 4 个任务/8 条路线，未创建 ledger、未派发模型或 MCP；之后候选 `f1a4a704fe2c36b22127006fe0bc9860733923d5` 实际运行了该 manifest 的四域首波。结果为 4 failed、4 unknown、0 succeeded，只有两条到达官方评分器且均为 0.0，8/8 清理验证通过。首波未通过成功门槛；历史 v7/v8 与 f1a4 unknown 均不得重放。准确路由、计量和失败阶段见[首波执行报告](../../../generated/261022/1426_公开评测首波执行报告_f1a4a70.md)及[机器证据](../../../evals/benchmarks/evidence/public-first-wave-live-20261022-f1a4a70.json)。

### 10a. 隔离评测工具注册（2026-10-18 用户已确认）

采用仅由可信评测代码构造的 EvaluationCatalog，不修改 PluginSpec.from_mapping 的 service-tickets 限制，也不增加生产 HTTP 的开关或通用远程工具注册。目录绑定活动试验作用域、基准版本、原始工具 schema 与目录摘要；关闭作用域后不能编译或执行。生产路径继续禁止覆盖员工身份；隔离基准中原 API 的 user_id 等业务参数按其原 schema 接受，不能被误作本机认证凭据。

编译器、绑定器和 PlanRuntime 仍为同一实现；仅显式支持这种受信目录的 schema/结果契约与完成策略。MCP 原响应完整保留，可提供标明来源的确定性 JSON 观察视图，不能通过模型猜测生成工具输出。未来结果路径在缺少上游 outputSchema 时作运行时类型检查；不放宽生产闭合结果 schema 的静态验证。

试验状态与生产 PostgreSQL 业务表分离：新建且独占的本地持久试验日志保存计划、节点、参数来源、真实调用和未知结果，复用生产解释器的存储端口。每次试验创建新日志与官方环境实例；不加载前一测试记忆或节点。模拟写入仅调用该试验的原始工具，成功响应不被伪装成公司审批回执；传输未知的写入停在原节点，不能盲目重试。正式企业业务的 prepare/确认/commit 代码不变。

上游明确返回的 MCP isError 是已收到的工具错误，与传输异常区分。原始成功/错误响应完整保留在试验日志；ReAct 按原观察继续恢复，计划进入显式 on_error，不能将明确错误改成未知写入。当前 EnterpriseOps 初始身份基于固定快照摘要与恢复上下文；正式配对仍须补充独立恢复状态验证，不能把该摘要称为已测量的全部数据库状态摘要。

完成节点只核验实际观察、激活路径与本试验日志，返回明确的 execution_finished；官方目标成功、错误副作用和最终状态由独立官方评分器提供。没有评分器结果不能生成合格 RouteRun。官方用户模拟器、澄清与原业务确认规则仍在相同工具适配层之外由评测协议驱动，费用单列。

首阶段先接通原始工具和结构化流程。Jev 节点仍只能引用已发布可信模板；没有适用模板时显式拒绝，不根据公开工具说明自动生成问题、评分尺度或模拟高确定性。模板、主模型 readiness 和完整公开任务的后续验收仍是独立门槛。

### 10b. τ-Bench 独立运行时桥（2026-10-18 用户已确认）

固定 τ2 版本的依赖锁与应用 MCP 2.3.0 依赖冲突，不放宽任一依赖锁，也不修改上游源码。可信评测代码为每个试验启动一个专用 Python 3.12 子进程，通过有界、带请求 ID 的本机 stdio JSON 协议连接原始 Environment 与应用侧 MCP。子进程不导入 DecAgent/MCP，应用不导入 τ2。环境创建使用原官方任务初始化；工具 schema、原始参数、文本结果、错误标志与上游可变状态元数据保持一致。工具目录不暴露任务参考答案、用户模拟器私有情景或评分器。

进程路径、来源版本、域与任务由可信评测器选择，不接受模型传入命令或任意代码。子进程禁止外网和模型调用，不继承模型凭据；启动、帧大小和单请求均有上限，协议失配/进程退出/超时后关闭该试验且不重试原工具。写入是否完成未知时由既有试验日志停在 unknown，不因重启创建新写入。关闭 MCP 会话同时回收其拥有的子进程，初始化失败也须清理。原始数据库摘要可供私有评测器比较重置状态，不能进入模型任务上下文。

80 个官方 test 任务的双重初始化一致性只证明离线运行时就绪，不能计为任务执行或成功。完整对话、用户模拟器与官方评分仍须独立接通并计量后才能报告任务成绩。

### 10c. 正式评测闭环补充

τ 工具 worker 与 Agent 共用每个试次的同一个权威 `Environment`：评测私有控制面负责构造原始 `SimulationRun`、调用原用户模拟器/用户工具、传递真实对话轮次、保留完整轨迹并调用官方 evaluator；Agent 只接收已发生的用户消息和合法工具响应。私有任务情景、参考动作、目标状态及 scorer 字段不进入 MCP 工具目录或 Agent 输入。每个路线的对话协议都能发送消息、请求澄清、接收用户回复并继续/终止；不能把此能力只提供给计划路线。官方 tau evaluator 是任务成功判定来源，工具顺序相似度不得代替官方分数。需要 LLM 裁判时遵循 10.1 的 Qwen 及单独记账约束。

用户模拟器一次回复包含多个用户侧工具调用时，先完整验证这一批，再按原顺序在同一 `Environment` 内执行；多条结果组成 τ2 `MultiToolMessage` 一次交回模拟器，所有工具结果返回前不得再次请求模拟器。评测轨迹保留批次边界、工具结果顺序及 `requestor=user`，评分桥只在构造 τ2 原始评分消息时按上游接口要求展开。不能把多调用拆成“工具一—模拟器—工具二—模拟器”，否则会改变官方用户状态和对话分支。

EnterpriseOps 的每次重置记录独立测得的初始数据库状态指纹；评测器在恢复后从私有数据库状态采集器读取状态，再与任务固定快照核对。`UpstreamScorerHook` 调用原 `VerifierEngine.execute_verifier`，保留官方业务目标结果；response-check 中若原 verifier 使用模型，也要将裁判请求列入独立 usage。没有测到的禁止副作用或中间授权边界标记为 `not_measured`，不得默认为零。

公开路线使用冻结 task manifest 和逐试次证据记录；初始化失败记为 `not_started`，Agent 失败仍进入结果与失败分母，官方评分故障记为 `scoring_error` 并可对同一保存状态重新评分，不重新运行 Agent。环境准备、Agent、用户模拟/人工等待、官方评分、清理和总墙钟分别计时。任务超过计划表达或运行资源边界时按原始任务计为 unsupported/failed 并留在分母，不能裁剪任务目标或将能力限制隐藏在预检中。

### 11. 工作台与可观测性

任务详情展示当前图/计划版本、所有节点及实际路径（未走分支明确区分）、参数来源、决策类型/原值/归一值/有效阈值、适用策略、确认与等待原因。语义人工回复和具体操作确认采用不同入口和标签；员工切换清理前一身份的详情/缓存，HTML 安全转义。

每个事件关联 task_id、plan_revision、node_id、decision/policy IDs、model request/usage ID、operation/receipt ID、actor 和 lease generation。跨模型、MCP、人工的时间线可导出脱敏 JSON；原始敏感证据只供当前有权员工检索。追踪异常需能回答“为什么又调用主模型”“为什么此处等待”“哪项操作已提交”，不需要外部服务才能恢复。

文档更新 README 导航以及 generated/ 中的使用指南、参数参考、架构解释、评测说明；固定名称的 OpenSpec 文档留在本 change。至少包含总体架构图、条件流程图、员工/解释器/Jev/MCP 泳道图、调用与恢复时序图、节点状态图和阈值结果图。图中区分已实现/模拟/待验收；计划文档图只表达设计。

## Risks / Trade-offs

- [计划过大或分支爆炸] → 节点/深度/状态限制、就绪批量和局部重规划；超过能力明确请人缩小任务。
- [高分仍错误] → 真实校准、无匹配路径、95% 目标与区间、默认策略按实际版本失效；不把高分视为授权。
- [减少主模型但增加 Jev 或状态 token] → 统计两类实际请求、问题和总费用，禁隐藏摘要；冷启动/热复用分开。
- [重启/重规划重复副作用] → 稳定节点/操作身份、冻结区和原回执核实，数据库与 checkpoint 冲突以真实操作证据为准。
- [记忆污染或越权] → 数据库先隔离、证据激活、TTL/撤销、推断与事实分开；不从一次成功自动发布流程。
- [旧版本兼容增加维护] → 仅旧任务恢复和基线使用 business-2；新任务入口唯一，后续另行清理。
- [公开 benchmark 与实际公司流程仍有差异] → EnterpriseOps-Gym 主测、τ-Bench 对话补测、自有流程集单列；所有结论限定于公开任务和已测契约，不据公开结果直接开放业务自动化。
- [预算内数据不够或目标未达标] → 记录不足、保留人工默认与未通过验收，不隐瞒或增加已授权费用。
- [人工参与影响延迟] → 机器耗时、人工等待和总完成时间分列；强制确认与低分接管分别统计。

## Migration Plan

1. 留存现有 business-2 的契约/测试/基线入口，记录当前模型与数据版本。新增迁移和模块，默认开关仍关闭；本次 propose 不实际迁移或重启服务。
2. 实施时在独立数据库验证迁移，再启用版本注册表。新任务写 business-3 和 plan schema 1；旧任务继续 business-2 executor。数据库兼容检查只拒绝未知版本，不因并存已知版本阻止全部 worker。
3. 先以模拟场景验证新解释器和真实 MCP wire；未知提交、待确认、取消、员工重新认证和租约过期跨升级回归。
4. 功能/数据协议冻结后有限真实模型评测，按策略族发布已验证阈值和复用计划；未达到目标的族保持人工状态。
5. 更新当前使用指南与界面导航后切换新任务默认版本，保留可观测开关及旧任务恢复。回滚只停止创建新 business-3；已有 business-3 保留兼容 worker 或显式暂停，绝不送到 business-2 或删除新表。
6. 历史修复与其记忆不删除；独立 Linux 部署/验收留给后续 change，不能据本机 Docker/WSL 判定完成。

## Open Questions

### 2026-10-20 冻结 cohort 的离线 phase 选择

`build_dry_run_report(plan, phase=...)` 只做只读选择，不改变 cohort digest、route ledger 或任何预算：`first-wave` 按 manifest 明确列出的四个 case 顺序选择 4/24 任务与 8/48 路线槽；`remaining-smoke` 从同一 smoke cohort 排除首波，选择 20/24 与 40/48；`full` 只接受 full cohort，选择 286/286 与 572/572。CLI `--phase` 将该选择和完整 cohort 分母同时写入 dry-run 报告。

此接口是 E7 live phase runner 的安全前置，不等于持久 claim/resume：它不会写入 ledger、认领 route、判断 completed/unknown，也不执行模型/MCP。后续 runner 必须在同一 smoke 账本中推进 first-wave 与 remaining-smoke，完整 cohort 使用独立固定身份；在 claim、恢复、锁、配额和清理都接通前，`--execute` 继续 fail closed。

`ControlledRoutes` 的循环边界已按动作语义拆开：PlanRuntime 控制步最多为已编译计划节点数（上限 32），ReAct 轮次单独封顶 200 次模型请求；模型请求与 MCP 工具调用继续由 `ObservedUsage`/持久账本各自计数。τ 固定源码 `96a3ef9` 的半双工 `Orchestrator.step()` 每次角色交接将 `step_count` 加一；Agent/User 发出的一个工具调用批次由 Environment 一次处理，因此不能按批内工具数推导步数。`EvaluationPlanSession`、`ControlledRoutes` 和公开 ledger 现分别记录并限制 `tau_steps`：用户模拟器每次回应、Agent 的每次对外动作、每个环境工具批次各占一个 τ 步尝试，账本在模拟器或工具副作用前持久预留。Plan 的 `decide` 节点也按 Agent 动作计入；模型请求数、MCP 工具数、Plan 控制步和 τ 步仍是不同维度。此映射只保证受控执行器采用与冻结运行时一致的角色交接单位，正式环境、原 scorer 和 cleanup 的四域闭环仍待 live 验收。

### 通用模型端点与有限降级

主模型优先使用 `qwen3.8-flash` 和现有 MAAS 兼容端点；备用通用模型固定为 OpenRouter `deepseek/deepseek-v4.1-flash`，默认从现有 Jev OpenRouter key 取凭证，不在配置、日志或 evidence 中复制密钥。只有 `DECAGENT_JEV_PROVIDER=openrouter` 且 key 非空时，备用配置可启动；否则明确报告 fallback 未配置。Jev 的 provider、模型及累计账本独立不变。

模型 factory 按角色构造有版本的 profile：Planner、最终回复、通用模型语义对照、τ 用户模拟器和裁判使用同一组可选主/备配置，但保持 role 单独归因。所有完整回放与公开评测在启动前冻结 profile 摘要；benchmark 各路线强制选择相同的单一模型 profile 并关闭跨模型降级，不跨模型混合分数。产品请求是否触发降级写入本任务的模型 attempt trace。

只有可重试的传输类错误、408、429 或 5xx 才能重试：同一 Qwen profile 最多两次（首次失败等待 2 秒；合法 Retry-After≤30 秒时遵守，超过则暂停），之后一次 DeepSeek fallback。认证/权限、模型不存在、参数不支持等错误立即停止同 profile；格式/plan validation 仍受 planner 的一次结构化修正上限约束。每次请求单独 Reserve/settle，UsageLedger 把暂时 unknown 的请求预留继续计入硬额度。新备用派发必须以 `recovery_of` 绑定同一任务中、刚才由 trusted orchestration 标记的单一 unknown provider attempt；最多一次 recovery，Jev 或工具写操作不能借用该例外。fallback 的输入/输出费用须有 OpenRouter provider policy `max_price` 硬上界并在本地按模型/实际使用上下文核算，否则拒绝派发。响应记实际 model/provider、OpenRouter generation id 和请求 id（若适配器可取得）；响应未知且没有 generation id 时保留 unknown，不做账单轮询自动释放。

新的独立 readiness 批次沿用原 10.7 历史账本，不覆写或重放。上限是 8 次请求、300,000 累计 input+output tokens、该批次 DeepSeek $1；每个 provider attempt 先原子预留 tokens、费用和请求槽，已用完/预留不足即停止。测试顺序是 Qwen 最小非流式/用量、工具参数兼容、生产只读计划编译；再按剩余额度以 DeepSeek 验证对应步骤；只有非流式通过后才检查流式。最终计划必须由原生产 compiler 接受，不执行 MCP 业务写入。分类记录连接/读取/HTTP/响应解析/usage/schema/compile 状态；未知问题不归为已确认的 Qwen 服务缺陷。

已知本地 preflight 失败仅能通过显式 `--resume-known-not-sent` 延续同一批次：账本必须证明没有任何已派发/unknown attempt，已有 attempt 必须均为 settled/not_sent 且 token/费用为零，已有阶段必须失败或跳过；前次身份、阶段和结果先追加归档，再允许重试。只有诊断源码指纹变化，或严格匹配旧版本的价格来源标签修正且费率数值相同，才能变更批次身份。模型、端点、费率数值和批次上限变化一律拒绝。任何已派发、计量未知或通过阶段均不可恢复。

本设计不让模型切换接管工具/MCP 重试：模型只负责重新生成纯计划/回复；若先前工具提交为 unknown，先按原 request key 查凭证再决定继续，绝不重新提交该写入。

### 2026-10-19 已批准的后续就绪授权

新授权 ID 为 `qwen-readiness-20261019-plan-execution-v1`，只在离线定位具体缺陷、增加失败回归且修复通过后启用。旧响应未保存时不得声称重现了其具体 schema 错误；离线报告须区分直接观测、可复现实现缺陷和未知外部原因。先保存模型、端点主机、catalog/schema/messages 字节规模、编译器与修复摘要，再在独立持久授权计数中最多派发 3 次全新只读请求。每次发送前持久化 attempt，格式修复也消耗同一计数且最多一次；进程崩溃留下 dispatched 记录时不得自动续跑。旧 unknown 请求保持原状态，与新授权关联而不抹除；新授权不是对旧未知请求的重试或自动解除。若新调用未知或无有效计划，停止该阶段并保留正式评测门禁，不自动换模型、扩超时/预算或再申请一轮。正常 ModelTurn 缺少 usage 与拒绝响应 usage 缺失遵守同一停止边界。

上述授权单独追踪为 10.7；10.1 保留上一轮诊断历史。游客试用与独立私有部署仓库的规格、任务和新增预算全部在 `guest-sop-local-trial`，公开基准结果不因此被勾选。

首次派发前固定本轮诊断累计 token 上限为 100,000（现有生产任务默认值），单请求保守输入上限仍为 65,536、输出上限 8,192，并计入累计总量。依据：离线量化 repair 预留约 58,635，加已观察的首轮实际 12,900 为 71,535，旧 65,536 累计边界会在用户本轮允许的一次修复前停止。本轮初始额度选择基于该测量，不修改旧账本或 unknown，也不允许执行中自动扩额。该批次此前引用的“免费额度”费率假设已由用户于 2026-10-20 更正：Qwen API 仍计费、代金券抵扣账单；不得用零费率计算比较成本，保留原始请求和未知费用，并在可核验来源到位前将金额标记为未核实。

只有不影响实现边界的登记值待执行阶段填写：下载时的公开数据精确版本/哈希与许可记录、实测解析模型版本、验证集最终选出的阈值，以及各性能指标实测值。不能提前填入虚构值；方案和失败处理已经在本设计中固定。

### 2026-10-19 公开试次 dry-run 与资源账本补充

公开执行入口固定使用 `comparison_trial_manifest_v5.json` 和
`evaluation_model_profiles_v2.json`。旧 v4/v1 文件保留为历史输入，不会被就地改写。
v2 profile 保存 provider、模型、无凭证 HTTPS endpoint、价格来源/版本、token 上限、
显式采样参数、unsupported 字段、输出/工具选择契约、timeout 与 provider routing；API key
不进入 manifest、证据或账本。通用模型客户端的评测工厂会对支持的 temperature/top_p 显式
发送冻结值，并省略 profile 标记为 unsupported 的 seed。公开对照关闭跨模型 fallback，
以维持同组公平性。Qwen v2 profile 的零费率源于此前对账户代金券的错误理解，
不得作为供应商费率或账单证据；新计费 profile 必须标出可核验的费率/账单来源，
并分别记录抵扣前 API 费用、代金券抵扣和实际支付。Jev 价格仍为 owner-reported
OpenRouter 记录；它们不是独立的供应商价格保证。

`run_public_trials.py` 默认仅做来源、清单、profile、readiness 和（启用时）本地镜像摘要
检查。dry-run 报告只能以新文件形式写入 benchmark evidence；不得覆盖既有报告。
`--state` 在 dry-run 下拒绝使用，`--execute` 在真实 callback、适配器、官方评分、清理
及预算守卫接入前 fail closed，因此 dry-run 绝不创建 ledger 或触发模型/MCP。

公开试次 SQLite ledger 升为 v3：模型 attempt 记录冻结的 provider/model/endpoint/
price version、profile digest、角色内序号、server IDs、可用的 HTTP 状态与实测时长；
工具 attempt 保存安全的工具名、effect、参数摘要、回执摘要及 unknown 状态。模型请求、
工具调用和 Agent 机器时间的额度先预留再结算；中断与 unknown 的用量预留不释放，unknown
route 禁止自动 replay。单活动执行者通过 lease heartbeat 和递增 generation fencing。用户
模拟器和裁判仍独立计数，不合并到 Agent 请求与 token 配额。

### 2026-10-20 Qwen 计费更正与 profile v3

v2 Qwen `0/0` rate 是旧的账户代金券解释，不是零费率或账单证据；v2 profile、v5 cohort 和原始 readiness evidence 保持不变。当前未能确认 `maas.qianwenaiapi.com` 在账号中对应的服务地域/费率，也未取得逐请求供应商金额，因此新的 `evaluation_model_profiles_v3.json` 对 primary、user_simulator、judge 使用 `pricing_status=unknown` 和 null 单价，并生成全新 v6 cohort。不能沿用 v2 的零费率，不把账户券抵扣解释成免费。

`ModelReply.cost_usd`、`RouteRun` 费用及公开评测账本现允许成本为 null，同时保留已知输入/输出 token。`ObservedUsage.complete` 只表示模型请求和 token 用量已知；独立的 `cost_complete` 表示成本完整性。公开 ledger schema 升为 v4：unknown-price profile 只能使用 null fee reservation，结算允许已知 token 配 unknown cost；聚合账本不会把缺失费用求和成 `$0`。这种 attempt 仍可进入官方任务评分，成本统计不得参与优势结论。Jev profile、共享累计 `$1` ledger 和 Jev attempt 的精确费用要求不变。

EnterpriseOps 的 response-check judge 现在也接受“请求数、token 和耗时已知、费用未知”的供应商账单状态：`BudgetedEnterpriseJudge` 与 scorer 继续执行原始官方 verifier，`TaskScore` 保留 judge token/请求、费用记为 null，账单完整性不会伪装成评分失败。定向测试 `test_unknown_judge_price_keeps_official_score_and_known_token_usage` 通过。τ worker 原有回调协议同样把缺失成本作为 null 传回，同时保留 token 分项。

`ProviderBillingEvidence`、逐请求账本字段与 `billing_snapshot()` 已实现，`live_public_runner` 会在每条路线报告中输出该原始粒度快照；未提供的 voucher/cash 分项保持 null，不会由账户余额推算。Qwen endpoint 的逐笔账单读取、适用地域/费率核验与账户时间窗对账仍未接入，因此 v3 profile 保持 pricing unknown，10.8.D 未完成，也不能做 Qwen 成本总额或 Jev/通用模型成本优劣结论。

已对 `comparison_trial_manifest_v6.json` 执行离线 smoke dry-run：24 个任务、48 个路线槽位，首波 4 个任务/8 个路线；ledger 创建数、模型请求和 MCP 请求均为 0。报告为 `evals/benchmarks/evidence/public-evaluation-dry-run-smoke-v6-20261020.json`。这是固定输入和当前 profile 的准备验证，不是模型或业务流程成绩。

### 2026-10-20 首波批次上限与 v7 cohort

用户确认的四域首波上限已冻结进 `comparison_trial_manifest_v7.json` 和 cohort digest：总计最多 500 次 Qwen 请求、5,000,000 Qwen tokens、1,600 次工具调用和 14,400 秒；Agent、用户模拟器、裁判分别最多 300/3,000,000、160/1,600,000、40/400,000。Jev 继续使用原累计 `$1` 账本，不在本批次中启用跨模型降级。v5/v6 manifest 与 v6 dry-run evidence 保留原样。

2026-10-21，用户为后续独立 Qwen 批次选择了同一组硬停止线：最多 500 次请求、5,000,000 个输入与输出 token、1,600 次工具调用和 14,400 秒；Agent/用户模拟器/裁判分别不超过 300/160/40 次请求及 3,000,000/1,600,000/400,000 token。按冻结费率最高 ¥2.7/M 乘以全部 token 上限，¥13.50 是抵扣前费率推导上界，不是供应商账单或预计消耗。该上限属于新的批次身份，不能挪用 v8 的已用量/unknown 预留，也不允许重放 v8 路线。新批次尚未冻结 cohort 或创建账本；样本暴露审阅与新 manifest 的所有放行门槛通过前不得派发。Jev 继续独立使用原累计 `$1` 账本。

公开 ledger schema v5 持久保存原批次开始时间、四小时截止时间、冻结限额摘要和首个 stop reason；请求/Token/工具统计从全部 attempt（含 reservation、unknown）恢复计算，unknown reservation 不释放。开始 route、模型/工具/机器时间 reservation 及 dispatch 前检查剩余批次额度。响应结算发现实际 Qwen token 超过总量或角色额度时，立即持久停止批次。早期版本只有 dispatch admission gate，尚无统一监督器与可取消执行，故当时 `--execute` 保持 fail closed；随后 10.10.B/D/E 已完成持久账本、监督派发、可取消 runner 与四域工程闭环。该工程验收不代表公开模型质量通过：v8 仍有失败和 unknown，10.3 仍未完成。

v7 离线 smoke dry-run 报告为 `evals/benchmarks/evidence/public-evaluation-dry-run-smoke-v7-20261020.json`：24 个任务、48 个路线槽位、首波 4 个任务/8 个路线槽位，模型/MCP 调用 0，未创建 ledger。它验证上限随冻结 cohort 传递，不是模型调用或业务流程测试。

v5 manifest 在原 24 smoke / 286 full 任务分母内额外固定首批四例：CSM
`task_20260106_161859_265_00364ece_0e836ce8`、ITSM
`task_20260115_201951_323_391f1eff_659b9dae`、τ retail train `0`、τ telecom train
`[mms_issue]airplane_mode_on|bad_network_preference|bad_wifi_calling|break_apn_mms_setting|break_app_both_permissions|data_mode_off|data_usage_exceeded|unseat_sim_card|user_abroad_roaming_disabled_off[PERSONA:Hard]`。
首波按 ReAct、Plan 顺序组成 8 个路线槽位；loader 即使看到重新计算后的 manifest digest，也会再核对这四个固定 ID 和顺序。

2026-10-19 的 smoke/full 固定清单预检均通过，包括本机固定 EnterpriseOps 镜像检查；
分别是 24/286 个任务、48/572 个 route slot，首波子集为 4 个任务、8 个 route slot，报告为 `preflight_passed_no_execution`，
模型调用 0、MCP 调用 0、未创建 ledger。证据保存在
`evals/benchmarks/evidence/public-evaluation-dry-run-smoke-20261019.json` 与
`public-evaluation-dry-run-full-20261019.json`。这只证明冻结输入可被预检读取，不是冒烟
执行、官方评分或 benchmark 得分。Jev 自然语义候选尚未独立审核，故保持 N/A；不得为
增加样本而构造非自然 Jev 判断。

本轮离线 ledger/route/profile/inventory 定向测试 46 项通过，Ruff 通过。10.8 的冻结
profile 和计量字段、10.10 的持久资源账本和 fail-closed dry-run 已部分实现；它们尚未
与真实 provider callback、三路线试次调度、τ 用户模拟/裁判及官方评分/清理执行链打通，
因此仍不满足 9.6、10.3–10.6、10.8、10.10 的完整验收门槛。

### 2026-10-19 公开评测续行门禁与首批计划

10.9 的 routed readiness 于 2026-10-19 通过，允许进入离线公开执行准备；它不代表公开任务已经执行。首批固定任务为 CSM `task_20260106_161859_265_00364ece_0e836ce8`、ITSM `task_20260115_201951_323_391f1eff_659b9dae`、τ retail train `0`、τ telecom train `[mms_issue]airplane_mode_on|bad_network_preference|bad_wifi_calling|break_apn_mms_setting|break_app_both_permissions|data_mode_off|data_usage_exceeded|unseat_sim_card|user_abroad_roaming_disabled_off[PERSONA:Hard]`。先对这四例分别执行 ReAct 与 Plan，共 8 个基础路线试次；完成四域工具、最终回复、官方评分及清理闭环后，补齐既定 24 例 smoke。若未来 manifest 版本变化，首批 ID 必须保持不变。

真实评测使用项目既有 `.decagent/business-eval/global-jev-ledger.json`，其 66 条历史 attempt 的已知支出为 `$0.007468398`、无 pending/unknown，当前余额 `$0.992531602`。公开 route 必须引用该账本并按每个 Jev request 原子预留；不得以新目录、新 mode 或新 route 初始化第二个额度。账本缺失或身份不符时 fail closed。Jev 的任何 unknown 用量暂停该实验组所有新增 Jev 请求；Qwen unknown 只终结当前 route，并停止它的后续模型/MCP/裁判调用，其他未开始 route 可继续。unknown route 不 replay。

公开执行 ledger 使用单一活动执行者租约、heartbeat 和递增 fencing generation。lease 未过期时拒绝 resume；lease 过期后的接管必须递增 generation 并将前一执行者未决 attempt 与运行中 route 记为 unknown。每个 reserve、dispatch、settle 和状态转换都校验 generation，使旧执行者即使恢复也不能写入或再次派发。route 的 30 分钟上限由可取消 deadline 执行，不以记录 machine time 代替实际中止。

后续 profile 新版本固定角色实际采样参数、响应/工具选择契约、timeout 和 provider routing，并标记 unsupported 参数；每次 attempt 同时记录请求 profile 和可取得的 resolved provider/model。不能通过 SDK 默认值隐式补齐实验配置。旧 v4 manifest 与 dry-run evidence 保留为历史，版本化 profile/manifest 使用新文件名，任何模型或路由改变均形成新的实验组。

全局实验还需独立总请求/Token/费用/墙钟限制，分别覆盖 Agent、用户模拟器和裁判；每试次的 200 请求、200 工具、100 万 token 和 30 分钟上限仍是局部上限。该总批次政策必须在首次真实派发前固定并写入新 manifest；未固定时仅允许 dry-run。不得挪用 Qwen readiness 的请求额度或 Jev 余额之外的其他授权。

### 2026-10-19 首批 Stage 1 实施进度

`GlobalJevLedger` 提供规范路径校验和只读打开入口，拒绝替代路径/符号链接且不自动初始化；辅助业务评测 CLI 在 `ledger-init` 前校验路径。公开试次 SQLite ledger 已完成共享 Jev reservation 关联与未知费用保留。公开试次 runner 仍只允许 dry-run，因此完整批次执行前仍须把规范的既有账本打开逻辑接入 live 调度入口，并完成 Agent/模拟器/裁判批次额度。v5 smoke/full 预检证据为 `public-evaluation-dry-run-smoke-v5-20261019.json` 与 `public-evaluation-dry-run-full-v5-20261019.json`；四个相关确定性测试文件 `105 passed`，profile factory 与全局 Jev ledger 结算身份有回归覆盖。Qwen/DeepSeek 受控路由另有 `34 passed`，临时 PostgreSQL 上 `test_conditional_model_budget.py` 为 `28 passed`；测试容器已停止并自动删除。

公开 trial ledger 的 execution lease 已具备心跳、租约过期原子接管、递增 generation 与旧 owner fencing。相关确定性测试覆盖活跃 owner 不能被抢占、过期后旧 owner 不能再开始 route 或 dispatch、unknown route 不 replay。租约相关安全项可以单独验收；正式 benchmark 执行仍被模型 callback、官方计分、全局额度及 cleanup 门禁阻止。

### 2026-10-20 PlanRuntime 工具调用账本适配

`ControlledRoutes` 的 session-scoped `gateway_factory` 现在可把受控路由实际调用的工具包装为 `LedgeredToolGateway`。工具发出前先建立资源 reservation 并通过 dispatch admission；若本地门禁在调用上游前拒绝，该 reservation 以 `not_sent` 释放。上游返回后 settle 的只有注册命令、参数摘要、结果摘要和耗时；派发后异常保守标记 route/attempt 为 unknown，后续不 replay。定向测试验证 wrapper 接入生产 PlanRuntime、成功账本摘要、未注册工具不触达上游、派发前拒绝不调用上游和 unknown 防重放。

这只完成工具侧 ledger adapter 的离线接线，不构成真实 benchmark runner。2026-10-20 后续切片另外实现了单请求 `LedgeredModelCallback`，负责冻结 profile 校验、模型资源 reserve/dispatch/settle/unknown、逐次实际费用来源和有限 timeout 传递；它尚未由统一 runner 用于规划、ReAct、语义判断、τ 用户模拟器、裁判和总结等全部角色。Qwen/Jev unknown 的跨路线调度策略、官方评分、完整 cleanup 和四域真实 callbacks 仍未集成，`--execute` 继续 fail closed；9.6.F、10.10.D、10.10.E 保持待办。

另将公开账本中 Qwen primary/user-simulator 的测试结算样例从 `Decimal("0")` 改为 `None`，并检查 token 用量可以完整而费用完整性为 false。这样测试夹具不会把未知费率误表达为零费用；没有新的 Qwen 或 Jev 请求。

### 2026-10-20 单次模型派发的资源账本适配

`LedgeredModelCallback` 是可组合的单次模型 callback，不是路线调度器。调用前由适配器计算完整模型可见输入的保守 token 上界，核对冻结 role/profile，原子预留 route 与批次请求/Token/费用，再记录 dispatch；只有能证明 provider 请求未发送的本地 preflight 错误可释放 reservation。发送后响应缺用量、网络异常或结构不完整即记为 unknown，当前路线停止且不得交给官方 scorer。已知 usage 的费用记录来源为供应商实际账单或冻结 profile 费率；provider billed 金额按原值记录，不能用券抵扣改成零。根据用户批准的首波请求/Token/墙钟硬上限，Qwen primary、user simulator 和 judge 可在费用预留为 null 时执行；若返回不含可靠费用，则仅结算已知 Token、费用保留 unknown，并禁止成本比较。此例外不覆盖 Jev：Jev 必须以已知预留接入原累计 `$1` 账本，未知费用时停止所有新 Jev 请求。

每次调用将 profile timeout、持久 route machine-time deadline 和批次 deadline 的最小值传给 callback。`PrimaryModel.respond(timeout_seconds=...)` 把该值应用到 OpenAI-compatible 同步 HTTP transport 并在请求后恢复客户端原 timeout。τ bridge 在同步 `parent_call` 前计算剩余时间，并将其传给接受 `timeout_seconds` 的受控 callback；`_tau_model_callback`、`ObservedUsage` 与 `LedgeredModelCallback` 继续传递并与 profile/route/batch 上限取最小值。这样真实受控模型请求会在 τ 请求截止前超时返回；τ bridge 随后关闭自己拥有的 worker 子进程树。该路径不强制中断忽略 timeout 参数的任意同步 callback，统一外层 supervisor 仍不能取消正在阻塞的整条 route，也没有覆盖工具传输的通用取消接口。机器时间起止时间保存在公开 ledger；完整批次 route/phase supervisor 尚未实现（单路线受控入口见下文）。

公开 `machine_time_attempts.started_at_utc` 记录 route dispatch/隔离环境启动，`agent_started_at_utc` 记录 Agent 回路的真实起点。环境初始化和 τ 初始用户模拟完成后、进入 `ControlledRoutes` 的 Agent callback 之前，runner 通过 `on_agent_start` hook 调用 `mark_machine_time_agent_started()`；该 ledger 操作只允许对 dispatched reservation 首次执行，并将持久化 deadline 重新锚定到此时间。这样环境准备仍消耗批次墙钟，但不消耗每 route 的 Agent machine-time 上限。重复 hook 不会移动起点或续期。`ControlledRoutes` 只提供 hook 顺序边界，当前尚无统一 runner 负责调用该 ledger method，也未证明任意阻塞工具或 callback 都可被外层 deadline 强制取消；这些限制不改变 `--execute` fail-closed 门槛。

`LedgeredModelAdapterFactory` 将可注入的 planner、ReAct、semantic、summarizer、τ user simulator 和 τ official judge 单请求 callback 按路线接到 `ControlledRoutes.model_adapter_factory`。它通过冻结 manifest trial-ID resolver 选取 ledger route，核对各角色 profile digest，并用每种 callback 的完整输入 token 上界创建 `LedgeredModelCallback`。τ route 同时返回冻结的 user/judge model identifiers；EnterpriseOps response-check judge 仍由 scorer 自己的预算适配端口管理。该 factory 只包装调用者提供的 callback，不构造 endpoint 客户端、不验证调用者回调内部没有隐藏重试，也不负责资源生命周期；集成 runner 仍须确保 callback factory 仅创建客户端、每次回调只发一个请求、结束时关闭客户端。

### 2026-10-20 单路线受控派发入口

新增 `execute_public_route()` 作为 per-route supervisor primitive，仅接受首波 ReAct 与 Plan。它在 claim 前验证项目规范 OS 生命周期锁、规范 smoke ledger 和同一 trial-bound `LedgeredModelAdapterFactory`；直接注入 planner/ReAct/semantic 等 callback 会在 route 状态变更前被拒绝。传给 `ControlledRoutes` 的 gateway factory 必须返回绑定相同 ledger、trial ID 与 route 的 `LedgeredToolGateway`。`start_route_with_machine_time()` 在一个 SQLite `BEGIN IMMEDIATE` 事务中完成 route claim、批次准入、route time-cap 检查与机时 reservation；额度不足则整体回滚，保留 pending route。成功后入口再按 dispatch → Agent-start hook → run → settle → finish 顺序执行；依据 `ExecutionResult` 只保存摘要，不持久化业务工具明文。

未知模型/工具路线仍保持 unknown，只允许补 cleanup；完成路线必须同时具备受信官方分数、成功证据与 verified cleanup。机器时间结算现在支持 supervisor 延迟终态：route 超限先记录 `settled_overrun`，由受控入口以 `resource_overrun` 失败终态保存已有 score 与 cleanup，而不误报 completed。旧直接 ledger 调用默认仍立即把 route 标为失败并抛出超限错误。

该单路线入口当时尚无批次 phase scheduler；后续已增加仅限首波的固定顺序 phase scheduler（见下文）。当前仍无可用 live CLI/provider callback 或 benchmark session/environment factory；phase scheduler 依赖调用方提供隔离试次与 ledger-bound callback factories，也未实现整条 route 的通用强取消。四域 live 闭环及 `--execute` 仍未通过门禁。确定性回归验证了适配器错绑/绕过拒绝、超限与 unknown 保留；不得据此宣称已经派发公开任务。

模型 adapter map 现实现为 dict-compatible `PublicModelAdapters`，保存唯一 callback client 资源并在 `ControlledRoutes` 的官方评分及 scoring usage 读取结束后逆序、幂等关闭。关闭失败被标为 `cleanup_failure`，业务评分证据仍保留，但不得将路线记作成功。callback factory 在构造阶段失败时尽最大努力关闭已创建资源。它只管理 callback 暴露的 `close()` 资源；无法观察或管理闭包内未暴露的隐藏 client，也不代表真实 provider client 已接通。

该切片的确定性证据包括模型 attempt reserve/dispatch/settle、role factory 将 ReAct callback 接到受控路线、用量 unknown 不评分、冻结 profile drift、Qwen known-cost 与 unknown-cost 账本路径、Jev unknown-price 发送前阻断、timeout 上界和评分后 callback 资源关闭/清理失败分类测试；PostgreSQL + 认证 loopback MCP + PlanRuntime 独立集成为 2 passed，但它不覆盖公开模型 provider，也不是 benchmark 路线成绩。该切片没有新增 Qwen、DeepSeek 或 Jev 请求；正式 benchmark 仍为 0/286，live runner 和 `--execute` 保持关闭。

### 2026-10-20 首波固定 phase 调度

新增 `execute_public_phase()`，只接受已经冻结的 smoke `first-wave`。它要求规范 supervisor OS lock、规范首波 ledger、四个固定 trial ID、ReAct/Plan 路线顺序及 durable batch snapshot；按 CSM、ITSM、τ retail、τ telecom 和每例 ReAct、Plan 顺序执行，只向单路线入口交付 pending slot。completed、failed、unknown、scoring_error、not_applicable 等终态均记为 skipped；未知路线不会重放，Qwen unknown 结束当前路线后仍可处理其他 pending 路线。批次已有持久 stop reason 时不再加载环境或构造 route dependencies，返回 8 槽位中的已执行/跳过情况。非终态旧 route 会报错并要求先走 ledger recovery，phase scheduler 不自行猜测或接管。

该函数仍要求调用方显式提供隔离 trial factory 和 ledger-bound route dependency factory；尚未接入 provider client/session factory、CLI、真实四域环境，也没有 phase-wide 可取消监督。测试只使用脚本化 route 回调验证顺序、未知隔离、pending resume 和既有 batch stop 行为，不会发模型或 MCP 请求。`remaining-smoke`、`full` 的 live phase 仍未实现，`--execute` 继续 fail closed。

模型身份落账遵守 requested/resolved 分离：profile fingerprint 是请求身份；provider 响应中的模型名属于 resolved identity。若 provider 没返回 resolved model，`ModelReply.model` 与 `model_attempts.actual_model` 都保留 null，RouteRun 不伪造 actual model 项；若实际名称存在且与冻结 profile 不同，仍先保存本次已发生的 usage/identity，再通过唯一账本写入 `model_profile_drift` 持久停止原因，阻止同批后续路线派发。profile、endpoint、参数和 role identity 继续按冻结值校验。相关确定性回归包括 `test_missing_upstream_model_identity_is_recorded_as_null_not_profile_identity` 与批次漂移停止测试，不会调用外部模型。

### 2026-10-20 原始计费证据字段

`ProviderBillingEvidence` 是单次模型回复可选携带的、逐请求供应商账单证据。公开 attempt 账本分别保存 gross API fee、币种、供应商明确返回的 voucher credit 和 cash paid、证据来源与供应商记录 ID；字段缺失时保留 null，不以账户余额反推，也不把账户或时间窗口总额分摊到路线。USD 原始 gross 与 `actual_cost_usd` 必须一致，因此券只解释谁承担账单，不会降低被比较的模型 API 成本。非 USD 原金额保留，但没有可核验汇率时 USD 成本仍 unknown。`billing_snapshot()` 按原币种汇总路线已有证据并报告缺失项，不合并不同币种；`live_public_runner` 已将该快照接入逐路线报告。Qwen 账单查询和时间窗对账还未接入，实际 Qwen 成本仍未核实。

### 2026-10-20 可恢复的官方评分摘要

route ledger 将评分状态与业务终态、cleanup 状态分开保存。为支持中断后生成诚实的路线报告，新增 `score_evidence_json`，只接受来源固定的最小结构：官方 benchmark 来源、0–1 score、由官方完整分数派生的 success 标记、可测的禁止副作用计数。评分摘要必须与 `score_digest` 相符，并按冻结任务绑定 EnterpriseOps 原始 verifier 或 τ 原始 evaluator；verifier SQL、gold、目标状态和评分文本不进入摘要。任何 route 上的 Agent 模型输入仍只由 `ControlledRoutes` 的公开上下文构造，不读取 ledger 私有评分列。新创建的 scored route 必须携带该摘要；旧 v6 SQLite ledger 通过给 route 表追加 nullable 列迁移，旧记录继续保持摘要缺失，不回填猜测数据。unknown route 仍不能被评分或改写，cleanup 补录不改变业务终态。

该切片只增加 crash-safe 评分摘要存储与读取，不接通 live route runner，也不构成任何公开任务的官方评分。报告尚未生成前，不得从 route status 或 digest 推算成功率。

### 2026-10-20 Route 与工具 deadline 传递

`execute_public_route()` 计算 route machine-time 与 batch wall-time 的最小剩余时间，并通过 `ControlledRoutes` deadline provider 传给 session。`LedgeredToolGateway` 在 reservation 前、reservation 后与 upstream 调用前检查截止时间；若 reservation 后、dispatch 前已过期，记作 verified `not_sent`；若已 dispatch 后超时或出错，则保守记为 unknown，路线不会继续执行或调用官方 scorer。HTTP EnterpriseOps environment 将有效值作为请求级 HTTPX timeout；τ session 向 MCP ClientSession 传入 `read_timeout_seconds`，并将同一上限传至隔离 τ 子进程 bridge。bridge 到期会关闭其拥有的 worker 子进程树。Tool gateway 缺少 timeout-aware 调用签名时会在公开执行入口校验前被拒绝。

相关定向回归为 `191 passed, 11 skipped`，包含 expired-before-dispatch、dispatch 后截止、HTTP timeout、MCP read timeout 和 τ bridge 子进程回收。该实现传递并执行传输层 timeout，但没有通用机制强杀任意忽略 deadline 的 Python 同步 callback；live provider/session factory 和批次 CLI 尚未接通，四域官方评分/cleanup 也未运行。因此这只是取消路径的工程切片，不能据此打开 `--execute` 或声称真实公开评测已闭环。

后续计时复核修正了一个边界：`route_remaining_seconds()` 在 machine-time reservation 已 dispatch、但 Agent-start hook 尚未运行时，返回完整 `reserved_seconds`，避免容器准备和 τ 初始用户回合递减 Agent 专属额度；batch 剩余墙钟仍作为独立最小值约束这些准备期 callback。Agent-start hook 持久化后，剩余时间才由其 deadline 计算。新测试先失败（过期 deadline 返回 0，而期望仍有 30 秒保留额度），修复后账本、路线和 phase scheduler 套件 `129 passed`；完整 `evals/deterministic` 为 `1850 passed, 14 skipped (286.53s)`。这验证计时与 adapter 边界，不验证 provider/session factories 或真实评测。

使用临时 PostgreSQL 17 实例运行 `evals/integration/test_business_route_execution.py` 为 `2 passed (21.20s)`，证明 production PlanRuntime 对接应用 PostgreSQL 与 loopback MCP 的目录读取集成仍通过。它不覆盖公开评测 runner/provider/scorer，也不构成公开 benchmark 分数。专用临时容器在测试后停止并删除；已有容器未更改。

首波 phase scheduler 的脚本化回归为 `12 passed`，包含真实 SQLite ledger 关闭并恢复后跳过 8 条已完成路线；加入该调度器后全量确定性 suite 为 `1837 passed, 14 skipped (268.23s)`，全仓 Ruff 和 OpenSpec strict（6 changes）通过。测试没有触发模型/MCP 请求；仅验证调度控制，不代表 benchmark 执行成绩。

### 2026-10-20 τ 步数账本接入

τ 的 200 步限制现已独立进入 `PublicTrialLedger`：每个 role step 在进入模拟器回调或工具调用前写入 `tau_step_attempts`，有独立序号，资源快照、route receipt 与 `RouteRun` 均分别报告该计数。账本恢复不清零，unknown route 不重放但仍可读取已记录步数；到达上限时在后续模型/工具/模拟器副作用前停止。τ 用户一次返回多个工具调用时，批内工具仍分别作为工具调用计量，但只占一个 Environment τ step；ReAct、Plan 的外部动作、Plan 语义 `decide` 与 user handoff 分开计步。

先行回归验证了独立上限、账本关闭/恢复不重置、达到上限前无工具派发、user multi-tool batch 单个 Environment step、semantic Plan node 单独计 Agent step，以及 unknown receipt 保留持久计数。随后固定 τ-Bench `96a3ef9` 的 retail train ReAct/Plan 与 telecom train ReAct 路线通过实际隔离 worker、原始 τ 工具、用户模拟器、原始 evaluator 和 cleanup；脚本 callback 产生 5/5/7 个 τ role-handoff steps。完整闭环详见下一节，9.6.C 的 τ bridge 工程验收已完成。它不是实时模型评测、不是官方 test split 分数，也没有创建公开试次账本。四域故障注入与正式首波仍由 9.6.G 和 10.10.D/E 覆盖；`--execute` 仍关闭。

### 2026-10-20 固定 τ retail train 工程闭环

在设置 `DECAGENT_TEST_TAU_PYTHON` 指向锁定的 Python 3.12 worker 后，固定 τ train 工程 smoke 实际运行三条路线：retail ReAct、telecom ReAct、retail Plan。三条路线均执行原始 τ 环境、三次模拟用户回复及澄清、原 τ `evaluate_simulation`，并核验 session 与环境关闭；τ 步数分别为 5、5、7。模型响应和用户/裁判回调均为脚本注入，Qwen、DeepSeek、Jev 请求数为 0。新增 telecom 路线时，官方 case ID 长达 213 字符暴露 `EvaluationScope` 的 160 字符过窄上限；已为 case ID 单独扩至 256，benchmark/version 仍限 160，回归验证 213 可接受、257 被拒。锁定 worker 的评测目录及 τ bridge 完整回归共 `45 passed (100.52s)`；新路线和边界的初次回归为 `4 passed (41.42s)`。该工程 smoke 不计官方 test split 成绩或首波试次；τ Plan telecom、EnterpriseOps 真实任务、四域故障注入和正式 phase runner 仍未完成。

2026-10-20 增补 EnterpriseOps scripted route evidence：两个固定任务分别经 ReAct/Plan，共 4 条路线实际运行在摘要固定的 CSM/ITSM 镜像中。评测专用 `EvaluationCatalog` 仅对精确 image/schema digest 上已审核的 `find_product`、`list_users` 赋只读语义；不改变生产 catalog，schema 漂移或显式 write hint 均不会被放行。MCP `structuredContent` 为 null 时才从文本 content 解码 JSON，随后仍执行原 output schema 校验。四条路线均走真实 MCP 读取、原始 EnterpriseOps VerifierEngine 评分和 teardown；每条官方 task score 为 0.0/`task_failure`，这只证明脚本化执行/评分/清理闭环，不代表任务解题能力。证据位于 `evals/benchmarks/evidence/scripted-enterprise-first-wave-routes-v2-20261020.json`。定向 catalog 回归 14 passed；锁定 τ worker 上 ReAct 用户模拟/原始评分 smoke 复核 2 passed。所有 callback 为脚本注入，provider 请求为 0。

上述证据不完成 9.6.F/G 或 10.3：没有统一 live supervisor 派发、没有八条首波真实模型路线、没有 Qwen/Jev 使用量或成本数据，也没有公开基准任务得分。只要候选 SHA/CI、规范账本、真正可取消 deadline、unknown 调度、四域故障注入和评分/清理门禁未同时验收，`--execute` 仍保持 fail closed。当前没有新增规格需求；delta spec 继续规定目标验收行为，进度证据记录在 proposal/design/tasks，不以实现记录修改需求文本。

该文档切片的验证快照：全量确定性测试（新增 telecom Plan 参数化前）`1897 passed, 16 skipped (281.19s)`；新增跨域 Plan 测试 `2 passed`；Ruff 通过、OpenSpec strict `6 passed`、diff whitespace 检查通过。`uv build` 生成 wheel 与 sdist 成功，CLI 和业务 MCP server 帮助入口可运行；核心 MCP loopback/数据库集成再以独立临时 PostgreSQL 17 执行 7 个相关集成文件，`80 passed, 0 skipped (241.03s)`，证据 `evals/benchmarks/evidence/postgres-core-integration-20261020.json`。全量 integration 与核心浏览器 E2E 仍未验收；这些本地测试不能代替同候选 SHA 的 GitHub CI。

2026-10-20 τ Plan 跨域补充：在原有 retail Plan 真实工具 smoke 基础上，把同一个脚本化 Plan/用户澄清/原始 scorer 路径参数化到 telecom，分别使用实际只读工具 `get_order_details` 与 `get_customer_by_phone`。两例均成功完成、各有 3 个用户模拟回合和 7 个 τ role-handoff steps，官方评分结果与 transcript 持久化一致，session/environment cleanup verified。锁定 worker 定向测试 `2 passed (30.85s)`，机器可读证据为 `evals/benchmarks/evidence/tau-plan-cross-domain-smoke-20261020.json`。这补足四个业务域都至少有一个 scripted route 实际调用工具、经过原 scorer 并清理的工程覆盖；仍不等于首波 8 条 ledger-backed live routes，也不构成任务成功率/成本/延迟结论。

2026-10-20 Python compatibility smoke：相同三个评测目录/runner 测试在 Python 3.11.15 与 3.13.0 各 `37 passed`，详见 `evals/benchmarks/evidence/python-matrix-smoke-20261020.json`。Python 3.11 直接 editable 安装失败发生在 pytest 启动之前：项目路径含中文，3.11 site 以本机 CP936 解码 UTF-8 editable `.pth`。非 editable wheel 在 ASCII-path 临时环境通过；3.13 的 `.pth` UTF-8 读取和 repo `.venv`/`uv run` 均通过。此记录是 Windows 本地兼容性说明，不构成正式 CI 完成或远端候选验证。

### 2026-10-20 冻结试次身份绑定与 resolved provider 记录

`PublicComparisonTrial` 在不改变 `ComparisonTrial` dataclass 字段和公平性 fingerprint 的前提下附带规范 SHA-256 manifest trial ID。公开 phase scheduler 和 per-route supervisor 在 route claim 前同时核对 manifest trial ID、benchmark、版本、case ID、exposure group、resource profile、response policy 与全部角色 profile digest；身份错配在任何模型、工具或机时 reservation 前拒绝。由此 ledger execution ID 不能被调用方绑定到其他 case，也不会改变已冻结的实验配对指纹。

模型 attempt 现在只把 callback 实际返回的 provider identity 写入 `actual_provider`。请求 provider 继续保存在 profile 中；provider 没有返回实际上游身份时，attempt 的 resolved provider 保持 null，不再将 requested provider 复制为 actual provider。`test_business_public_execution.py` 与对照身份测试共 32 passed，`test_public_model_dispatch.py` 的对应单请求回归验证实际上游缺失时为 null。该实现仍不创建真实 provider callback 或公开基准试次 factory；`--execute` 保持关闭，正式评测仍为 0/286。

### 2026-10-20 Jev unknown 的跨路线 admission 证据

共享 `GlobalJevLedger` 的 pending/unknown attempt 会阻止所有后续 Jev reservation，不局限于原 route。公开 ledger regression 从一条已启动路线实际经过 `reserve_model_attempt(role="jev") → mark_model_dispatched → mark_model_unknown`，再通过另一条运行中路线调用同一 API，验证派发前拒绝且不新增第二笔本地/global attempt。route 与 Jev ledger 的状态保持 unknown/held，以保护原累计 `$1` 上限。相关试次、模型派发与 route supervisor 定向测试共 `81 passed`，全仓 Ruff、strict OpenSpec 和 `git diff --check` 通过。此证据验证账本级门禁；provider factory、真实 Jev 请求的超时/unknown 故障注入及报告仍未接通，因此不能宣称 live runner 已满足该策略。

### 2026-10-20 EnterpriseOps 原始任务 verifier 验收

9.6.B 的环境/评分闭环现在使用冻结 CSM 与 ITSM smoke 任务自身的私有 verifier，而不再只使用 `SELECT 1` readiness 查询或 synthetic scorer。每个域分别启动两次新的 digest-pinned 容器和数据库；读取并比较私有数据库指纹、确认原 MCP 工具可用，再由 pinned `EnterpriseOfficialScorer` 执行该任务原始 verifier。没有执行任务动作时，CSM 与 ITSM 在两次重置中均返回已评分的 `task_failure`（CSM 0/4、ITSM 0/2）；这证明 verifier 与错误分类链工作，不是 Agent 任务成功。评分摘要只保留 digest、数量与状态，SQL/期望值不暴露，`forbidden_side_effects` 保持 `not_measured`，每个环境 cleanup 均通过核验。`evals/integration/test_business_route_execution.py` 同时覆盖 environment setup failure、official task failure 和 scoring failure 的互斥分类。

证据文件为 `evals/benchmarks/evidence/enterpriseops-official-verifier-smoke-20261020-csm.json` 与 `enterpriseops-official-verifier-smoke-20261020-itsm.json`；本地 Docker 集成 `2 passed`，评分器与路由故障分类定向回归 `92 passed`。0 模型请求、0 业务写入；首波 ReAct/Plan、实时 provider、公开任务成功率和 `--execute` 门禁仍未验收。

### 2026-10-20 CSM 知识文章语义模板审阅

首波 CSM 任务中，用户要求依据产品和故障描述查找、引用合适知识文章；该判断依赖自然语言和真实检索候选，适合受限 `Choice` 与显式 `no_match`。用户工单数量比较、ITSM 明确字段和状态条件仍由代码处理。模板 `csm_knowledge_article@1.0.0-csm-knowledge-1` 已接入评测专用 `QuestionRegistry`、Plan 编译器、`ControlledRoutes` 和原生产 `PlanRuntime`，且只注册到精确类型的隔离 `EvaluationCatalog`。编译器要求文章候选来自一个 `retrieve_knowledge` 读节点的实际结果、查询明确使用字面量 `state=published` 且没有 `limit`；路由再从同一已验证节点记录中附加实际查询参数。模板仅比较真实响应里的已发布记录，不截断候选，不泄露隐藏 verifier 字段；成功选择后仅将已返回的整数 article ID 写入接受结果，并保持 `grants_write_authority=false`。超过 24 个候选、上下文超 16,000 字符、有限查询、无已发布候选或契约不匹配时该判断不适用。

隔离真实容器的工具目录显示上游 `retrieve_knowledge` 没有 `readOnlyHint`，且 input schema 未声明 `required`。为不把它误分类为写操作，EvaluationCatalog 仅对 pinned EnterpriseOps schema 完全匹配的该工具应用只读覆盖；明确 `readOnlyHint=false`、其他 benchmark 或 schema 漂移均不覆盖。真实 schema 注册检查通过，容器 cleanup verified，0 业务工具调用、0 模型请求。`test_business_evaluation_semantic_registry.py` 8 passed、模板构造器 6 passed、公开 Session 相关套件 35 passed；其中脚本 callback route 测试只验证受控节点与生产 PlanRuntime 接线，不是外部模型或公开任务结果。证据为 `evals/benchmarks/evidence/jev-semantic-template-audit-20261020.json`、`jev-semantic-template-registry-20261020.json`。工具响应没有总量或截断标记，所以本次候选集严格等于该次实际返回的数组，不推断未返回记录。真实 runner 对同一冻结状态和候选执行 Jev/Qwen 配对尚未运行；9.6.D 仍未完成，公开基准仍为 0/286，无 Jev 质量/性能结论。

### 2026-10-20 τ 同步模型 callback 的 deadline 契约

`_StdioChannel` 收到 τ worker 的 `parent_call` 后，现在要求父进程 callback 显式接受 `timeout_seconds`，并只以剩余 deadline 调用它；不接受该参数的 callback 在执行前被拒绝，随后当前 τ worker 被关闭，不会重放。这个边界防止无界 callback 被误认为受 deadline 约束。用户模拟器与裁判的 90/120 秒角色时限改为 `timeout_cap_seconds` 请求级参数；它与 route/batch deadline 取最小值，不再临时修改共享 channel 的默认 timeout，因此并发/后续请求不会继承其他角色的 timeout。模型适配器会把有效值继续传到 HTTP client timeout；不过 Python 不能安全强杀任意同步函数，因此 callback 若接受但故意忽略 timeout，仍可能阻塞 supervisor。跨进程 supervisor 隔离/取消尚未完成。本变更不构成完整取消保证，也不改变 OpenSpec 任务勾选状态。

新增回归先以不具备 timeout 契约的 callback 复现原有放行行为，再验证拒绝发生在 callback 调用前且 worker 退出；另验证单请求 timeout cap 会超时并关闭当前 worker，用户模拟器/裁判传入 90/120 cap 且不改共享 channel。相关 τ、公开执行、账本、模型派发及 live runner 文件合计 `127 passed, 14 skipped`。全量 `evals/deterministic` 为 `1899 passed, 17 skipped (259.61s)`；全仓 Ruff、OpenSpec strict（6 changes）和 `git diff --check` 通过。没有模型 API、benchmark ledger、业务 MCP 或评分器请求；公开 benchmark 仍为 `0/286`。

### 2026-10-20 Linux CI 冻结清单换行校验修正

GitHub Actions 的 Python 3.13 在候选 `1004f7a7c7f58addce34d4c88d4a0ff9f6fec998` 上发现 `task_inventory.json` 的原始字节 SHA 在 Linux LF checkout 与 Windows CRLF 工作树不一致，导致 87 项确定性测试在预检阶段失败。`manifest_hashing.py` 现将 source manifest 按 UTF-8 LF、任务清单按原冻结 UTF-8 CRLF 规范化后校验；manifest builder 与正式运行预检共用实现，冻结摘要、任务内容及历史证据不变。新增 builder 和 loader 的 Linux LF 模拟回归。修复后 Windows 本机全量确定性测试 `1903 passed, 17 skipped (247.80s)`，专用 PostgreSQL 集成 `604 passed, 24 skipped (1158.02s)`，全仓 Ruff 与 OpenSpec strict（6 changes）通过；临时数据库已清理。修复候选的 GitHub CI 尚待复验；无模型 API 调用、公开试次账本或业务工具调用，公开 benchmark 仍为 `0/286`。

### 2026-10-20 离线确定性测试与 live provenance 门禁分离

GitHub CI run `39435322121`（候选 `2b65a2a1afc21074385caa3874c139833e363bfb`）在 Python 3.11 的确定性 job 中有 84 项失败：若干测试把本机忽略/未跟踪的公开基准下载附件和 `.env` 当作仓库必备文件。修复在 `load_public_run_plan()` 增加显式 `check_source_files` 开关；默认 `true`，生产/真实评测入口继续校验来源附件，离线 manifest/ledger 测试显式关闭该校验，并且回归断言默认 live preflight 仍拒绝缺失附件。离线测试也显式关闭 runtime 环境校验，避免依赖本机模型配置。将 50ms lease 竞态测试改成直接将 DB lease 到期时间设为过去，消除调度时序偶发失败。

受影响 deterministic 套件复测中；本地全量 deterministic 已达到 `1904 passed, 17 skipped`，全仓 Ruff 通过。第二修复尚未提交或推送，候选 SHA 的远端 CI 待重跑。仍无模型请求、公开试次或业务工具派发，公开基准为 `0/286`。

### 2026-10-20 CI 第三轮边界修正与 Runner 状态核验

候选 `60ea6f6dce5855cb078c4796abeaa3708d988247` 的 GitHub CI run `39438714094` 中，Python 3.11 deterministic `3 failed, 1901 passed, 17 skipped`；Python 3.13 因 matrix `fail-fast` 被取消。失败由 dry-run 默认读取 live `.env`（1 项）和 `tau_process.PinnedTauRuntime` 对 frozen inventory 使用原始字节 SHA（2 项）引起。dry-run 现不加载 live runtime 或外部下载资产；live `--execute` 仍显式启用 runtime、source、image、readiness 门禁。τ process 与 `verify_tau_runtime.py` 改为共享 `task_inventory_sha256()`，保留冻结清单摘要并接受 GitHub Linux LF checkout。新增回归先失败后修复；受影响套件 `91 passed, 13 skipped`，tau-only verifier CLI 的独立解释器回归 `1 passed`，全量 deterministic `1906 passed, 18 skipped (224.22s)`，Ruff 与 OpenSpec strict 均通过。pinned τ 解释器另完成 `verify_tau_runtime.py` 80/80 初始化核验；全程 0 模型请求。没有公开路线、benchmark MCP 工具或正式评分派发。

部署边界只读核验：GitHub API 当前返回该仓库 self-hosted runners 数量为 0；本机未发现 Actions Runner 服务或进程；默认分支 release workflow 仅依赖 `ubuntu-latest` 发布 GHCR，不存在 public workflow 调度本机 Runner 的 job。现有 `decagent-api`、worker、broker、proxy、Postgres 容器保持运行。故不需要注销/停用任何 Runner，也没有为满足计划而停服务；私有 Runner/CD 迁移仍属后续部署工作。

### 2026-10-20 固定首波 live 派发与未闭环结果

候选 `9bc3ee6654e46630c9ae8238a28eb6e34a088698` 的 GitHub CI run `39442012550` 全部 job 成功后，`run_public_trials.py --mode smoke --phase first-wave --execute` 通过实时 readiness、环境和工具门禁，在规范 v7 ledger 中按冻结顺序调度 4 个案例 × ReAct/Plan。所有 8 个路线槽现为终态，phase 调度器没有重放其中任何 unknown/failure route。只读检查证据见 `evals/benchmarks/evidence/public-first-wave-live-20261020.json` 及其 summary。

结果：CSM ReAct unknown (`tool_dispatch_error`)，CSM Plan failed (`agent_failure`)，ITSM ReAct unknown (`model_dispatch_error`)，ITSM Plan failed (`agent_failure`)，τ retail ReAct/Plan 均 unknown (`model_dispatch_error`)，τ telecom ReAct/Plan 均 failed (`environment_failure`)。8/8 session cleanup verified；所有路线均未调用官方 scorer，故没有 task score/result digest。批次保留的 40 个其余 smoke route 仍 pending，运行到此按计划停止。

计量账本有 4 条 Qwen attempt：CSM ReAct primary settled 6,821 input + 131 output tokens，另 3 条用量 unknown；primary/τ simulator 各登记 2 条。唯一 settled attempt 的 API cost unknown，且没有任何 bill/voucher/cash record。Token 预留不能作为实际用量；优惠券也不等于 `$0` 供应商费用。没有 Jev 或 judge attempt。CSM ReAct 的一个工具 call `find_user` 在工具调用账本中为 unknown，effect=`write`；未记录工具结果，不能推断是否有业务副作用。cleanup verified 不等价于 effect absence verified。

当前错误账本只保存粗粒度 `error_category`，没有 exception type、dispatch substage 或 sanitized transport classification。因此 `model_dispatch_error` 等不能说明请求是否到达上游，也不能据现有事实判断是网络、配置还是适配器失败。后续实验开始前应在离线切片中补足最小安全诊断字段与故障注入测试；保留旧 attempt unknown/预算预留，禁止同账本重试。

该次执行已证明 candidate gate、supervisor/ledger route admission、真实 callback/provider 进入尝试后的 usage handling、unknown/failure 终止和 session cleanup 至少有实际路径被调用；它未验证 official scoring、任务完成、成功率、成本或 Jev 优势。运行结束时 stdout 报告序列化因 `Decimal` 抛出 `TypeError`，但 JSON 证据在异常前已成功写入。现已加 Decimal regression 并让 CLI 复用报告序列化器；没有重跑试次。9.6.F/G、10.3/10.4、10.10.D/E 均保持待办。

### 2026-10-20 Safe diagnostics after first-wave unknowns

First-wave report v1 and the canonical ledger were not rewritten, and no unknown route or tool/model attempt was replayed. An additive nullable migration now adds `failure_stage` and sanitized `exception_type` to model/tool attempts and route lifecycle, plus bounded HTTP status to model/tool attempts. Failure stages come from a finite registry; exception messages, request/response bodies, tool arguments/results, and credentials are never persisted in these diagnostic columns. Attempt snapshot methods expose only safe IDs, registered command/effect names, digests, measured usage/cost, status, safe error category, allowlisted failure stage, exception class, HTTP status, and duration.

`LedgeredModelCallback` records whether unknown usage arose in the model callback or response validation. `LedgeredToolGateway` distinguishes a deadline failure after the durable dispatch marker from an exception raised by the upstream gateway; both remain unknown and non-replayable, while the diagnostic stage identifies the observed boundary. `ControlledRoutes` propagates safe stages for setup, Agent execution, official scoring, model-resource cleanup, and environment cleanup. `execute_public_route()` preserves attempt-level diagnostics when it appends cleanup evidence to an unknown route. New live reports use schema v2 and include whitelisted per-attempt snapshots; existing v1 evidence remains immutable.

Verification: focused model dispatch, public ledger, public route, live runner, and diagnostic suites pass (`167 passed`). The full deterministic suite passes (`1911 passed, 18 skipped`); Ruff and strict OpenSpec validation pass. The regression specifically checks that a timeout stores `TimeoutError` and the stage without the exception message, tool writes stay unknown and non-replayable, prior unknown diagnostics survive cleanup amendment, old ledger migration adds nullable columns without changing the cohort, and the report contains only whitelisted attempt metadata. This improves diagnosis for a future separately authorized cohort; it does not repair or retry the 2026-10-20 attempts, and it does not open the live gate.

### 2026-10-21 Diagnostics for settled response-contract failures

`LedgeredModelCallback` now also persists the allowlisted `model_response_validation` stage and sanitized exception class when a provider response includes complete usage but the caller rejects its plan/action contract. The cost and token usage still settle normally; exception text, prompt, and response content remain excluded. Existing ledger rows are not backfilled or rewritten.

Verification: the new real-SQLite regression first failed because the settled attempt had no failure stage, then passed after the ledger adapter and `settle_model_attempt()` persisted the existing nullable diagnostic columns. Focused public model dispatch and ledger suites pass (`84 passed`); full deterministic suite passes (`1970 passed, 18 skipped`); Ruff passes. No provider requests were made. This records future response failures more precisely without changing historical v8 route outcomes or enabling a live batch.

### 2026-10-21 评测候选身份与历史 cohort 边界

已运行过的 v7 首波 cohort 没有持久化 candidate SHA，且包含模型 attempt 和终态 route。不得把当前 commit、其 CI 记录或后生成的 report 倒推为历史试次候选。新实现要求空白且合法的 ledger 在任何 route claim 前绑定一个通过同 SHA GitHub CI 的候选；恢复、dispatch 和 report 必须验证同一绑定。已有活动的无 SHA ledger 不可补绑；损坏 metadata 不可绕过校验。此保护只允许将来的新 cohort 正确归因，不修复历史结果，也不重发其 unknown/failed routes。新 cohort、预算和派发批次需要重新冻结后才能开始真实评测。

### 2026-10-21 历史 v6 迁移测试夹具复核

候选 CI `39474267980` 发现 `test_business_identity_migration.py` 在 v6 schema fixture 中调用依赖后续 `workspace_id` 列的当前 `TicketStore`。测试夹具改为直接向 v6 的 people/category 表写入历史行，不改迁移实现。相关 PostgreSQL migration tests `3 passed`；全量 integration 及修复后精确 SHA 的 GitHub CI 尚待复验。该复核不改变运行时设计；selective-business-evaluation spec 已包含 candidate SHA 在 claim 前绑定、历史无 SHA ledger 拒绝补绑的要求，因此本次不增删规范需求。

### 2026-10-21 EnterpriseOps response-check judge 受控接线

EnterpriseOps pinned 原始 verifier 可含 response_check，其 judge 现在由公开模型适配器按 judge role 创建，并通过当前 route 的 LedgeredModelCallback 进行 profile 校验、预算预留、单次 dispatch 和 settle/unknown。EvaluationPlanSession.requires_official_judge 对所有 Enterprise response_check 启用账本绑定；ControlledRoutes 在评分前绑定 callback。若 scorer 已注入客户端，受控 session 明确拒绝，避免绕过 route 账本；EnterpriseOfficialScorer 继续运行原始 verifier。消息适配只接受有界文本 role/content，不暴露 verifier 结果给 Agent。bridge 从当前 route 的 attempt snapshot 读取真实请求、prompt/completion token、费用来源和时间；价格未知时返回 null，未结算 attempt 不能形成分数。τ 的官方模拟器/裁判路径保持原状。

受控验证包含：公开 factory 为 Enterprise response-check 绑定冻结 judge profile；受控 ReAct route 将 callback 接到原始 verifier；费用 unknown 保留已知 usage；unknown dispatch 阻断评分。相关 route/model/scorer/runner 回归 90 passed，Ruff 与 git diff --check 通过。没有模型、MCP、账单或公开评分派发。此代码仍未进入精确候选 CI；取消任意忽略 deadline 的同步调用、worker/监督者分离、新候选 cohort 和四域真实评分/cleanup 仍是 live gate。

### 2026-10-21 一次性 provider worker 与接管保护

受控 first-wave model adapter 的 provider HTTP 请求现在通过 `run_cancellable_process()` 进入一次性 spawned 子进程。账本/预算 reserve 与 `mark_model_dispatched()` 仍由监督进程完成；子进程只收到当前请求、冻结 profile、当前模型凭证、工具 schema 与有效 timeout，在进程内创建并关闭一个 `PrimaryModel`，返回有界 JSON 响应。规划/工具结果校验和 attempt settle 仍回到监督进程；子进程不持有 route ledger，也不复用 provider client。子进程不会接收其他角色的 Jev/fallback secrets。

一次性 worker 先取得规范 `.decagent/business-eval/public-worker.lock`，并检查启动它的监督进程仍存活；监督进程取得规范主锁后，也会尝试取得并释放该 worker guard，若 guard 仍被旧请求持有则拒绝开 ledger。这样在旧 supervisor 异常退出而 provider 子进程尚未回收的窗口，新进程不能与旧请求重叠。Windows worker 被放入 `KILL_ON_JOB_CLOSE` Job Object，POSIX worker 建立独立 session；deadline 由父监督进程触发整个拥有的进程树终止。POSIX 终止会在 worker sentinel 就绪后、reap leader 前再次发送 group kill，并在 Linux `/proc` 中确认组内无活动进程；zombie 不再执行用户代码，不阻塞 drain。活动成员超过 2 秒仍未退出时报告 unresolved tree error，不得宣称终止完整。父进程退出后，worker 的 monitor 会结束自身。

此切片不等于完整 route worker/supervisor 架构：`ControlledRoutes`、session、路由状态和 route ledger 操作仍在监督进程同步执行；一般业务工具 callback 及任意 Python 阻塞同步 callback 仍须分别遵循自己的 timeout-aware 子进程/传输契约。Jev semantic path 由专用 Decisions worker 执行，primary semantic 对照由 `OneShotPrimaryModel` 的请求级 worker 执行；它们都没有把完整 route/session 移入 worker。若真实执行中收到 timeout，现有 ledger 仍将该次已派发 attempt 和 route 保留为 unknown，不自动重试。新设计要求已加入 selective-business-evaluation delta spec；首波真实执行和所有新模型 dispatch 仍关闭。

验证覆盖结果帧、单角色凭证传递、父进程退出后不执行 provider target、超时后 worker/后代进程回收、模型 attempt 记为 unknown 且不可重放，以及释放主锁时仍存活的 worker 阻止 supervisor takeover。一次主切片的全量 deterministic 为 `1929 passed, 18 skipped`；随后移除 facade 中未使用的额外 secrets 字段并增加 parent-exit 回归，最终公开评测相关 deterministic 文件 `140 passed`，全仓 Ruff 与 OpenSpec strict `6 passed, 0 failed` 通过。该证据仅验证隔离与锁机制，不代表 Qwen、Jev、工具或官方评分器真实调用已成功，也不改变历史 8 条路线结果。

### 2026-10-21 Candidate-scoped canonical ledger

The historical `public-smoke-v7.sqlite3` path remains available only for compatibility and history inspection. A new first-wave execution derives its canonical path from both the frozen cohort digest and the CI-verified candidate SHA, under `.decagent/business-eval/public-trials/cohorts/`. This makes same-candidate resume idempotent while different candidates cannot overwrite, inherit, or erase old route attempts. The shared canonical Jev ledger remains global, so candidate scoping cannot reset Jev's cumulative budget. Arbitrary path overrides and symlinked canonical components fail closed. The old v7 ledger is not migrated or modified by this path change.

The path and dispatcher regressions cover candidate/cohort separation, same-candidate resume, preserved legacy data, and rejection of mismatched paths. The four affected deterministic files pass (`88 passed`). This is a ledger identity/isolation change only: it does not create benchmark trials or authorize model, Jev, tool, or scorer dispatch. Live execution remains closed; full route-worker separation and remaining cancellation gaps are still outstanding.

### 2026-10-21 Route deadline propagation through agent callbacks

`ControlledRoutes` now evaluates the persisted route/batch deadline immediately before planner, ReAct, semantic-decision, and final-summary calls, then passes the remaining seconds through `ObservedUsage.call_reply()`. A deadline-bound callback must accept `timeout_seconds`; the existing ledgered adapter intersects it with the frozen model profile timeout and its own ledger deadline before the transport call. τ user-simulator/judge and business-tool calls continue to use their existing session and bridge deadline paths. This closes the gap where a replacement callback could receive a deadline-bound route but be invoked without its timeout contract.

Regression coverage exercises planner, ReAct, Jev semantic, and final-summary callback deadlines; the four affected deterministic files pass (`122 passed`), the full deterministic suite passes (`1939 passed, 18 skipped, 232.78s`), and Ruff passes. This does not establish hard cancellation for arbitrary synchronous tool callbacks, provider worker separation for the full route, or four-domain live scorer/cleanup completion. No model, Jev, business tool, or official scorer request was dispatched; the live gate remains closed.

### 2026-10-21 Qwen CNY rate profile and per-request cost reporting

The Qwen rate source supplied by the owner applies to the configured `https://maas.qianwenaiapi.com/compatible-mode/v1` endpoint. A new profile schema v4 freezes the public standard realtime `qwen3.8-flash` rates in CNY: input ¥0.8/M tokens, output ¥2.7/M, and cached input ¥0.1/M. The source URL, observation version, currency and rates enter each Qwen role profile fingerprint. New public manifest v8 binds those profiles to the same frozen 206+80 denominator and 4-task first-wave identity. The old v3 profile and v7 manifest remain readable and unchanged; the Jev profile fingerprint and global USD ledger remain unchanged.

The evaluator uses the rate only as a frozen calculated gross API cost. If response usage provides cache-hit tokens, those are priced at the cache rate; if that count is absent, input is charged at the ordinary input rate and the basis is `frozen_rate_upper_bound`. The Qwen OpenAI-compatible response documents `usage.prompt_tokens_details.cached_tokens`; installed LangChain OpenAI normalization maps it to `input_token_details.cache_read`. The parser accepts either representation and rejects conflicting counts. Per-attempt evidence records usage, cached input tokens, currency, cost source and calculation basis. `billing_snapshot()` now separates provider-invoiced gross/voucher/cash evidence from frozen-rate estimates by currency; amounts remain in CNY and are never guessed into USD. Missing per-request invoice, voucher and cash values remain null. `usage_complete` and `cost_complete` are distinct.

Verification: profile v4 legacy round-trip, manifest v8 scope, CNY reserve/settle/report, cached-token parsing, provider CNY bill/voucher separation and v3/v7 compatibility are covered by deterministic tests. No new model request, invoice lookup, or public benchmark route was executed. Manifest v8 is `pre_registered_not_run`; the historical 8-route attempt set remains untouched, and public scored results remain `0/286`. Jev-versus-Qwen cost comparison still needs a verified CNY/USD conversion rate and comparable provider-billed or frozen-rate basis.

### 2026-10-21 Primary-model semantic comparison callback

`PublicPrimarySemanticCallback` is the general-model counterpart to `PublicJevSemanticCallback` for the isolated `plan_primary_semantic_mcp` route. Both rebuild and validate the same `QuestionRegistry` definition, exact question version, state fields, candidate values, actor digest, and permission digest before dispatch. The primary callback sends only that state and question to the frozen primary profile and requests one `decagent_semantic_choice` function call with a JSON Schema parameter contract, following the [Qwen function-calling guide](https://platform.qianwenai.com/docs/developer-guides/tool-calling/function-calling). The current profile uses `tool_choice=auto`; if Qwen returns text or no tool call, that response fails the contract and is not retried. The callback requires a legal selected option, a probability value for every option and no extra option, and a distribution that passes the shared Choice contract. Code derives the primary confidence as `max(0, min(1, (p_selected - 1/n) / (1 - 1/n)))`, where `n` is the number of options; Qwen does not self-report this field. This is a deterministic transform of the model's distribution, not evidence that the distribution is calibrated to empirical correctness, so it must not be treated as a production automation threshold. Output-contract failure with measured usage is settled as a measured rejected request; absent usage or post-dispatch uncertainty remains unknown through the existing ledger wrapper. The callback does not authorize tool writes.

`LedgeredModelAdapterFactory` now requires a semantic callback for both primary and Jev comparison routes and binds each to its frozen profile. The opt-in combined factory dispatches by route; defaults remain off, and the v8 first-wave manifest is unchanged. The primary path uses `OneShotPrimaryModel` and its cancellable provider worker, while Jev uses its dedicated Decisions worker. No model, Jev, business tool, or scorer request was made. The two callback/dispatch test files pass (`37 passed`) and Ruff passes. This is offline readiness only: no paired live trial or score exists, exposure review remains open, and 9.6.D is not complete.

### 2026-10-22 Empty τ user-simulator tool catalog regression

The fixed τ retail train case 0 may present the user simulator with an explicit empty tool catalog. `_schema_only_tool_validator()` now distinguishes an absent catalog (`None`) from a supplied empty catalog (`[]`): the latter injects a validator that rejects all tool calls, satisfying the paired `tool_definitions`/validator contract without granting tools. A local HTTP loopback regression verifies the response, usage, and provider request ID with `tools=[]`; the focused callback file passes (`25 passed`) and the full deterministic suite passes (`2,025 passed, 18 skipped`).

The change is committed at `cb050a92b181cc75c7939a99d8c466a1f912c266`; same-SHA [CI run 39736719671](https://github.com/shuaibilx/DecAgent/actions/runs/39736719671) passed all jobs in about 12.1 minutes, including Python 3.11/3.13 checks, PostgreSQL integration shards, package/MCP/Compose, and Docker boundary validation. This only fixes a verified local preflight defect. The f1a4 historical routes remain unchanged, three Plan timeouts and the CSM MCP unknown remain unresolved, official live smoke has not passed, and no Jev gain is established. No additional model request was dispatched.

### 2026-10-22 Root-cause metadata for wrapped tool failures

`LedgeredToolGateway` now records the deepest safe exception class and valid HTTP status from a bounded exception chain. Traversal prefers explicit `__cause__`, then uses unsuppressed `__context__` when wrappers omit `raise ... from ...`; `raise ... from None` remains intentionally opaque. The chain is cycle-safe and capped at eight exceptions. Messages, bodies, tool arguments/results, and credentials are never read or persisted. Both the gateway failure and deadline paths retain the existing `unknown` terminal state and no-replay behavior.

Regression coverage verifies explicit and implicit wrapper chains, a valid upstream status, suppressed context, and absence of private arguments/messages in the attempt snapshot. The focused public diagnostics and tool-ledger suites pass (`67 passed`); the full deterministic suite passes (`2,028 passed, 18 skipped, 286.37s`). Ruff and strict OpenSpec validation pass. The code is committed as `ec09ccb1076f26854f99f12a9980c3b37e2ee248`; same-SHA [CI run 39741402103](https://github.com/shuaibilx/DecAgent/actions/runs/39741402103) passed all jobs, including Python 3.11/3.13 checks, PostgreSQL shards, wheel/MCP/Compose, Docker boundary, and Workbench tests. This only improves future failure attribution; it cannot reconstruct the cause hidden by the immutable f1a4 CSM attempt. No historical route was changed and no model, Jev, MCP, or scorer request was dispatched.

### 2026-10-22 Measured duration for unknown tool attempts

`LedgeredToolGateway` measures elapsed time from the durable dispatch mark through the observed transport/deadline exception and persists it with `mark_tool_unknown`. The attempt and route remain `unknown`, with no result digest and no replay; the duration is latency evidence only and does not prove a remote effect or response. Historical attempts with null duration are immutable and remain null.

The regression deliberately failed before the fix because `tool_attempts.duration_seconds` stayed null after an upstream timeout. Focused public trial/execution/route tests now pass (`150 passed`), Ruff passes, and strict OpenSpec validation passes. No external provider or MCP call was made.

### 2026-10-23 v10 fixed first-wave live results

The frozen v10 cohort ran on candidate `9d61688c52ee66435a22e18c4fa1c8348a70095a` after its same-SHA CI passed. All four fixed tasks and all eight ReAct/Plan route slots reached terminal status: 3 failed, 5 unknown, 0 succeeded; cleanup was verified for 8/8. Four Plan routes ended with a 120-second Qwen `TimeoutError`; CSM/ReAct ended unknown during MCP `find_user` dispatch with `ValidationError`. Two other ReAct routes failed with `ValueError`; τ telecom/ReAct reached the official evaluator and scored 0.0. The other seven routes were not scored. No unknown route was replayed.

The four Plan timeout attempts had conservative input-bound values from 49,890 to 62,912, but these are not provider-reported token counts. `PublicPlannerCallback.input_token_upper_bound` uses serialized UTF-8 byte length plus schema overhead. The ledger therefore has no actual provider input usage for these unknown requests, and their apparent proximity to the 65,536 profile bound does not establish that request size caused the timeouts. The attempts also had no returned HTTP status or provider request ID; only the local 120-second timeout is confirmed.

The batch used 30 Qwen requests (21 primary and 9 user-simulator), 220,086 settled tokens, 257,347 reserved token-bound units for unknown usage (input uses a UTF-8 byte upper bound, not provider usage), 13 tool calls, and 772.246 seconds of wall time. Frozen-profile cost estimates for settled responses total ¥0.1884416; the four unknown primary attempts retain ¥0.2681368 in fee reservations. The run did not record per-request provider invoice evidence, so these estimates are not a verified invoice amount. The batch stayed within its 500-request, 5,000,000-token, 1,600-tool-call and 4-hour limits. There were no Jev or LLM-judge requests.

The tool ledger contains 10 settled reads, 2 settled calls classified as write-effect by registered tool metadata (`find_configuration_items` in ITSM/ReAct and `refuel_data` in τ telecom/ReAct), and one unknown read (`find_user`). These calls ran inside isolated benchmark environments, and environment cleanup was verified for all routes. The official score evidence did not measure a forbidden-side-effect count (`forbidden_side_effects` is null); successful cleanup does not prove that no side effect occurred.

Offline journal review confirms ITSM/ReAct recorded `list_users` and `find_configuration_items` before its `ValueError`; τ retail/ReAct recorded four settled reads before its `ValueError`. Journals retain tool names, effects, statuses and digests, but not a safe exception origin line or message. The CSM `find_user` exception took about 55 ms and had neither HTTP status nor result snapshot; because dispatch was marked, it remains unknown. These records isolate the failure boundaries but do not establish the underlying causes. No attempt was replayed during this review.

Static call-path review narrows, but does not prove, the CSM failure: ReAct, `_ObservedGateway`, and `EvaluationPlanSession.call` validate the same input before `environment.call_tool`; after a response returns, `EvaluationCatalog.observe` validates the result against the pinned output schema. The registered MCP adapter uses the repository's raw HTTP/JSON-RPC client, while the persisted deepest exception class is `ValidationError`, consistent with the local `jsonschema.ValidationError` raised by result validation. The leading hypothesis is an output-schema mismatch after the read response, but the ledger omits the exact source stage and validation path, so it is not treated as confirmed. The tool attempt remains unknown and is not replayed.

This run demonstrates real supervised dispatch, timeout/unknown handling, tool-call accounting, a τ official-evaluator result and environment cleanup, but it does not satisfy the four-domain quality gate. It does not provide a successful route, a full set of domain scores, a Jev comparison or a Jev advantage claim. The 24-task smoke remainder and 206+80 full benchmark stay paused. Before another live cohort, investigate the four primary timeouts, the CSM MCP validation failure, the ReAct failures and the zero τ telecom score; preserve all terminal records and require a newly frozen candidate/cohort and separate batch authorization. The machine and human-readable reports are `evals/benchmarks/evidence/public-first-wave-live-20261023-v10-9d61688.json` and `.md`. No normative spec requirement changed in this evidence-only update; task status remains 72/79.

### 2026-10-23 Safer attribution for tool-output validation failures

Future isolated trials now tag exceptions raised while parsing or validating a returned tool result against the frozen output contract as the allowlisted `tool_output_validation` stage. `safe_failure_stage` finds the first allowlisted stage through the existing bounded, cycle-safe exception chain, and `LedgeredToolGateway` persists that stage while retaining the deepest safe exception class. Error messages, response bodies, schema paths, arguments, and credentials remain excluded. The dispatch result remains `unknown` when the remote outcome cannot be confirmed; this diagnostic label does not permit replay or alter terminal status.

Regression coverage exercises the actual output-schema failure path and wrapped-stage propagation. The focused diagnostics, ledger, session, execution, and route suites pass (`171 passed`); the full deterministic suite passes (`2,126 passed, 18 skipped, 274.88s`). Ruff and strict OpenSpec validation pass. This is prospective instrumentation only: it does not recover the missing source stage for the immutable v10 CSM attempt, prove the leading output-schema hypothesis, or complete 9.6.F. No model, Jev, MCP, or scorer request was dispatched.

### 2026-10-23 Billing evidence completeness correction

Review found that a cost-basis aggregate emitted `provider_billed: "0"` when every settled attempt used a frozen profile estimate and no provider invoice amount was recorded. The aggregate now emits `provider_billed: null` unless every cost-bearing attempt in that currency has a provider-billed basis, and separately reports the known partial amount and billed/missing attempt counts. Request-level provider invoice, voucher, and cash fields remain null when absent. The v10 machine report was corrected from its retained cost records; task outcomes, token counts, frozen-rate estimates, and unknown reservations are unchanged. This correction adds no provider billing evidence and does not turn unknown into zero.

Regression tests distinguish an absent provider invoice from a known billed amount and verify that unexpected observer runtime errors retain the generic tool stage. The focused regression tests pass; the full deterministic suite passes (`2,127 passed, 18 skipped, 280.88s`), Ruff passes, strict OpenSpec validation passes (`6/6`), and the corrected v10 JSON passes consistency checks against its route counts and recorded cost attempts. No external requests were made.

### 2026-10-23 ReAct boundary diagnostics after v10 journal review

The v10 local evaluation journals were reviewed read-only. They identify the failed routes' benchmark domains, safe tool names, statuses, digests and model-attempt counts, but do not contain exception messages, source locations, or a saved traceback. The two ReAct `ValueError` causes therefore remain unknown; the route history cannot be reconstructed by replaying model/tool calls, and no v10 status or score was changed. The CSM validation hypothesis and the ReAct causes remain separate: the CSM tool attempt is unknown, while the two ReAct routes are failed.

For future runs, `ControlledRoutes._react()` now annotates errors at the known boundary before the route receipt is written: route-boundary checks, model callbacks, response/action validation, τ user-simulator interaction, and tool gateway calls. Existing more-specific stages in a bounded exception chain take precedence, and the original exception class is preserved where possible. `PublicReactCallback` marks measured malformed actions as `model_response_validation`. Exception messages and benchmark content are still excluded from persisted diagnostics. This classification is prospective and does not prove the source of either historical `ValueError`.

TDD regressions first failed because the new boundary and callback stages were absent, then passed after the change. Focused ReAct-stage and model-dispatch tests pass (`31 passed`); the broader public-route, session, callback and evaluation selection passes (`300 passed, 14 skipped`). Ruff and strict OpenSpec validation pass (`6/6`). No model, Jev, MCP, τ simulator, or scorer request was dispatched. The full four-domain live gate remains incomplete; 9.6.F and 9.6.G stay unchecked.

Post-review correction: `PublicModelOutcomeUnknown` route receipts now retain the most-specific allowlisted stage from the bounded exception chain instead of replacing it with `model_callback`. A route-level regression verifies that a wrapped `model_worker_response` stage survives without retaining the underlying private message. The focused regression passes; the expanded affected route/public evaluation selection passes (`434 passed, 14 skipped`). Ruff and strict OpenSpec validation pass (`6/6`). The live gate remains incomplete, and no external request was made.

### 2026-10-23 Preserve safe provider phase when the supervisor kills a timed-out worker

The v10 Plan requests reached the parent's hard 120-second deadline, which killed each one-shot child before its HTTP client could return a transport exception frame. That explains why the historical ledger contains only `TimeoutError`; it does not identify why Qwen or the endpoint failed to answer. Future one-shot model requests now share a fixed-size multiprocessing progress record containing only an allowlisted stage code and optional HTTP status. A single packed raw integer keeps deadline observation lock-free, so the parent cannot block on a worker-owned mutex; HTTP request/response hooks update it, and the parent snapshots it before terminating the worker. Only an observed transport boundary (`request_pre_send` or `provider_response`) is retained for a supervisor timeout. Reaching the internal `model_call` stage alone is insufficient; the attempt records generic `model_worker_timeout` with null HTTP status. All outcomes use the existing `ModelWorkerOutcomeUnknown` path. A response status still does not establish usage or billing, and the attempt remains unknown with its reservation retained.

A loopback HTTP integration regression returns status 200 headers and deliberately stalls the body past the parent deadline. It verifies that the attempt exposes only `model_worker_provider_response`, status 200, and `TimeoutError`, while neither the test credential nor request text appears in the public exception. A second regression verifies a no-progress deadline remains at `model_worker_timeout` with null HTTP status. Both tests first failed against the prior implementation; the focused cancellable-model suite passes (`27 passed`) and touched-file Ruff passes. Full deterministic verification passes (`2,162 passed, 18 skipped`), Ruff and strict OpenSpec validation pass. Manual CI on the later deployment-boundary commit found an unrelated stale-date integration test failure on both Python versions; a clock-injection fix passes the guest-budget module locally (`36 passed`), while the full integration suite and same-SHA CI for that correction remain pending. The v10 candidate and ledger remain immutable; this change does not retry or replay any request.

**Post-review correction (2026-10-23):** A regression now starts a real one-shot child, verifies that it marked `model_call`, then lets the parent deadline terminate it. The supervisor filters that internal stage and reports generic `model_worker_timeout`; only transport-stage progress is retained. A companion persistence test verifies that a timeout after HTTP 200 headers remains an unknown model attempt and route with the safe stage/status, and cannot be replayed. Both regressions pass. The complete deterministic suite passes (`2,164 passed, 18 skipped`), the guest-budget PostgreSQL integration module passes (`36 passed`), Ruff and both strict OpenSpec validations pass. Same-SHA GitHub Actions [run 39940453829](https://github.com/shuaibilx/DecAgent/actions/runs/39940453829) for commit `14ffd1fe238306cdd858673bb786ac2c3bd581ff` passed all 9 jobs in 12m31s. No model request or benchmark route was rerun.

### 2026-10-24 One-shot provider timeout classification

The new first-wave cohort's four primary Plan attempts timed out at `request_pre_send` after the supervisor's 120-second deadline; this confirms only that the local request hook ran, not that Qwen received or billed the request. Offline call-path review found that the child kept the original full timeout even though process startup and client setup consumed part of the supervisor's hard deadline. The child now receives the same absolute monotonic deadline as the caller, computes the remaining time immediately before provider dispatch, and reserves a bounded 0.5–5 second response grace. When the SDK reports a transport timeout or connection failure, the worker returns only the allowlisted boundary, exception class, and any observed HTTP status. The caller still records missing-usage attempts as `unknown`, retains reservations, and forbids replay.

A loopback HTTP regression that sends HTTP 200 headers and stalls the body verifies that the SDK timeout is classified before supervisor termination while status 200 and the `provider_response` boundary remain observable; it does not produce usage or turn the attempt into success. The cancellable-model suite passes (`28 passed`), public model-dispatch tests pass (`30 passed`), Ruff passes, and strict OpenSpec validation passes. No Qwen, Jev, MCP, τ simulator, scorer request, or benchmark route was dispatched during this change. The patch improves timeout attribution only; it does not establish why the four live Qwen requests failed to return. A fresh frozen cohort and authorization are required to test the change against a provider.

### 2026-10-24 SOP-Agent pilot pairing and callback diagnostics

The approved pilot is a deterministic, metadata-only sample from the frozen v10 inventory. Seed `sop-pilot-20261024-v1` ranks `case_id` values by SHA-256 of `seed + NUL + case_id` and selects 10 EnterpriseOps CSM, 10 ITSM, 2 τ retail official-test, and 2 τ telecom official-test tasks. The schedule has 68 slots: ReAct and one-plan deterministic execution for all 24; CSM additionally receives a primary-semantic full-workflow route and a Jev semantic route. Selection reads only benchmark/domain/split/exposure/case ID and frozen route/profile metadata; prompts, goals, reference actions, and scoring material are not used. The pilot file has not yet been generated or frozen against a candidate SHA.

For a semantic pair, the primary route retains its freshly compiled `Plan` only in the current scheduler process. The Jev route receives that exact object through `frozen_plan`; the two route receipts must carry equal plan and decision-input digests, with scopes `full_workflow` and `execution_only`. The pair comparator rejects missing or mismatched digests. If the current process holds a completed primary receipt with no decision-input digest, the scheduled Jev slot is finalized as `not_applicable` with no environment, tool, or model dispatch. If recovery finds a terminal primary route but the in-memory Plan and decision evidence are unavailable, the Jev slot is finalized as failed/incomplete rather than inferred to be N/A; the ledger does not currently persist enough semantic metadata to distinguish a natural no-decision result from a process interruption. Neither case re-plans or dispatches a model/tool to manufacture a pair. N/A and incomplete slots remain separately visible; only N/A is excluded from evaluated-route success-rate denominators, while route coverage and incomplete counts are reported. The Plan is not persisted as a hidden prompt.

The pilot evidence reader now normalizes the original requested path without resolving it, rejects parent traversal and checks every original path component for symlinks before resolving the final target. This prevents an intermediate symlink pointing back into the ignored evidence directory from bypassing the evidence-path boundary. Regression coverage includes a deterministic symlink-resolution fixture; the local account cannot create real symlinks, so the integration-style filesystem case is skipped on this machine.

The evaluator now supports safe callback reason codes for malformed ReAct responses and τ event/payload/reply/model/content/tool-call contracts. τ worker exceptions carry only an allowlisted code, failure stage, and exception class to the supervising process. The route result digest and error category retain the diagnostic classification; error messages, request/response bodies, tool parameters, and credentials remain excluded. Existing v10 failures remain immutable and cannot be retroactively diagnosed from this instrumentation.

The CSM pinned output compatibility adjustment and the comparison/scheduler/report/pilot regressions are present in the uncommitted worktree. The affected deterministic selection passes (`219 passed, 14 skipped`) and touched-file Ruff passes. These checks do not cover the current diff in GitHub CI. No new model request, pilot manifest freeze, MCP route, τ simulator, or official judge request has run; 9.6.D/F/G and 10.3–10.6 remain incomplete.

### 2026-10-24 Diagnostic fee admission and report completeness

The bounded live diagnostic now carries a separate Qwen CNY ceiling of ¥13.50 in addition to its request, token, tool-call, role, route, and wall-clock limits. At request reservation, the public ledger atomically adds the request's frozen-profile upper bound to settled and unresolved Qwen amounts; unknown attempts retain that fee reservation. Settlement totals use the recorded CNY amount, and a measured fee overrun durably stops further batch dispatch. The batch snapshot exposes actual, reserved, total, and unknown-cost fields; the diagnostic evidence validator cross-checks those totals against per-attempt records and rejects missing or over-limit amounts. This keeps voucher offsets separate from gross API cost.

An integration regression exposed a diagnostic report defect: `execute_live_public_phase()` reused the phase-name variable for the returned phase receipt, so the live diagnostic omitted the evidence block required by the pilot gate. The implementation now keeps `phase_name` and `phase_receipt` separate. Tests cover report limits/source identity, CNY pre-dispatch reservation, unknown reservation retention, and settlement overrun stopping the remaining routes.

This is local implementation only. The current candidate has not been committed or verified by same-SHA GitHub CI; no Qwen, DeepSeek, Jev, business MCP, τ simulator, or official scorer request has been made. The diagnostic and pilot remain closed until the candidate is clean and CI-verified. The initial live diagnostic path is still Qwen-only; if it fails, a separately frozen OpenRouter diagnostic cohort must be implemented and price-checked before any DeepSeek dispatch.

The current full deterministic run passes (`2,219 passed, 18 skipped`); the focused diagnostic-gate, pilot-manifest, and live-runner regressions pass (`33 passed`). Repository Ruff and strict OpenSpec validation pass (`6/6`). These are local checks only and do not replace same-SHA CI or the bounded live diagnostic.

Budget review found that separate diagnostic and pilot route ledgers would otherwise each inherit a fresh full-wave allowance. The pilot now derives its batch ceilings from the validated diagnostic report: request, token, tool-call, Qwen gross-cost, role-specific and wall-time use (including unknown reservations) are subtracted; the elapsed diagnostic time is rounded up to whole seconds. The pilot freeze carries the diagnostic report SHA-256, candidate identity and computed remaining limits, and live execution rejects a report that differs from the frozen binding. This keeps per-route ledgers separate while enforcing one cumulative weekly ceiling; changing cohort/state paths cannot restore spent budget.

Diagnostic reports and the runtime pilot manifest are written under the repository's ignored `.decagent/evaluation/` state directory. Writing immutable evaluation state inside a tracked source directory would make the CI-verified candidate dirty before the next phase; these artifacts are hash-bound into the pilot report, and the final human-readable evidence can be copied into the versioned evidence directory after dispatch ends.

### 2026-10-24 Diagnostic dry-run and recovery evidence correction

The CLI advertised `--phase diagnostic` for offline use, but `build_dry_run_report()` rejected that phase because `_trials_for_phase()` only registered `first-wave`. The live path already narrowed the cohort to the four initial smoke cases, so the dry-run defect made the published preflight command unusable and could hide whether the intended 4-task/8-route slice was selected. A follow-up review also found that a repaired diagnostic dry-run still inherited the full smoke cohort's 500-request batch limits. Both paths now call the same bounded diagnostic-plan builder: the report selects four fixed IDs/eight route slots and reports Qwen limits of six requests, 600,000 total tokens, and 2,700 seconds (45 minutes), with zero model/MCP calls and no ledger. The deterministic test asserts those caps and the direct CLI invocation with digest-pinned image preflight succeeds.

The code review also found that a resumed Jev slot could be marked `not_applicable` solely because the primary Plan was no longer in memory. The scheduler now marks it `failed/incomplete` whenever the primary route is already terminal but no recoverable receipt proves that a natural decision was absent. Only a current-process completed receipt with no decision-input digest establishes N/A. This is intentionally conservative because the ledger currently does not persist a semantic-observation marker; it can overcount an interrupted unknown-applicability slot as incomplete, but it cannot inflate N/A coverage or pretend a semantic pair ran. The spec now states this boundary explicitly.

The diagnostic evidence reader checks parent traversal and every original lexical path component for symlinks before resolving the target, including an intermediate symlink that resolves back inside the ignored evidence directory. A deterministic resolution fixture covers this on CI; a real filesystem symlink regression is skipped locally because the Windows account cannot create symlinks. Final local verification passes: deterministic suite `2,234 passed, 19 skipped` (267.36 seconds), repository Ruff, strict OpenSpec (`6/6`), `git diff --check`, and the diagnostic dry-run with digest-pinned image preflight. The same-SHA CI is still pending. No model request or live route has been issued.

### 2026-10-24 Zero-dispatch diagnostic candidate correction

Candidate `7148a0d0bb5a14aa492cdec1d7d5633d291ad950` passed same-SHA CI run [40066788788](https://github.com/shuaibilx/DecAgent/actions/runs/40066788788), but its first live diagnostic preflight stopped before model/tool dispatch. The bounded diagnostic builder changed `resource_limits` while leaving each selected task's `resource_profile_digest` at the smoke value, so `build_public_comparison_trial()` correctly rejected the mismatch. Read-only ledger inspection confirms eight routes remain pending, with zero model, tool, machine-time, or τ-step attempts and no score/cleanup evidence. No Qwen request or cost was incurred; the original batch start/deadline remains recorded.

### 2026-10-24 bounded Qwen diagnostic: Plan route starvation and recovery design

Candidate `91d1cf0e25f87d3f74eb83674f6257c8004b8a98` passed final same-SHA CI run [40068945541](https://github.com/shuaibilx/DecAgent/actions/runs/40068945541), attempt 2 (12/12 jobs). The first attempt had one failing Python 3.13 PostgreSQL shard; rerunning failed jobs passed. A bounded live diagnostic then used 3 Qwen requests (22,191 tokens; frozen-profile pre-credit estimate ¥0.0201905), 2 MCP calls, and 64.5 seconds of measured phase runtime. CSM/ReAct failed response validation and received the original verifier score 0.0; CSM/Plan failed before any model or tool dispatch after the diagnostic's three-request Agent-role ceiling had been consumed. Six other route slots remained pending. No usage was unknown, Jev/simulator/judge calls were zero, cleanup was verified on both terminal routes, and forbidden side effects were not measured. This result did not pass the required plan→MCP→official-score readiness gate.

The issue was the diagnostic scheduler's ReAct-first ordering combined with a three-request Agent-role cap, not a reason to increase the approved budget. The corrective design changes only the diagnostic route order to Plan then ReAct for each task, preserving the normal baseline definition, task denominator, and all limits. A deterministic regression checks this order through the actual phase scheduler. The completed diagnostic ledger and its original 45-minute window remain closed; this code change cannot replay or extend it. Another live run needs a new candidate, same-SHA CI, a new budget window, and separate authorization.

The corrected working tree passed the full deterministic suite (`2,237 passed, 19 skipped`), Ruff, strict OpenSpec validation and `git diff --check`. Its read-only diagnostic dry-run selected four fixed development tasks and eight slots in Plan-first order, with no ledger, model request or MCP call. Commit `dce9068dde8740b5ea10dc96e33495e7085ea491` passed same-SHA GitHub Actions [run 40071453290](https://github.com/shuaibilx/DecAgent/actions/runs/40071453290), 12/12 jobs; there were no CI failures.

The correction updates the four diagnostic records with the digest of the bounded resource profile. A successor candidate may take over this still-unspent diagnostic authorization only after both old and new ledgers prove zero dispatch, all eight routes pending, and no stopped batch. The old ledger remains retained and hashed in the user-level authorization's supersession history; the replacement ledger carries the original `started_at` and deadline, so candidate correction cannot renew the 45-minute budget. Unknown or any started attempt still blocks candidate transfer. These recovery changes are not yet committed or CI-verified; no model call has been made.
