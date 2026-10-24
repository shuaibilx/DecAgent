# Tasks

## 当前执行状态与后续顺序（2026-10-25 00:25，北京时间）

- **OpenSpec 状态：** 主 change 为 **76/82**；待完成 9.6.D/F/G 与 10.4–10.6。四个已暴露案例只作开发诊断，正式 pilot 仍是 24 个未暴露任务，当前尚未冻结 pilot manifest；公开基准为 0/286。
- **readiness v2 结果：** 旧账本不恢复，unknown 不重放。本次独立批次于 2026-10-24 完成：Qwen 3 次/14,459 tokens，通过连通与工具契约，但计划未通过冻结 compiler（`PlanProposalInvalid`）；OpenRouter DeepSeek 4 次/16,457 tokens，通过连通、工具契约、只读计划 compiler 与流式 usage。总计 7 次/30,916 tokens、56.433 秒，无 unknown。抵扣前冻结费率估算为 Qwen ¥0.0137883、OpenRouter $0.0121728；提供方账单实际值未返回。无业务 MCP 或官方 scorer 调用。详见[机器证据](../../../evals/benchmarks/evidence/routed-model-readiness-20261024-132906.json)与[执行说明](../../../evals/benchmarks/evidence/routed-model-readiness-20261024-132906.md)。v2 未记录 Fireworks 路由，不能解锁固定 Fireworks 的公共 cohort，也不能与 Qwen 结果拼接。
- **readiness v3 真实结果：** 新批次获独立授权后于 2026-10-24 执行，限定 OpenRouter Fireworks profile，四阶段 4/4 通过；4 次请求、16,282 tokens、16.426 秒、抵扣前冻结费率估算 $0.0117276。无 unknown、重试、Qwen/Jev/业务 MCP/官方评分调用；供应商账单实际值不可用。v7 的 `validate_live_public_readiness` 接受该机器证据。详见[机器证据](../../../evals/benchmarks/evidence/routed-model-readiness-fireworks-v3-20261024.json)和[执行报告](../../../evals/benchmarks/evidence/routed-model-readiness-fireworks-v3-20261024.md)。
- **单例开发闭环预检：** 使用受 v3 readiness gate 保护的 `openrouter-diagnostic` 入口，自动选择冻结 manifest v12/profile v7 中唯一 CSM 的 `plan_mcp` 路线（`task_20260106_161859_265_00364ece_0e836ce8`）。当前代码将这一例上限收紧为 5 次 OpenRouter Agent 请求/280,000 tokens/$0.30、40 次工具调用、1,800 秒；用户模拟器、LLM judge 和 Jev 均禁用。正确 phase dry-run 通过，`dry_run=true`、任务 1/1、路线 1/1、ledger/model/MCP 派发均为 0。机器记录见[最新单例 dry-run 证据](../../../evals/benchmarks/evidence/openrouter-diagnostic-preflight-v4-20261025.json)；较早 v3 记录保留作历史。回归见[诊断门禁测试](../../../evals/deterministic/test_public_diagnostic_gate.py)。此前误用默认 v11/Qwen manifest 的 Python 手工预检拒绝 Fireworks profile；未派发，正确 CLI phase 会加载 v12/v7。
- **执行期门禁审查修正：** 首次 live 路由准备审查发现 CSM 非 τ 路线的冻结 `tau_steps=0` 被 `ControlledRoutes` 构造器拒绝；已允许 0 步预算并增加非 τ Plan 路线回归。另增加 Qwen readiness v2 的 `model_profile_digest` 与冻结 primary profile 一致性校验，防止旧证据用不匹配的 profile 资格放行。受影响的 7 个测试文件为 202 passed、1 skipped；全量 deterministic 为 2,310 passed、19 skipped；Ruff、OpenSpec strict 6/6 通过。最新 v4 dry-run 仍是 1 CSM Plan、0 ledger/model/MCP 派发。上述代码尚未提交、未过同 SHA CI，未运行真实业务路线。
- **下一步：** 先完成全量 deterministic、最终差异审查和同 SHA CI；再就单一 CSM Plan 开发闭环另行取得授权。授权后才可执行 Plan→实际 MCP→EnterpriseOps 官方 verifier→cleanup；最多 5 次 Fireworks Agent 请求、280,000 tokens、抵扣前冻结费率估算 $0.30、40 次 MCP 工具调用、30 分钟；不得调用 Qwen、用户模拟器、LLM judge 或 Jev。该任务已暴露、只作工程诊断，不进入 pilot 分数，也不是策略比较。保留失败与未知证据；闭环通过和同 SHA 验收后再运行 24-task/68-route pilot。readiness 与单例诊断都不能单独证明性能或 Jev 收益。

## 历史执行状态与后续顺序（2026-10-24 18:26，北京时间）

- **交付目标：** 10 月 31 日前交付 SOP-Agent 可复现的小规模公平对照报告。主 change 为 **73/80**；剩余 7 项是 9.6.D/F/G 与 10.3–10.6。核心 Agent 不重建；游客扩展、私有 CD、公网入口、部署演练、记忆消融和阈值扫描暂停。
- **已完成的本轮真实诊断：** Qwen round 2 在 `907017651e72a2a5d295006f4858f0055d1b9cea` 上执行，CI 12/12。4 任务/8 路线中 3 条终结、5 条 pending、0 条成功；3 次 Qwen 请求、34,054 tokens、1 MCP 调用、180.34 秒、0 用量 unknown，冻结费率估算 gross API cost ¥0.0433172。CSM Plan/ReAct 官方 verifier 均为 false；ITSM Plan 额度耗尽后未实际派发；没有 τ 调用。详见[round 2 证据](../../../evals/benchmarks/evidence/sop-agent-diagnostic-round2-20261024-9070176.md)。这不是基准成绩。
- **OpenRouter 诊断状态：** Qwen 批次原始绝对 deadline 为 2026-10-24 09:21 UTC（17:21 北京时间），本次恢复时已过期。OpenRouter live 请求为 **0**；仅完成 1 条 CSM Plan dry-run，0 ledger、0 模型/MCP 派发。禁止用新 ledger 延长 45 分钟窗口；需新的明确批次授权后才能派发。
- **当前候选与离线验证：** `ee43db0e1bd52073ab4513c1026b3973dff2dc6c` 已推送到 `feat/conditional-evaluation`；同 SHA GitHub Actions [run 40084779985](https://github.com/shuaibilx/DecAgent/actions/runs/40084779985) 的 12/12 jobs 通过。当前候选增加了角色级请求上限的 route-claim 前置检查，避免额度耗尽后把未派发路线记为任务失败；round 2 的旧记录保持不变。回归验证：Agent/OpenRouter cap scheduler 两项测试 2 passed，持久账本跨路由 cap 测试 1 passed，相关 Ruff 通过。当前 `--phase diagnostic --check-images` dry-run 选择 4 个开发案例/8 个路线槽，镜像检查通过，`ledger_created=false`、模型/MCP 派发均为 0；详见[当前候选预检](../../../evals/benchmarks/evidence/sop-agent-current-diagnostic-preflight-ee43db0-20261024.json)。这不是新诊断批次，也不是 pilot。
- **真实评测门禁与执行顺序：** Qwen round 2 的绝对 deadline 为 2026-10-24 09:21 UTC（17:21 北京时间），已经过期；OpenRouter live 请求仍为 **0**。不得用旧 45 分钟授权、新 ledger 或新 `started_at` 继续真实调用；必须先取得新批次明确授权并冻结候选、provider/profile、请求/Token/费用/时间上限。当前 pilot evidence gate 只支持 Qwen，OpenRouter readiness 不能直接放行现有 pilot；若选 OpenRouter 作为 pilot provider，先实现并验证匹配的 profile、evidence gate 和独立账本。任何 pilot 都须与 readiness provider/profile 完全一致，不跨 provider 拼接；24-task/68-route 样本保持 CSM 10、ITSM 10、τ retail test 2、τ telecom test 2，Jev 只测已审核的 CSM knowledge Choice；N/A 单列。
- **预算与结论：** 全周共同硬上限为 500 次通用请求、5,000,000 token、1,600 次工具调用、4 小时、Qwen gross API ¥13.50、OpenRouter gross API $5；Jev 仍用原累计 $1 账本，unknown reservation 不释放。成本按真实 usage 与冻结费率标准化，代金券和现金账单分开。报告保留完整分母、终态、成本完整性、端到端/分阶段延迟、配对区间和副作用测量状态；不降低 95% 自动正确率与每类至少 100 个样本的生产阈值资格要求。
- **历史边界：** v10 首波 `5b43a45` 的 0 success、3 failed、5 unknown，以及早期 `91d1cf0` diagnostic 的 2 failed/6 pending 均不可重放、不可并入新试验，也不作为性能证据。完整历史见[任务整理前快照](../../../generated/261024/0952_jev完整任务快照_清理前.md)。

## 历史状态（2026-10-24，round 2 批次建立前）

以下历史记录保持原样以保留证据；其“需要新授权/本轮尚未实现”等时间性文字由上方当前执行方案取代，不代表当前授权状态。

- **游客与本机交付：** [guest-sop-local-trial](../guest-sop-local-trial/tasks.md) 为 22/28；该 change 暂停，未完成 2.5、4.4、5.3、5.4。
- **完整历史快照：** [本次整理前快照](../../../generated/261024/0952_jev完整任务快照_清理前.md)；[上一版逐字快照](../../../generated/261022/任务整理/1202_jev任务完整快照_整理前.md)。详细实现证据见[工作台](../../../generated/261021/任务整理/1434_实现与工作台证据.md)、[评测](../../../generated/261021/任务整理/1434_评测基础证据.md)、[模型与真实评测](../../../generated/261021/任务整理/1434_模型与真实评测证据.md)、[整体验收](../../../generated/261021/任务整理/1434_整体验收证据.md)。

## 1. 基线留存与兼容准备

- [x] 1.1 冻结 business-2 MCP 契约、历史基线入口及版本指纹；验证固定任务可由留存入口执行。
- [x] 1.2 建立隔离的新旧任务数据库夹具；验证旧用户库不被读取、重置或改写。
- [x] 1.3 编写并核对迁移、旧任务恢复、新任务版本及回退规则。

## 2. 计划契约、可信模板与规划器

- [x] 2.1 定义版本化计划/节点/绑定/条件/证据契约；覆盖合法、非法和规模边界。
- [x] 2.2 冻结可信 Jev 模板、候选来源、选择语义、无匹配及评分尺度；拒绝运行时改写和伪造来源。
- [x] 2.3 编译时校验依赖、可达性、条件边、合流与有界展开；拒绝循环、悬空引用和超限计划。
- [x] 2.4 实现类型化参数绑定与稳定记忆槽解析，并通过最终 MCP schema 校验。
- [x] 2.5 接入受约束的 Planner 适配、计划 schema 和最多一次格式修正；拒绝非法输出和任意代码。
- [x] 2.6 以真实证据判断完成状态并确定性渲染结果；未知提交不得被宣称成功。
- [x] 2.7 发布 schema、模板规则和绑定示例；示例经同一编译器校验且可随包读取。

## 3. 持久解释器与新旧任务恢复

- [x] 3.1 增加幂等数据库迁移并证明旧数据、操作、审批及账本不变。
- [x] 3.2 实现不可变计划修订、稳定节点身份、CAS 与租约保护。
- [x] 3.3 实现持久 PlanRuntime；验证分支、合流、恢复计数及上限等待。
- [x] 3.4 实现有界 Jev 批处理；验证共享状态合批、动态重构题和旁支隔离。
- [x] 3.5 接入受治理 MCP 写操作；崩溃恢复时按 request/operation 回执核对且不重复副作用。
- [x] 3.6 实现局部重规划与冻结区；已提交、待确认或 unknown 节点不可被抹除或盲目重放。
- [x] 3.7 隔离 legacy business-2 与新任务版本分派；验证版本未知及回退边界。
- [x] 3.8 更新恢复文档与状态/时序图，并以崩溃测试核对恢复路径。

## 4. 预算分离与实际请求记账

- [x] 4.1 按模型族、用途和节点归属预留原子分类额度；并发下不得超额。
- [x] 4.2 分离模型与非模型动作门禁；保留总费用、unknown、时长、取消和租约硬限制。
- [x] 4.3 为重试、摘要、最终生成和发送 unknown 统一计量；恢复不得释放未知预留。
- [x] 4.4 更新预算配置、错误码和用户说明；认证扩额及历史回执符合账本契约。

## 5. 阈值策略与持久人工接管

- [x] 5.1 实现 Choice/Score 原值、Noul 派生值、策略匹配与分布校验；覆盖边界和异常答案。
- [x] 5.2 持久化策略版本及生效事件；不得低于管理员阈值或缺省自动放行。
- [x] 5.3 实现绑定 actor/task/node/revision 的人工待办与回复；覆盖全部 primitive 和拒绝/补充等结果。
- [x] 5.4 恢复前重验权限与新鲜度；过期或不匹配回复不得产生副作用。
- [x] 5.5 保持稳定槽显式记忆、低分等待及独立写入确认；通用规划不得自动读写。
- [x] 5.6 发布阈值和人工接管指南、流程图，并核对置信度与正确率的区别。

## 6. 工作记忆与上下文

- [x] 6.1 实现可重建工作记忆；崩溃恢复和人工更正保留审计历史。
- [x] 6.2 实现受限规划/判断上下文；保留必需证据，不将推断冒充执行结果。
- [x] 6.3 实现员工隔离的业务记忆迁移、确认、去重、版本、TTL 和撤销。
- [x] 6.4 记忆写入经可信模板筛选；稳定槽只供已发布流程读取。
- [x] 6.5 执行时按当前员工/scope 解析稳定槽并重验记录；缺失、歧义或失效均暂停。
- [x] 6.6 发布记忆与上下文说明、数据流图和样例；敏感信息不进入权限指令或通用 trace。

## 7. 验证流程发布与复用

- [x] 7.1 实现 workflow 草稿、验证、发布、撤销及版本指纹；一次成功不能自动发布。
- [x] 7.2 实现显式流程匹配与参数重绑定；不唯一或不完整时安全暂停。
- [x] 7.3 发布三个工单流程模板；热复用正常路径零 primary 请求，重规划单独计量。
- [x] 7.4 处理依赖漂移与撤销；待确认或 unknown 操作仍可核验但不重放。
- [x] 7.5 发布模板编写、验证、发布和失效指南，并通过注册表及回归校验。

## 8. 工作台和 trace

- [x] 8.1 提供受 ACL/CSRF 保护的计划、节点、决策、用量查询及脱敏导出。
- [x] 8.2 展示实际路径、未走分支、参数来源和决策策略；刷新后与持久状态一致。
- [x] 8.3 实现语义待办、阈值及本人记忆界面；切换员工/断线恢复不产生重复副作用。
- [x] 8.4 串联任务、计划、节点、模型、策略、操作与回执时间线；支持本地故障定位。
- [x] 8.5 更新使用指南与 README 导航；按指南完成隔离环境中的提交、暂停、回复和恢复。

## 9. 评测数据、统计和基线工具

- [x] 9.1 固定 EnterpriseOps-Gym/τ-Bench 来源、许可、任务、数据库及镜像版本；来源不明时阻止正式评分。
- [x] 9.2 固定官方划分、完整清单与子集规则；标签不进入 Agent 输入，未跑任务如实列出。
- [x] 9.3 冻结 DecAgent 辅助判断集及留出集；标注来源、人工核对和场景隔离。
- [x] 9.4 实现原始预测缓存、阈值扫描和策略候选报告；只适用于可信决策族。
- [x] 9.5 实现 Wilson 区间、配对 bootstrap、误/漏接管及交互分类统计。
- [x] 9.6.A 在隔离数据库上完成冻结 business-2 执行基线配对并核对环境/身份/工具指纹。
- [x] 9.6.B 验证 EnterpriseOps 环境重置、私有 verifier、失败分类和清理；私有标签不得进入模型输入。
- [x] 9.6.C 通过 τ 子进程桥记录 transcript，再使用官方 evaluator；ReAct/Plan 均支持模拟用户澄清。
- [ ] 9.6.D 冻结自然适用的 Jev 模板，并在同一状态输入下完成 Jev/通用模型真实配对。当前仅审核通过 CSM 知识文章 Choice；其他域 N/A，结构化条件走规则。见[适用性审阅](../../../evals/benchmarks/evidence/jev-applicability-contract-review-20261022.md)。
- [x] 9.6.E 冻结公开 manifest、暴露组、profile、预算、复用/记忆消融与计时；标签和私有评分材料不得进入 Agent 输入。完成仅表示设计冻结，不代表真实评测通过。
- [ ] 9.6.F 验收真实模型/MCP callback、ReAct/Plan/Jev 配对、τ 模拟器、官方评分、最终回复、cleanup 与 unknown 停止派发。完成条件为至少一个计划路线通过真实工具、官方评分和清理闭环；历史失败不能替代验收。首次 live 准备发现 `tau_steps=0` 构造边界并已用回归测试修复；全量 deterministic 已通过（2,310 passed、19 skipped），仍待同 SHA CI。CI 通过后再执行一个已暴露 CSM `plan_mcp` 开发闭环，其限制为 5 次 Fireworks Agent 请求、280,000 tokens、$0.30、40 MCP 工具调用和 30 分钟；用户模拟器、LLM judge、Jev 禁用，须另行授权后才可派发。见[最新单例预检](../../../evals/benchmarks/evidence/openrouter-diagnostic-preflight-v4-20261025.json)及[历史诊断报告](../../../evals/benchmarks/evidence/sop-agent-diagnostic-20261024-91d1cf0.md)。
- [ ] 9.6.G 完成四域首波闭环验收并保留失败、unknown 与准确分母。历史 v10 结果为 0 success、3 failed、5 unknown，cleanup 8/8；不得重放或计作性能证据。见[首波报告](../../../evals/benchmarks/evidence/public-first-wave-live-20261024-5b43a45-100qwen.md)。
- [x] 9.7 持久化 Jev 累计 1 美元预留/结算/unknown 停止账本；历史费用不可重置。
- [x] 9.8 对计划路线实现证据约束的确定性最终回复及有界 Qwen 总结；总结失败/unknown 受账本约束。

## 10. 真实质量和效率验收

- [x] 10.1 完成主模型 readiness 检查与放行记录；旧 2026-10-18 unknown 不重试。
- [x] 10.2 按每决策族至少 100 个自动样本及 95% 正确率门槛评估阈值资格；样本不足时不选生产阈值、不用 holdout 补样。
- [x] 10.3 完成模型-specific readiness 与允许的 OpenRouter continuation。Qwen round 2 历史批次不重放。本次 readiness v2 在候选 `8acbe767ce1d7091c7ce23760c630bd34002ad94` 同 SHA CI 12/12 通过后执行；7 请求、30,916 tokens、56.433 秒，无 unknown。Qwen 使用 3 请求/14,459 tokens，连通及工具契约通过但 Plan 为 `PlanProposalInvalid`；OpenRouter DeepSeek 使用 4 请求/16,457 tokens，四阶段 readiness 全通过。抵扣前冻结费率估算分别 ¥0.0137883 与 $0.0121728；实际供应商账单字段不可用。无业务 MCP/官方 scorer 调用。OpenRouter 资格独立于 Qwen，不能解锁 Qwen-specific pilot gate；开展 DeepSeek pilot 前还需实现并通过匹配的 eligibility、预算账本与同 SHA CI。完整结果见[机器证据](../../../evals/benchmarks/evidence/routed-model-readiness-20261024-132906.json)和[执行说明](../../../evals/benchmarks/evidence/routed-model-readiness-20261024-132906.md)。
- [x] 10.3.B 将 routed readiness 升级为 profile-bound v3：OpenRouter readiness 请求固定 Fireworks；账本持久绑定 cohort/profile 与各阶段请求 fingerprint；attempt 记录实际 route/request profile；gate 校验 ledger、provider、model、endpoint、价格 digest 和覆盖执行费率的成本上界。旧 v1 仅保留审计，不得解锁 live cohort；旧 v2 不能靠补外层 metadata 升级；其他 provider 的 Qwen unknown 保留但不覆盖独立 OpenRouter qualification。合法恢复保留 `status=settled, dispatch_state=not_sent` 历史，但不计入真实 `used_calls`；live executor 使用 plan 保存的 readiness path 再次校验，阻止绕过 loader 的直接调用。见[readiness gate 回归](../../../evals/deterministic/test_public_diagnostic_gate.py)、[live executor 回归](../../../evals/deterministic/test_live_public_runner.py)、[模型 profile 回归](../../../evals/deterministic/test_routed_readiness.py)和[模型请求身份回归](../../../evals/deterministic/test_primary_route_retries.py)。本地全量 deterministic `2,304 passed, 19 skipped`；同 SHA GitHub Actions [run 40104969197](https://github.com/shuaibilx/DecAgent/actions/runs/40104969197) 对提交 `c7a1d9aa4d9c2bb1f43ae4ebe78ae1ff9ac6dae0` 的 11/11 jobs 全部通过。
- [x] 10.3.C readiness 按冻结 cohort primary profile 选择并持久绑定目标 provider；v7 cohort 为 OpenRouter Fireworks。实现提交 `69c6c6f0fb7c86ee3a1c31b54d049e1a856c37bb` 通过同 SHA GitHub Actions [run 40106888705](https://github.com/shuaibilx/DecAgent/actions/runs/40106888705)，11/11 jobs。后续仅文档提交，不改变被测代码。新授权批次最多 4 次请求、300,000 tokens、45 分钟及抵扣前 $0.30；实际四阶段 4/4 通过，4 次请求、16,282 tokens、16.426 秒，冻结费率估算 $0.0117276，无 unknown/超额。v7 readiness gate 本地验证通过。后续审查另补 Qwen readiness v2 的 primary `model_profile_digest` 一致性检查及回归；本项已完成的 Fireworks readiness 证据不变。证据见[机器记录](../../../evals/benchmarks/evidence/routed-model-readiness-fireworks-v3-20261024.json)与[报告](../../../evals/benchmarks/evidence/routed-model-readiness-fireworks-v3-20261024.md)。本任务只验收模型 profile；业务 MCP、官方评分器和正式 benchmark 均未运行。
- [ ] 10.4 在相同冻结任务、初始状态、权限、工具和模型下比较四种路线：ReAct+Skill+MCP、一次规划+规则执行+MCP、一次规划+通用模型语义判断+MCP、一次规划+Jev+MCP。前两种及规则路线覆盖所有 24 个任务；两个语义路线只对同一 10 条 CSM 任务配对。结构化条件走规则；无自然语义节点的 Jev 记 N/A，不算零成本成功。报告任务质量、评分/副作用、调用、成本、分阶段延迟和同状态配对差异。
- [ ] 10.5 分开报告规划/Agent、τ 官方用户模拟器、官方裁判和任何真实员工交互；冻结模型 profile 并分别计量角色用量。模拟器或评分器不得冒充人工批准。不得把历史已失败/unknown 路线重放后混入新试验。
- [ ] 10.6 发布含准确任务/路线/评分分母、所有终态、配对区间、复现命令、成本完整性、端到端与分阶段延迟、自然 Jev 覆盖和图表的报告。说明错误副作用是否实际测量；区分标准化 API 成本、voucher/现金账单；样本不足或质量不达标时不宣称 Jev 收益或生产阈值资格。
- [x] 10.7 完成有界 Qwen 离线定位与 readiness 复验；unknown 不重置、不重发、不释放预留。
- [x] 10.8.A 冻结规划、回复、通用语义对照、τ 模拟器和裁判的完整模型 profile。
- [x] 10.8.B 固定 Qwen 主路由和 OpenRouter DeepSeek 备用路由；逐请求安全记录请求/模型身份、用量、成本及耗时。OpenRouter 主模型及 fallback 的流式/非流式请求都显式要求 usage，回归见[模型路由测试](../../../evals/deterministic/test_model_routing_config.py)。
- [x] 10.8.C 验证有限重试、unknown 预留、正式对照禁降级和模型漂移隔离；未知写操作不重放。
- [x] 10.8.D 固定 Qwen CNY profile 与 manifest 费率来源；按抵扣前费用和账单抵扣分开记录，不猜测账单或汇率。
- [x] 10.9 完成有界多阶段 readiness；至少一个模型通过 usage 完整的只读计划编译，不调用业务 MCP。
- [x] 10.10.A 固定 dry-run phase 与准确任务/路线分母；默认零账本、零模型/MCP 派发。
- [x] 10.10.B 固定 cohort/profile/预算身份；请求、Token、时间、费用与 Jev 账本持久且不可换路径重置。
- [x] 10.10.C 实现活动执行者租约、heartbeat 与 fencing；恢复不能重放完成或 unknown 路线。
- [x] 10.10.D 接通 supervisor、ledger、MCP、deadline、官方 scorer 和 cleanup，并以隔离测试验证。
- [x] 10.10.E 验证公开 runner 故障恢复与四域受控工程闭环；工程闭环不等于公开基准成绩。
- [x] 10.10.F 诊断仅记录允许的阶段、状态与安全原因码；公共 Plan 解析/编译失败区分 `malformed_response` 与 `invalid_plan`，不保存异常消息或请求/响应正文，也不暗中重试。新增 route ledger 的安全 `diagnostic_code` 持久化与读取验证，并覆盖 unknown 路线仅补录清理、不改写业务终态的回归。清理补录不可重放；无法恢复的历史异常原因保留为未知。回归见[公开规划器回调测试](../../../evals/deterministic/test_public_planner_callback.py)、[试次账本测试](../../../evals/deterministic/test_business_public_trials.py)与[执行器测试](../../../evals/deterministic/test_business_public_execution.py)；完整历史见[整理前任务快照](../../../generated/261024/0952_jev完整任务快照_清理前.md)。
- [x] 10.10.G 角色级请求额度达到上限后，在下一路线 claim 前持久化批次停止并跳过未开始路线；验证不创建该路线的模型、工具或机时 attempt，且不将其计作任务失败。见[角色上限调度回归](../../../evals/deterministic/test_business_public_execution.py)与[持久账本恢复回归](../../../evals/deterministic/test_business_public_trials.py)。

## 11. 整体回归与本机交付

- [x] 11.1 在隔离数据库运行确定性、集成、Docker 边界测试及 Ruff；跳过项单列，不以缺依赖的 skip 冒充通过。
- [x] 11.2 在源码树外验证发行包、MCP wire、资源和恢复/回退入口。
- [x] 11.3 审计需求、场景、测试、实测证据和指南；清理新路径重复逻辑，保留必要兼容，并将独立 Linux 验收列为后续。
