# Tasks

## 当前状态（2026-10-24，候选修复本地验证通过，候选 CI/live 待执行）

- **OpenSpec checkbox：72/79 完成。** 未完成：9.6.D、9.6.F、9.6.G、10.3、10.4、10.5、10.6。
- **历史真实首波：** 候选 `5b43a45962ee16bc0f61015d1063ee9ad6c676fe` 上 4 个任务/8 条 ReAct 与 Plan 路线为 3 failed、5 unknown、0 success；清理 8/8 verified。仅 ITSM/ReAct 与 τ telecom/ReAct 获官方评分，均为 0.0。Jev 未纳入；unknown 不重放。详见[首波报告](../../../evals/benchmarks/evidence/public-first-wave-live-20261024-5b43a45-100qwen.md)。
- **本轮本地进展：** 已有 metadata-only 24-task/68-route pilot sampler、primary semantic 完整路线与 Jev execution-only 同一 Plan/decision-input 配对、无自然判断 N/A 与 primary 失败配对 incomplete 区分、ReAct/τ callback 安全原因码、CSM `find_entitlements.product_id` nullable 输出 observation 契约、Qwen CNY 费用硬上限事务门禁，以及 bounded diagnostic dry-run/phase 修复。提交 `7148a0d` 的同 SHA CI 12/12 通过。第一次 live 仅在派发前因诊断任务的 `resource_profile_digest` 沿用了 smoke 摘要而停止；账本 8 条路线均 pending，模型/工具/机器时间/τ 尝试为 0，未产生模型请求或费用。当前修复为诊断任务重算资源摘要，并加入仅限零派发状态的候选接替：旧、新账本须保持 8 条路线 pending、尝试数为 0，旧账本不得有评分、清理结果或停止标记；接替保留旧账本及审计摘要，沿用原 45 分钟起点和截止时间，不增加预算。修复后的离线 dry-run、Ruff、OpenSpec strict 和全量确定性测试（2,236 passed、19 skipped）通过；同 SHA CI 与真实诊断仍待执行。详见[零派发诊断记录](../../../evals/benchmarks/evidence/public-diagnostic-pre-dispatch-20261024-7148a0d.md)。
- **尚未验收：** 尚未发出新 Qwen、DeepSeek 或 Jev 请求，尚未冻结 24-task pilot manifest、运行新 cohort 或获得新官方评分。Qwen historical Plan timeout 根因仍待有界诊断；OpenRouter 的备用诊断 cohort 仍未建立；历史 unknown 不重放。前一候选 CI 和本轮旧版 `2,234 passed, 19 skipped` 不覆盖当前未提交修复。
- **候选 CI：** 已有候选 `7148a0d0bb5a14aa492cdec1d7d5633d291ad950` 的 [CI run 40066788788](https://github.com/shuaibilx/DecAgent/actions/runs/40066788788) 12/12 通过；它不覆盖当前工作区修复。
- **游客与本机交付：** [guest-sop-local-trial](../guest-sop-local-trial/tasks.md) 为 24/28，剩余 2.5、4.4、5.3、5.4。
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
- [ ] 9.6.D 审核并冻结自然适用的 Jev 模板；在同一状态输入下配对 Jev 与固定通用模型。离线审阅确认仅 CSM 知识文章选择适用，使用 Choice；其他域 N/A，结构化条件走规则。状态/问题相同的 mock 配对回归通过；真实配对仍待新 cohort 与授权。见[适用性审阅](../../../evals/benchmarks/evidence/jev-applicability-contract-review-20261022.md)。
- [x] 9.6.E 冻结公开 manifest、暴露组、profile、预算、复用/记忆消融与计时；标签和私有评分材料不得进入 Agent 输入。完成仅表示设计冻结，不代表真实评测通过。
- [ ] 9.6.F 接通并验收真实模型/MCP callback、ReAct/Plan/Jev 配对调度、τ 模拟器与官方裁判、最终回复、unknown/评分失败分类、环境清理及 unknown 后停止派发。工作区现有精确 Plan 配对、决策输入摘要、N/A/incomplete 区分、τ 安全原因码、CSM nullable observation 修正及四域 diagnostic gate；新增 Qwen CNY 费用原子预留/超限停止、共享 diagnostic/pilot 总额度、忽略状态中的证据绑定与报告修复、用户级候选/phase 授权锁、预检前绝对墙钟、同路线就绪证据及 symlink-safe 报告读取。最终本地 deterministic `2,234 passed, 19 skipped`，全仓 Ruff、OpenSpec strict（6/6）与 diff check 通过；`diagnostic` dry-run 显示 4 任务/8 路线、6 次 Qwen 请求/600,000 token/2,700 秒，且 0 ledger/模型/MCP。当前修改仍未过同 SHA CI，也未执行 live diagnostic。Plan 超时和历史 route 原因不能由旧 unknown 反推，见[首波报告](../../../evals/benchmarks/evidence/public-first-wave-live-20261024-5b43a45-100qwen.md)及[design 诊断记录](design.md#2026-10-24-diagnostic-fee-admission-and-report-completeness)。
- [ ] 9.6.G 完成四域首波验收：2026-10-24 固定 4 任务/8 路线结果为 3 failed、5 unknown、0 success，清理 8/8 verified；仅两条 ReAct 路线获得官方评分且均为 0.0。未满足完整评分/质量门槛，unknown 不重放，下一批需重新冻结 cohort 并授权。见[首波报告](../../../evals/benchmarks/evidence/public-first-wave-live-20261024-5b43a45-100qwen.md)。
- [x] 9.7 持久化 Jev 累计 1 美元预留/结算/unknown 停止账本；历史费用不可重置。
- [x] 9.8 对计划路线实现证据约束的确定性最终回复及有界 Qwen 总结；总结失败/unknown 受账本约束。

## 10. 真实质量和效率验收

- [x] 10.1 完成主模型 readiness 检查与放行记录；旧 2026-10-18 unknown 不重试。
- [x] 10.2 按每决策族至少 100 个自动样本及 95% 正确率门槛评估阈值资格；样本不足时不选生产阈值、不用 holdout 补样。
- [ ] 10.3 先以四个已暴露开发案例走通有界 live diagnostic：通用模型总计最多 12 次请求/600,000 Token/45 分钟；Qwen cohort 最多 6 次并以原子 CNY 费用预留限制 ¥13.50。若 Qwen 仍不稳定，OpenRouter DeepSeek 的最多 6 次诊断必须冻结独立提供方/价格/模型组，不能在本次 Qwen 诊断或正式比较中混用，且仍计入同一总诊断资源上限。至少一个固定模型经过真实计划、MCP、官方评分及清理闭环后，才按 seed `sop-pilot-20261024-v1` 仅使用 metadata 冻结 CSM 10、ITSM 10、τ retail test 2、τ telecom test 2 共 24 任务/68 路线。正式 pilot 必须绑定诊断报告 SHA 与相同候选，并从全周共享的 500 次 Qwen 请求、500 万 Token、1,600 工具调用、4 小时、¥13.50 上限中扣除诊断实际用量及 unknown 预留；不得用新 cohort/state 重置额度、重放历史 unknown 或换题补分。当前 diagnostic 尚未跑，24-task manifest 尚未生成，206+80 全量未开始。
  - 历史 2026-10-24 首波仍为 3 failed、5 unknown、0 success，清理 8/8；两条官方分数均 0.0，不作为新 diagnostic 成绩或 Jev 收益证据。此前 100 请求/100 万 Token/2 小时/¥2.70 为旧候选预算，已由当前 12 请求/600,000 Token/45 分钟/¥13.50 Qwen diagnostic 上限替代，不得用于本次 dispatch。
  - 2026-10-24 离线补齐 cohort 额度冻结：仅 manifest v9+ 可使用低于既有首波最大值的角色及批次硬上限；角色请求/token 配额须精确加总，超上限、布尔值及错误分配均拒绝。默认 v10 manifest 保持完全一致。定向测试和同 SHA CI 已通过；该代码不创建新 cohort，不重用 v10 账本，也不授权真实派发。
  - 2026-10-24 候选 `9a28504439c28bbd35006e860c78801ac4183c82` 的 12-job 同 SHA CI 全绿；首波 `--check-images` dry-run 选择 4 个固定任务/8 个 ReAct-Plan 路线槽，镜像预检通过，`ledger_created=false`、模型调用 0、MCP 调用 0。它未运行评分器，不改变 v10 终态或授权新批次。报告：[操作者摘要](../../../evals/benchmarks/evidence/public-first-wave-preflight-9a28504.md)、[机器报告](../../../evals/benchmarks/evidence/public-first-wave-preflight-9a28504.json)。
- [ ] 10.4 在相同任务、状态、权限、工具及隔离条件下比较 ReAct、一次规划确定性执行和一次规划加 Jev；报告质量、副作用、调用、成本、延迟及配对差异。本轮仅执行 ReAct/Plan 两条路线，没有成功路线或 Jev 配对；工具账本记录 2 次 settled 及 1 次 unknown 调用按注册工具元数据标记为 write-effect，实际禁止副作用数量未测量。仅自然适用语义节点参与 Jev 比较。
- [ ] 10.5 分开报告标签 oracle、τ 官方模拟器与真实员工交互；冻结 profile、分别计量角色用量，模拟用户不得冒充真实审批者。本轮 τ 用户模拟器 10 次请求/32,788 Token，独立记账；裁判 0 请求，真实员工交互 0，不能据此验收人工介入效果。
- [ ] 10.6 发布带准确分母、统计区间、可复现命令和成本/延迟/自动覆盖图的报告；证据不足时不宣称 Jev 收益。本轮已发布 4/24 任务、8/48 路线的人读与机器报告，5 unknown、3 failed、0 success，仅 2 条官方评分且均为 0.0；实际 provider invoice 缺失，unknown usage 采用预留。无法形成完整质量区间或性能比较图，正式报告仍待工程闭环及后续合格样本。
- [x] 10.7 完成有界 Qwen 离线定位与 readiness 复验；unknown 不重置、不重发、不释放预留。
- [x] 10.8.A 冻结规划、回复、通用语义对照、τ 模拟器和裁判的完整模型 profile。
- [x] 10.8.B 固定 Qwen 主路由和 OpenRouter DeepSeek 备用路由；逐请求安全记录请求/模型身份、用量、成本及耗时。
- [x] 10.8.C 验证有限重试、unknown 预留、正式对照禁降级和模型漂移隔离；未知写操作不重放。
- [x] 10.8.D 固定 Qwen CNY profile 与 manifest 费率来源；按抵扣前费用和账单抵扣分开记录，不猜测账单或汇率。
- [x] 10.9 完成有界多阶段 readiness；至少一个模型通过 usage 完整的只读计划编译，不调用业务 MCP。
- [x] 10.10.A 固定 dry-run phase 与准确任务/路线分母；默认零账本、零模型/MCP 派发。
- [x] 10.10.B 固定 cohort/profile/预算身份；请求、Token、时间、费用与 Jev 账本持久且不可换路径重置。
- [x] 10.10.C 实现活动执行者租约、heartbeat 与 fencing；恢复不能重放完成或 unknown 路线。
- [x] 10.10.D 接通 supervisor、ledger、MCP、deadline、官方 scorer 和 cleanup，并以隔离测试验证。
- [x] 10.10.E 验证公开 runner 故障恢复与四域受控工程闭环；工程闭环不等于公开基准成绩。
- [x] 10.10.F 安全保存模型/工具/route 诊断；保留未知和失败阶段、不记录敏感正文，清理补录不可重放。后续 ReAct 路径新增 route supervisor、model callback、response validation、τ simulator 和 tool gateway 阶段；one-shot worker 仅通过无锁有界进程间状态保留传输阶段/状态码。本轮扩展白名单响应原因码，并从 τ 子进程安全传回 route 终态；异常消息、请求/响应正文仍不入账。历史 v10 timeout 与 `ValueError` 不可追溯；本轮扩展尚待同 SHA CI。

## 11. 整体回归与本机交付

- [x] 11.1 在隔离数据库运行确定性、集成、Docker 边界测试及 Ruff；跳过项单列，不以缺依赖的 skip 冒充通过。
- [x] 11.2 在源码树外验证发行包、MCP wire、资源和恢复/回退入口。
- [x] 11.3 审计需求、场景、测试、实测证据和指南；清理新路径重复逻辑，保留必要兼容，并将独立 Linux 验收列为后续。
