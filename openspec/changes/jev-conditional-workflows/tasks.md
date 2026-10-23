# Tasks

## 当前状态（2026-10-24）

- **完成：72/79。** 未完成的 7 项：9.6.D、9.6.F、9.6.G、10.3、10.4、10.5、10.6。
- **最新真实首波（v10）：** 候选 `9d61688c52ee66435a22e18c4fa1c8348a70095a` 同 SHA CI run 39784378797 通过；固定 4 个任务/8 条 ReAct 与 Plan 路线已终态，3 failed、5 unknown、0 success，清理 8/8 verified。仅 τ telecom/ReAct 获得官方评分，结果 0.0；其余 7 条未评分。Plan 路线均遇到 120 秒 Qwen timeout，CSM/ReAct 的 MCP dispatch 为 unknown。共 30 次 Qwen 请求、220,086 已结算 Token、257,347 unknown Token 上界预留（输入部分按 UTF-8 字节安全上界估算，不是服务端 usage）、13 次工具调用（10 个 read settled、2 个已结算调用按工具元数据标记为 write-effect、1 个 read unknown；禁用副作用数量未测量）、墙钟 772.246 秒；已结算使用冻结费率估算 ¥0.1884416，4 次 unknown 保留 ¥0.2681368 费用预留，无供应商逐请求账单凭证。Jev/judge 均未调用。批次已结束但质量门槛未通过；不重放 unknown，剩余 smoke 与全量暂停。见[人读报告](../../../evals/benchmarks/evidence/public-first-wave-live-20261023-v10-9d61688.md)和[机器报告](../../../evals/benchmarks/evidence/public-first-wave-live-20261023-v10-9d61688.json)。
- **2026-10-24 执行前 dry-run：** 在代码 HEAD `edd30f1e60a10a21eebd3d41bced20d6bc8b4063` 运行固定 v10 `first-wave --check-images`，校验通过，选中 4 个任务/8 个路线槽（完整 smoke 分母 24/48），固定镜像检查通过；`ledger_created=false`、模型调用 0、MCP 调用 0。该预检没有运行任务、评分或重放历史 unknown，也没有形成新的 live 授权；公开 benchmark 仍为 0/286。详见[预检说明](../../../evals/benchmarks/evidence/public-first-wave-preflight-20261024-edd30f1.md)和[机器报告](../../../evals/benchmarks/evidence/public-first-wave-preflight-20261024-edd30f1.json)。
- **最近一次全绿代码候选 CI（2026-10-24）：** CSM schema 兼容修正 SHA `7b56d588682a36d8ea2171890676af3e6f45ac3c` 的同 SHA [run 40014153430](https://github.com/shuaibilx/DecAgent/actions/runs/40014153430) 12/12 jobs 成功，墙钟 8:35、累计 Runner 53:11；两个 Python deterministic 各 `2,176 passed, 18 skipped`，六个 PostgreSQL shards 合计 `1,364 passed, 66 skipped`。相同 SHA 的 first-wave dry-run 仅 4 任务/8 路线、镜像和冻结清单预检，0 ledger/模型/MCP；没有 live 执行或新授权。详见[CI 验收证据](../../../evals/integration/evidence/ci-browser-sharding-20261023.md)和[CSM 兼容性记录](../../../evals/benchmarks/evidence/csm-find-user-output-contract-20261024.md)。
- **后续文档-only CI（2026-10-24）：** SHA `ca54b6b45601e1e792636c01d1d350de0d70a88e` 的 [run 40023851522](https://github.com/shuaibilx/DecAgent/actions/runs/40023851522) 11/12 jobs 成功；Python 3.12 production gateway browser E2E 未找到“批准当前冻结参数”按钮而失败，失败时页面记录 `approval.state` 事件和预期的无条件计划记忆上下文 `contract_changed`。此 run 只覆盖文档提交，不验证当前工作区改动；失败原因尚未在本地复现。当前额度冻结补丁仍待完整本地 deterministic 与同 SHA CI。
- **最新分支 CI 复核：** `4e932ce` 的 run `39935184424` 曾在 Python 3.11/3.13 的 PostgreSQL integration shard 2 中因预算日期固定值与系统时钟跨日不一致而失败；已通过显式注入账本时钟修复。另修正 supervisor deadline 将内部 `model_call` 误当成传输进展的问题；新增真实子进程回归和 HTTP 200 后超时的 attempt/route unknown 防重放回归。全量 deterministic `2,164 passed, 18 skipped`、guest budget PostgreSQL `36 passed`、Ruff 和两个 change 的 strict OpenSpec 通过。修复提交 `14ffd1fe238306cdd858673bb786ac2c3bd581ff` 的同 SHA CI [run 39940453829](https://github.com/shuaibilx/DecAgent/actions/runs/39940453829) 9/9 jobs 全绿（12m31s）。
- manifest v9 的候选 `f1a4a704fe2c36b22127006fe0bc9860733923d5` 首波已实际执行：4 个任务、8 条路线为 4 failed、4 unknown、0 success；两条已评分路线均为 0.0，清理 8/8 verified。Qwen 3 次 Plan 请求用量 unknown，保留 198,176 Token 与 ¥0.2052352 费用预留；已知用量冻结费率估算 ¥0.0853511，不是供应商账单。不得重放 unknown，也不得把本次失败尝试说成基准成功。详见[首波执行报告](../../../generated/261022/1426_公开评测首波执行报告_f1a4a70.md)和[机器证据](../../../evals/benchmarks/evidence/public-first-wave-live-20261022-f1a4a70.json)。候选同 SHA [CI run 39731158534](https://github.com/shuaibilx/DecAgent/actions/runs/39731158534) 全部通过。首波后离线复现并修复 τ retail 空工具列表造成的本地拒发；定向 callback 文件 `25 passed`、全量 deterministic `2,025 passed, 18 skipped`、Ruff 与 OpenSpec strict 通过。修复提交 `cb050a92b181cc75c7939a99d8c466a1f912c266` 的同 SHA [CI run 39736719671](https://github.com/shuaibilx/DecAgent/actions/runs/39736719671) 全部通过；Plan 超时与 CSM MCP unknown 仍阻塞真实闭环，历史路线状态不变。
- **最新离线诊断切片：** commit `ec09ccb` 为工具异常安全记录显式/隐式异常链；focused `67 passed`、全量 deterministic `2,028 passed, 18 skipped`、Ruff 与 OpenSpec strict 通过，同 SHA CI [39741402103](https://github.com/shuaibilx/DecAgent/actions/runs/39741402103) 全绿。它不能还原历史 CSM MCP unknown；9.6.F 仍未完成，未发新模型或工具请求。详见[设计日志](design.md#2026-10-22-root-cause-metadata-for-wrapped-tool-failures)。
- **本轮诊断切片：** 已让经核实的模型 `not_sent` attempt 保留有限失败阶段和异常类型；本轮相关回归 `138 passed`、公共 release 边界回归 `1 passed`、Ruff 与 OpenSpec strict 通过。此项不重写历史记录或完成 9.6.F。详见[证据](../../../evals/integration/evidence/public-preflight-diagnostics-20261022.md)。
- **本轮诊断/账单修正：** 新增 allowlisted `tool_output_validation` 阶段并沿有界异常链记账；仅将预期 `ValueError` 标为结果验证，意外运行时异常保持通用阶段。账单聚合在缺供应商实际费率时输出 `provider_billed: null`，另列已知部分和 attempt 数；修正 v10 机器报告，不改路线/Token/冻结费率/unknown 预留。最终验证结果见 design 末尾日志；该改动不能还原 v10 CSM attempt，也不完成 9.6.F。
- **v10 CSM 只读诊断：** 新鲜 pinned 环境中的合成 `find_user` 查询通过目录校验、数据库指纹不变且清理成功；未复现历史 `ValidationError`。原路线没有参数、响应或 traceback，根因仍未确认，unknown 不变，9.6.F 未完成。见[诊断记录](../../../evals/benchmarks/evidence/csm-find-user-readonly-diagnostic-20261023.md)。
- **2026-10-22 执行前候选预检（历史，仅 dry-run）：** `8b8d4c2` 同 SHA GitHub Actions [run 39773178389](https://github.com/shuaibilx/DecAgent/actions/runs/39773178389) 九个 job 全通过，墙钟 12m42s；`--check-images` 选中 4 个固定任务/8 个首波路线槽、清单镜像检查通过，0 ledger、0 模型请求、0 MCP 调用。证据文档跟进提交 `06b3903` 未改运行时代码，九个 CI job 同 SHA [run 39775263485](https://github.com/shuaibilx/DecAgent/actions/runs/39775263485) 全通过，10m18s。上述结果不是 live 评测或模型质量证据；首波 4 小时批次已过期，新 cohort 需单独冻结预算。详见[机器预检](../../../evals/benchmarks/evidence/public-first-wave-preflight-8b8d4c2.json)与[说明](../../../evals/benchmarks/evidence/public-first-wave-preflight-8b8d4c2.md)。
- **v10 cohort 冻结与执行前验证（历史）：** v9 三次 Plan primary 调用均在约 60 秒本地 deadline 终止，usage 与服务端接收状态仍 unknown。新增 profile v5 / manifest v10 仅将 primary timeout 从 60 秒调到 120 秒，模拟器/裁判保持 60 秒，Jev 保持 10 秒；任务、模型、价格、费率、暴露分组与预算不变，旧 v4/v9 和历史账本不可变。v10 manifest digest `1096d0515168eab71b06cb6b9bde558a0fa2a5269ec723aef996886a342b2fed`，profile digest `4fa091d2840928bfb6a00dccfbd452a2dee8c42c465512955fd63923eb0a6721`。定向 inventory `17 passed`、全量 deterministic `2,122 passed, 18 skipped`、全仓 Ruff 与 OpenSpec strict `6/6` 通过；`first-wave --check-images` 选中 4 任务/8 路线槽，0 ledger、0 模型/MCP 请求。候选 `d3e000692999c813c2ea084fe4dfa17c4f08aa05` 同 SHA CI [39782622299](https://github.com/shuaibilx/DecAgent/actions/runs/39782622299) 九个 job 全通过；CI 后复跑预检及机器证据见[报告](../../../evals/benchmarks/evidence/public-first-wave-preflight-20261022-d3e0006.md)。live 批次仍须单独授权。
- 游客与本机交付 change 当前为 22/28；剩余 6 项见其自己的 tasks.md。
- [整理前逐字快照](../../../generated/261022/任务整理/1202_jev任务完整快照_整理前.md)保留本文件的完整原文、所有验收细节和逐次历史。SHA-256：4f623e1ec66314eab4715e33b06794777f49fc2c961504f6421129185960a484。
- 详细证据入口：[实现与工作台](../../../generated/261021/任务整理/1434_实现与工作台证据.md)、[评测基础](../../../generated/261021/任务整理/1434_评测基础证据.md)、[模型与真实评测](../../../generated/261021/任务整理/1434_模型与真实评测证据.md)、[整体验收](../../../generated/261021/任务整理/1434_整体验收证据.md)、[当前计划](../../../generated/261022/0920_后续实施计划与验收顺序.md)。

本清单保留全部任务编号、勾选状态和可执行验收摘要。详细原文与运行历史保存在快照及上述证据页；更新状态时只补短证据链接，不把测试日志重新贴入任务行。

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
- [ ] 9.6.F 接通并验收真实模型/MCP callback、三路线调度、τ 模拟器与官方裁判、最终回复、unknown/评分失败分类和环境清理；unknown 后不得继续派发该路线。2026-10-24 的隔离只读诊断复现了 CSM find_user 输出契约失配：冻结 schema 声明 active:boolean，真实响应以整数编码；已实现严格 0/1 schema-aware 规范化，保留原始 MCP 响应、记录 JSON Pointer、ReAct/Plan 共用规范值，并升级结果契约 v2。转换只遍历内联 properties、schema-valued additionalProperties 和对象 items；不展开 `$ref`/组合关键字，未转换值继续严格校验并失败关闭。回归与真实只读 MCP 集成通过，但不能证明历史 v10 ValueError 由同一原因造成；历史账本未保存调用参数、响应和 traceback，unknown 不重放。四域真实模型/评分闭环仍未验收，见[兼容性诊断](../../../evals/benchmarks/evidence/csm-find-user-output-contract-20261024.md)、[监督与回调回归](../../../evals/integration/evidence/public-cancellable-supervision-20261022.md)及[v10报告](../../../evals/benchmarks/evidence/public-first-wave-live-20261023-v10-9d61688.md)。
- [ ] 9.6.G v10 首波固定的 CSM、ITSM、τ retail、τ telecom 四域八路线均已尝试但验收未通过：3 failed、5 unknown、0 success，仅 τ telecom/ReAct 一条路线评分且为 0.0；清理 8/8 verified。2026-10-24 的 CSM 输出兼容修正只证明隔离只读工具契约路径可用，不改变任何历史路线、评分或账本，也未运行模型。不得重放 unknown；只有新授权批次完成四域评分与成功门槛后才补齐固定 24 例 smoke。仅对自然适用且审核通过的节点做 Jev 配对。
- [x] 9.7 持久化 Jev 累计 1 美元预留/结算/unknown 停止账本；历史费用不可重置。
- [x] 9.8 对计划路线实现证据约束的确定性最终回复及有界 Qwen 总结；总结失败/unknown 受账本约束。

## 10. 真实质量和效率验收

- [x] 10.1 完成主模型 readiness 检查与放行记录；旧 2026-10-18 unknown 不重试。
- [x] 10.2 按每决策族至少 100 个自动样本及 95% 正确率门槛评估阈值资格；样本不足时不选生产阈值、不用 holdout 补样。
- [ ] 10.3 完成冻结四域首波、每域至少一个有效官方评分及清理闭环，然后补齐 24 例 smoke；通过后再决定是否运行 206+80 全量。v10 的 8 条路线均终态且清理 8/8，但 4 条 Plan 路线因 Qwen timeout 为 unknown，CSM/ReAct 也为 unknown；其余 3 条 failed，仅 τ telecom/ReAct 到达官方评分器且得分 0.0。首波门槛未通过，剩余 smoke 与全量暂停。报告任务分母、成功/失败/unknown 与区间；不得冒充全量。
  - v10 实耗已核对：30 Qwen 请求、477,433 计费/预留 token、13 个工具调用；只有一个官方分数且为 0.0，不能外推更大批次资源。新 cohort 的建议硬上限为 100 请求、100 万 token、400 工具调用、2 小时，Qwen 抵扣前成本上限 ¥2.70；该估算待单独授权，未派发。见[下一批资源估算](../../../evals/benchmarks/evidence/next-public-cohort-budget-estimate-20261024.md)。
  - 2026-10-24 离线补齐 cohort 额度冻结：仅 manifest v9+ 可使用低于既有首波最大值的角色及批次硬上限；角色请求/token 配额须精确加总，超上限、布尔值及错误分配均拒绝。默认 v10 manifest 保持完全一致。该代码不创建新 cohort，不重用 v10 账本，也不授权真实派发；定向测试与同 SHA CI 待验收。
- [ ] 10.4 在相同任务、状态、权限、工具及隔离条件下比较 ReAct、一次规划确定性执行和一次规划加 Jev；报告质量、副作用、调用、成本、延迟及配对差异。v10 仅执行 ReAct/Plan 两条路线且没有成功路线，未调用 Jev，也没有可支撑性能比较的配对结果；工具账本记录 2 次已结算且按注册工具元数据标记为 write-effect 的调用，禁用副作用数量未测量。仅自然适用语义节点参与 Jev 比较。
- [ ] 10.5 分开报告标签 oracle、τ 官方模拟器与真实员工交互；冻结 profile、分别计量角色用量，模拟用户不得冒充真实审批者。
- [ ] 10.6 发布带准确分母、统计区间、可复现命令和成本/延迟/自动覆盖图的报告；证据不足时不宣称 Jev 收益。v10 已发布四任务/八路线的人读与机器报告，但仅一条官方评分且为 0.0，另有七条未评分；无法形成完整质量区间或性能图，正式报告待四域评分及后续合格样本。
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
- [x] 10.10.F 安全保存模型/工具/route 诊断；保留未知和失败阶段、不记录敏感正文，清理补录不可重放。后续 ReAct 路径新增更细的 route supervisor、model callback、response validation、τ simulator 和 tool gateway 阶段；one-shot worker 被父级 deadline 终止时，仅通过无锁有界进程间状态保留 HTTP 传输阶段/状态码；内部 `model_call` 不算传输进展，未观察到传输边界时记通用 timeout；v10 历史 timeout 与 `ValueError` 仍不可追溯。

## 11. 整体回归与本机交付

- [x] 11.1 在隔离数据库运行确定性、集成、Docker 边界测试及 Ruff；跳过项单列，不以缺依赖的 skip 冒充通过。
- [x] 11.2 在源码树外验证发行包、MCP wire、资源和恢复/回退入口。
- [x] 11.3 审计需求、场景、测试、实测证据和指南；清理新路径重复逻辑，保留必要兼容，并将独立 Linux 验收列为后续。
