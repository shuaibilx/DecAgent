# Tasks

本文件只保存执行任务、验收要求、短状态与证据入口。源码和 OpenSpec 根均为 DecAgent；源码路径相对项目根目录。测试必须使用隔离数据库，不得读取、重置或迁移正在运行的业务库。

## 当前状态（2026-10-22 更新）

- 本 change 为 **72/79，7 项待完成**；9.6.E、10.10.B、10.10.D、10.10.E 与 11.1 已完成，保留全部任务编号及先前历史证据。其他 change 状态见各自清单。
- 历史 v7 首波为 5 failed、3 unknown；保留原账本且不重放。当前 v8 首波已在候选 `5a21ded64061ce5576b33c58dcec3266615e2a1a` 执行 4 个任务/8 条路线：2 failed、6 unknown、0 成功；两条已评分路线均为 0.0，8/8 清理 verified。结果见[首波报告](../../../generated/261021/2110_公开评测首波报告_v8.md)与[机器证据](../../../evals/benchmarks/evidence/public-first-wave-live-20261021-v8-5a21ded.json)。完整有效公开比较仍为 0/286，不能表述成从未尝试。
- Qwen routed readiness 已通过；旧 $0 不是供应商账单。[平台费率来源与实施说明](../../../generated/261021/模型计费/1441_千问平台费率核验.md)现已绑定到 profile v4 / manifest v8；CNY 费率计算与逐请求 provider bill/voucher/cash 保持分开。账户逐请求 invoice 及 CNY/USD 汇率仍未核实，因此没有可用的跨币种成本优势结论。
- 旧 unknown 不重放、不释放原预留。新 cohort、候选与额度须按现有门禁冻结；正式对照关闭跨模型降级。Jev 仍使用原累计 $1 账本。
- 候选 `5a21ded64061ce5576b33c58dcec3266615e2a1a` 的同 SHA CI run [39600167554](https://github.com/shuaibilx/DecAgent/actions/runs/39600167554) 全部通过，包含 Python 3.11/3.13、PostgreSQL、Workbench、隔离 Docker 边界、wheel/MCP 与 Compose。受控四域工程闭环仍为 176 项通过；真实 v8 首波现已执行，但质量门槛未通过。旧 unknown 不重放。
- 2026-10-22 暴露范围复核已冻结新 manifest v9，SHA-256 `f984ec74c9776efef0b6ba808ff035198500f1997537e01776dc2a895173a604`：只有两个已实测 EnterpriseOps case 标为 `development_exposed`；其他 204 个 EnterpriseOps 与 τ 官方 test 80 个为 `not_exposed`，τ train smoke 属开发数据。该标签不代表独立 holdout，也不能排除预训练污染。v9 first-wave dry-run 通过，4 个固定任务/8 个路线槽位，未创建 ledger、模型或 MCP 调用。v8 及其 unknown 路线保持不变且不能 live dispatch。本轮改动已在本地提交；远端推送与同 SHA CI 因 GitHub HTTPS 连接失败尚未完成，公开真实评测仍未启动。

执行顺序：四域受控工程闭环与候选 CI 已完成 → v8 首波已执行但门槛未通过 → v9 精确案例暴露分组、角色 profile/资源/消融冻结及 dry-run 已完成 → 完成其余本地隔离与恢复门禁并提交新候选 → 同 SHA CI 通过后，再按已批准停止线执行新的受控首波；v8 unknown 不重放。剩余 smoke、自然 Jev 配对和全量暂停，游客与本机交付在独立 change 推进。

[后续规划](../../../generated/261021/计划/1157_后续规划索引.md) · [历史与验收证据导航](../../../generated/261021/任务整理/1434_任务整理索引.md) · [整理前完整原文](../../../generated/261021/任务整理/1434_jev任务原文快照.md)

任务保留文本与原文逐字比对；移出的验收展开和执行记录仍按原编号保存在证据页，相关约束不因分离而取消。原状态、旧测试和旧候选证据不等于本次重新验收；以 design/specs 与适用候选的证据共同核对。

## 1. 基线留存与兼容准备

[本组验收记录](../../../generated/261021/任务整理/1434_实现与工作台证据.md#group-1)

- [x] 1.1 留存当前 business-2 MCP 运行契约、基线入口、相关测试与版本指纹，确认没有依赖已退役 CLI 的基线；交付基线 manifest，并验证同一固定任务可由留存入口执行。
- [x] 1.2 在 evals/integration 的独立数据库夹具中建立新旧任务并存样本，记录图版本和未知操作状态；验证夹具不读取/重置用户业务库。
- [x] 1.3 编写迁移与回退操作说明，列清“旧任务按旧版本恢复、新任务新版本、回退不降级 checkpoint”；用现有 worker claim/check_version 路径逐项核对文档。

## 2. 计划契约、可信模板与规划器

[本组验收记录](../../../generated/261021/任务整理/1434_实现与工作台证据.md#group-2)

- [x] 2.1 新增 business/plans/contracts.py 的版本化计划/节点/绑定/条件/目标证据契约；在 evals/deterministic/test_business_plan_contracts.py 验证直线、分支、类型错误、未知字段和规模限制。
- [x] 2.2 从 business/questions.py 与当前业务契约整理可信模板注册表，固定 Choice/Noul/Score 语义、候选来源、无匹配和尺度；测试拒绝运行时改写说明/档位、空候选及伪造来源。
- [x] 2.3 实现 compiler.py 的依赖、可达性、条件边和合流校验及有界展开；测试未激活分支不阻塞、未来引用/未定义分支/循环/超过 32 节点与深度 4 被拒绝。
- [x] 2.4 更新 bindings.py 的类型化参数解析，`verified_memory` 仅使用稳定 `memory_kind + subject` 槽，并接入最终 MCP schema 校验；测试创建回执 ID→后续分派、可空值、过期候选、缺失/歧义记忆槽、占位 ID 和非法执行员工覆盖。
- [x] 2.5 为现有 models/primary.py 增加 Planner 适配接口、计划 prompt/schema 和最多一次格式修复；用受控响应测试真实请求分类、拒绝非法输出、不生成任意代码及不隐藏内部重试。
- [x] 2.6 实现证据约束的完成判断与默认确定性结果渲染；扩展 test_business_completion_claims.py，验证未知提交/模型自称成功不能完成、合法辅助读取不阻塞目标。
- [x] 2.7 交付计划 schema、可信模板发布规则和参数来源示例文档；示例通过同一编译器校验，包资源测试验证安装后的 schema/模板可读取。

## 3. 持久解释器与新旧任务恢复

[本组验收记录](../../../generated/261021/任务整理/1434_实现与工作台证据.md#group-3)

- [x] 3.1 按下一个可用 SQL 迁移号增加计划修订、节点运行和必要事件字段；验证旧 schema 11 数据可迁移、重复迁移幂等、现有 operation/approval/usage 证据不变。
- [x] 3.2 实现 business/plans/repository.py 的不可变 revision、稳定节点身份、CAS 修订和租约保护；集成测试验证并发修订只有一份生效、过期租约不能推进。
- [x] 3.3 实现 runtime.py 和 business-3 固定运行图，按就绪节点推进分支/合流/参数，持久累计尝试与控制步数；测试已知分支无需 primary、恢复不重置计数、上限后明确等待。
- [x] 3.4 接入依赖正确的 Jev 批量与输入上限，默认关闭推测；测试共享状态合批、同批答案依赖被拒绝、新工具结果后重新构题、未消费旁支不打断以及请求/问题分开计数。
- [x] 3.5 将 governed_write 接到现有 execution/operations/grants 和真实 MCP 客户端；在新节点日志写入前后注入崩溃，验证原 request_key/operation_id 核实、无重复副作用及无盲目重试。
- [x] 3.6 实现局部计划补丁与冻结区校验；测试新事实触发、已提交/待确认/未知节点不可抹除、已准备参数变化需旧操作失效、重规划版本及请求总上限。
- [x] 3.7 修改 worker/图版本分派，隔离 legacy business-2 恢复，新任务默认入口受开关控制；集成测试验证新旧并存、未知版本隔离、回退后新版本任务暂停/兼容恢复。
- [x] 3.8 更新执行状态与恢复文档，交付节点状态图及提交响应丢失时序图；根据崩溃测试记录核对每个恢复分支，保留历史修复回归。

## 4. 预算分离与实际请求记账

[本组验收记录](../../../generated/261021/任务整理/1434_实现与工作台证据.md#group-4)

- [x] 4.1 扩展 storage/usage.py 及配置的 model_family/purpose/节点归属和原子分类额度，默认 primary 目标 3/上限 6、Jev 24、总请求 30；并发预算测试验证每个真实请求计入且不能超额预留。
- [x] 4.2 分离模型 guard 与非模型动作 guard，保留总费用、未知费用、时长、取消、租约及执行上限；扩展 test_model_budget.py 验证 primary 耗尽后合法 Jev/工具继续，整体硬限制仍阻止新动作。
- [x] 4.3 为重试、摘要/最终生成、无效响应和发送状态未知统一记账；测试本地未发送与可能已发送的区别、无效计划仍收费、旧 usage 不被猜造新分类及恢复不释放未知预留。
- [x] 4.4 更新预算配置/错误码与用户说明，记录第 4–6 次 primary 的异常原因；验证配置边界、认证扩额事件和原回执只读核实例外与文档一致。

## 5. 阈值策略与持久人工接管

[本组验收记录](../../../generated/261021/任务整理/1434_实现与工作台证据.md#group-5)

- [x] 5.1 实现 decision_policy.py 的 Choice/Score 原值与 Noul 派生值、策略匹配和分布校验；测试 p=0.96→0.92、p=0.5、等号边界、NaN/越界/异常答案及模型版本漂移。
- [x] 5.2 实现策略发布/最低阈值与任务阈值覆盖，保存版本和生效事件；测试不能降低管理员下限、只允许已验证范围、缺策略不自动 primary fallback。
- [x] 5.3 扩展持久人工待办及认证回复接口，绑定 actor/task/node/revision/state/candidates；集成测试覆盖合法选择、Score 档位、Noul 是/否、无匹配、补充、拒绝和重复提交。
- [x] 5.4 实现原节点恢复前权限/新鲜度重验；测试候选撤销、不同员工、旧 revision、过期回复、服务重启与未响应持续等待均不产生错误副作用。
- [x] 5.5 对齐稳定槽记忆架构：关键决定低分等人、低置信度写入提议不得落库；已发布工作流按槽确定性读取，通用规划不筛选或自动跳过记忆。回归未激活旁支不接管、语义回复不能批准写入、低分提议不保存及无通用 Jev 读取入口。
- [x] 5.6 编写阈值与人工接管指南和两类确认流程图；核对 certainty/正确率/统计区间的说明，使用接口测试记录验证所有等待状态描述。

## 6. 工作记忆与上下文

[本组验收记录](../../../generated/261021/任务整理/1434_实现与工作台证据.md#group-6)

- [x] 6.1 实现 task working memory 投影及可重建事实引用；集成测试验证事实已保存但投影未更新的崩溃恢复，人工更正替代推断但保留原审计。
- [x] 6.2 实现 business/context.py 的受限规划/判断输入，保留冻结节点、外部 Jev 决策、未知结果与必要证据；测试输入预算、必需字段不被截断、推断不冒充已执行、默认无摘要模型请求。
- [x] 6.3 增加独立业务记忆迁移、schema 与员工范围查询，支持确认、去重、版本、TTL 和撤销；集成测试覆盖跨员工隔离、30/7 天默认期限、当前业务事实不得缓存为授权。
- [x] 6.4 实现记忆写入的可信模板筛选及稳定槽读取契约；仅已发布工作流可绑定 `memory_kind + subject`，通用规划不自动检索记忆。测试 Jev 写入判断不能替代证据/本人确认、无效模板零调用、同槽去重及无槽零读取调用。
- [x] 6.5 将 `verified_memory` 绑定改为稳定槽解析，禁止模板保存 memory_id；每次待执行节点按当前员工和 scope 解析唯一有效记录并复核来源/版本/有效期。测试非发布工作流拒绝、缺失/歧义/撤销/过期/scope变化暂停、执行中更新不复用旧缓存、已发生操作不重放及历史 repair memory 语义不变。
- [x] 6.6 更新 memory/context 说明，交付事实—推断—经验与显式记忆槽的数据流图、计划绑定和解析上下文样例；敏感信息和注入文本测试验证不进入权限指令或通用 trace。

## 7. 验证流程发布与复用

[本组验收记录](../../../generated/261021/任务整理/1434_实现与工作台证据.md#group-7)

- [x] 7.1 实现 workflow registry 的草稿/验证/发布/撤销及版本指纹；测试一次成功不能自动发布、无效报告或带历史审批/操作 ID 的模板被拒绝。
- [x] 7.2 实现显式流程与结构化输入匹配、可选可信 Jev 选择、真实参数重绑定；测试不唯一/缺字段/不适用路径，以及同模板不同员工和目录更新后的隔离。
- [x] 7.3 基于当前 create、directory/assign、get/set_priority 命令交付创建工单、目录分派、查看后按需调整优先级三个模板及分支回归；验证正常复用含结果渲染在内 primary_requests=0，异常局部重规划被单独分类。
- [x] 7.4 实现依赖漂移和撤销后的失效与安全边界暂停；测试待确认/未知操作仍能核实，不因模板撤销重放。
- [x] 7.5 编写流程模板编写、验证、发布和失效指南，附冷启动与复用对比流程图；所有示例通过注册表校验和已发布流程回归。

## 8. 工作台和 trace

[本组验收记录](../../../generated/261021/任务整理/1434_实现与工作台证据.md#group-8)

- [x] 8.1 扩展 gateway/business.py 的计划/节点/决策/用量查询及脱敏导出；集成测试验证员工 ACL、CSRF、原员工回复约束和凭据不泄漏。
- [x] 8.2 更新 static/business 页面展示实际路径、未走分支、参数来源、三种确定性和阈值策略；用固定持久任务进行浏览器验收，验证刷新后与服务端一致。
- [x] 8.3 提供语义选择/补充/拒绝、阈值设置和本人记忆管理界面，保留独立具体写入确认；浏览器验证切换员工清理缓存、失效候选提示、断线恢复及重复点击无副作用。
- [x] 8.4 串联 task/plan/node/模型请求/策略/operation/receipt 的事件时间线；故障案例证明能定位重规划原因、未知费用和原操作，验证不依赖外部 trace 服务恢复。
- [x] 8.5 同步当前使用指南和 README 导航，交付架构图与员工/运行时/Jev/MCP 泳道图；按指南在隔离本机实例完成提交、暂停、回复和恢复，验证链接/命令与截图一致。

## 9. 评测数据、统计和基线工具

[本组验收记录](../../../generated/261021/任务整理/1434_评测基础证据.md#group-9)

- [x] 9.1 为 EnterpriseOps-Gym CSM/ITSM 主 benchmark 与 τ-Bench 补充 benchmark 建立可追溯 manifest，固定仓库版本、任务/域 ID、许可、数据库快照、工具/API 版本及容器镜像摘要；验证许可或环境不明时明确阻止正式记分，不擅自重新分发原始数据。
- [x] 9.2 保留两个 benchmark 的官方划分，建立可复现的完整任务清单及资源受限子集规则；按任务族标注流程深度、工具依赖、状态依赖、结构化分支、运行时语义分支、对话澄清和异常恢复，验证答案/目标状态不进入 Agent 输入且未运行任务如实列出。
- [x] 9.3 固定 DecAgent 业务契约专用判断集及至少 40 开发/验证、80 留出流程场景，覆盖中文、全部 primitive、动态候选、权限、故障、人工与记忆；仅作针对性补充，不替代 EnterpriseOps-Gym 主流程成绩；交付标签来源/人工核对清单和场景族隔离检查，未核对项不得标人工验证。
- [x] 9.4 在 `decagent/evaluation/business` 实现语义决策原始预测缓存、离线阈值网格扫描、95%/100 自动样本选择规则及策略候选报告；阈值只适用于当前可信注册表中的语义决策族。
- [x] 9.5 实现 Wilson 区间、任务/组配对 bootstrap、误/漏接管、OOS 误执行及人工交互类型统计；用手算小数据与固定 seed 验证分母/聚类正确。

**9.6 三路线公平比较、公开评测控制面与官方计分闭环**

总项 9.6 只有以下子任务全部满足后才算完成。此前合并描述保留为验收范围；拆分用于准确标记工程闭环与真实任务证据，不降低原要求。

- [x] 9.6.A 复用冻结 business-2 MCP 包，在相同 service-ticket 语义种子、治理条件和隔离数据库下完成 execution-only 历史基线配对；保留 schema 11 与当前 schema 17 的兼容隔离，不修改冻结源码，并核对 seed/case digest、身份/权限、工具和治理摘要。
- [x] 9.6.B EnterpriseOps 每次独立重置后采集私有数据库指纹并调用原 verifier，区分任务失败、环境失败和评分失败；未测中间副作用标 `not_measured`。验收：CSM/ITSM 各对冻结 smoke 任务执行两次新容器/数据库重置，核对稳定指纹、MCP 读取及原始任务 verifier；未执行任何任务动作时，两域 verifier 均返回已评分的预期 `task_failure`，清理均核实成功，私有 SQL/标签不进入模型输入。环境启动失败与评分器失败分类由 `test_business_route_execution.py` 覆盖。
- [x] 9.6.C τ 子进程桥使同一权威环境服务用户模拟器、Agent MCP 工具与状态同步，先持久化完整 `SimulationRun` transcript，再交给原始 official evaluator；ReAct/Plan 均可通过 τ simulator 澄清。
- [ ] 9.6.D 依据公开政策与工具契约审核并冻结自然适用 Jev 模板，配对相同状态下 Jev 与固定通用模型语义判断；结构化条件走规则，无适用场景标 N/A。

  阶段证据：CSM `csm_knowledge_article` 的 Choice 模板已依据 pinned MCP schema 审核并冻结；只在一次 `retrieve_knowledge` 的实际返回数组中选择已发布文章，保留 `no_match`，不授予写权限。它已接入隔离 Evaluation PlanRuntime。当前代码同时提供 opt-in 的 `PublicJevSemanticCallback` 与 `PublicPrimarySemanticCallback`：两者从同一个 `QuestionRegistry` 重建同一模板、版本、候选状态与权限摘要；Qwen 使用单次结构化 function call 返回完整选项分布和选择，仍由相同规范化器校验。`LedgeredModelAdapterFactory` 会按比较路线绑定 Jev 或 primary profile；合并工厂可按 route 分派两种 callback。离线 callback/路由定向测试 37 passed，Ruff 通过；没有模型请求、公开 trial 或业务工具调用。真实同状态配对尚未执行，样本暴露范围与新 cohort 仍待冻结，因此本项保持未完成；不得把历史 v8 unknown route 当作可重放样本。
- [x] 9.6.E 固定公开试次 manifest、暴露组、角色/模型 profile、每试次与全批资源、冷/热复用、显式记忆消融和计时；检查标签与私有评分材料不进入 Agent 输入。v9 manifest SHA-256 `f984ec74c9776efef0b6ba808ff035198500f1997537e01776dc2a895173a604` 保留 286 full / 24 smoke / 4 first-wave 案例，绑定 profile/resource/response digests、200 请求/工具、1M token、30 分钟单路线及 500 请求/5M token/1,600 工具/4 小时批次限制，并冻结 cold/hot reuse、employee-bound verified-memory 消融。逐案例暴露映射与用户确认见 [exposure review](../../../evals/benchmarks/evidence/exposure-review-20261022.md)；v8 未改写。入口对 v7/v8 live dispatch fail closed；v9 first-wave dry-run 精确显示 4 任务/8 路线，ledger/model/MCP 均为 0。现有 Agent 输入边界测试验证公开 prompt/tools 投影不含目标标签和 private scorer；dry-run 不输出 exposure label。定向测试 83 passed，完整 deterministic `1,978 passed, 18 skipped`，全仓 Ruff 与 OpenSpec strict 通过；同 SHA CI 尚待执行。本项完成表示评测设计冻结和输入边界验收，不代表真实评测完成。

  历史说明（保留）：v8 创建时所有 case 均为 `unassigned`，workflow-family 注释使用未审核启发式，不能用于暴露组。该事实仅描述 v8，不适用于新冻结的 v9。

  暴露审阅与 v9 冻结（2026-10-22）：用户确认仅 CSM `task_20260106_161859_265_00364ece_0e836ce8` 与 ITSM `task_20260115_201951_323_391f1eff_659b9dae` 进入项目实测；其余 204 个 EnterpriseOps case 仅以 ID 出现在 inventory/manifest/dry-run，用户未在项目外查看任务内容。τ 官方 test 80 条也未在项目外查看，项目内调试使用 train。v9 按精确 case 分组且不按未经审核的 workflow-family 启发式扩散。`not_exposed` 不代表独立 holdout。审计依据、digest 与“不启动实测”边界见 [exposure review](../../../evals/benchmarks/evidence/exposure-review-20261022.md)；历史 v8 manifest 和 trial ledger 不变。

- [ ] 9.6.F 接通真实模型与工具 callback、三路线调度、官方用户模拟器/裁判、最终回复、评分错误分类、未知状态处理和环境清理；Qwen unknown 终结当前 route，Jev unknown 暂停全局新增 Jev 请求。
- [ ] 9.6.G 完成四域首批 8 个 ReAct/Plan 路线试次及每域官方评分/清理闭环，再补齐固定 24 例 smoke；只有自然适用且已审核的语义节点可增加 Jev 配对。
- [x] 9.7 实现累计 Jev 1 美元账本预留、读取和未知费用停止机制，并保留主模型请求/token；测试历史费用不可重置、零额/超额/未知预留/中途停止后不能继续及恢复前置检查。
- [x] 9.8 为公开计划路线实现实际工具证据的确定性最终回复与一次受控 Qwen 总结回调，冻结并核对输出策略摘要；测试无总结的零调用、真实来源引用、超过输出/输入限制、未知写入拒绝、错误/未知总结用量停止、与 ReAct 的公平配置和官方评分隔离。总结计入 Agent primary 与执行时间，不伪造 goal.description，不删除 response_check 任务；真实模型门禁通过前仅使用离线回调验证。

## 10. 真实质量和效率验收

[本组验收记录](../../../generated/261021/任务整理/1434_模型与真实评测证据.md#group-10)

- [x] 10.1 完成主模型就绪诊断与放行决策（勾选表示检查流程已执行，不表示模型通过）：保留 `qwen3.8-flash`，最多使用 3 次获准的新请求且包含一次格式修复；记录每次请求和账本，超时或派发/用量未知立即停止，不切换模型。2026-10-18 的 unknown 请求不重试。
- [x] 10.2 完成冻结数据的阈值资格诊断与决策：只有经审核的验证数据具备每决策族至少 100 个自动样本时才执行阈值选择；不满足时如实保留 95% 正确率目标、报告无合格策略且不使用 holdout 补样。当前 synthetic auxiliary 数据最高每族 40 个工作流样本、20/10 个记忆样本，已证实不具备资格，因此本轮不产生生产阈值。
- [ ] 10.3 先执行四域首波（CSM、ITSM、τ retail、τ telecom 各 1 条；ReAct/Plan 共 8 个路线试次），通过真实模型、工具、原官方计分及清理闭环后，再补齐 24 例固定 smoke；每域至少完成一个官方计分流程并通过评分错误处理检查。门槛通过后才运行 EnterpriseOps 全部 206 条和冻结 τ official test 80 条。固定任务、模板、模型、策略和快照；提交每域/暴露组/未暴露组/任务族的分子分母和区间。模型或资源不足时保留准确未运行清单，不冒充全量或实测结果。DecAgent 自有数据只作为辅助回归，不进入公开基准主分数或阈值资格。10.9 routed readiness 已通过；用户确认的 v8 首波上限已执行；当前首波报告未满足门槛，10.3 保持未完成。

  首波实测（2026-10-21，候选 `5a21ded64061ce5576b33c58dcec3266615e2a1a`）：8 条路线全部终态，2 条失败且经官方评分为 `0.0`，6 条 unknown，成功 0，清理 8/8 verified。CSM 与 τ retail 未获官方分数；Qwen/模拟器 26 次请求中 5 次用量 unknown（252,369 Token 预留），不重放。主模型/模拟器共 182,591 已知 Token；按冻结 profile 的已知成本上界 ¥0.1583867，unknown 预留约 ¥0.2797192，invoice/voucher/cash 不完整。用户确认的批次费率推导抵扣前上界约 ¥13.50（5,000,000 Token × 最高冻结费率 ¥2.7/M，不是供应商账单）。实际批次消费 434,960/5,000,000 预算 Token、26/500 请求、9/1,600 工具调用、约 449 秒/4 小时。详见[首波报告](../../../generated/261021/2110_公开评测首波报告_v8.md)。

  后续独立 Qwen 批次的用户选择上限同为 500 次请求、5,000,000 Token、1,600 次工具调用和 4 小时；角色上限为 Agent/模拟器/裁判 300/160/40 次请求、3,000,000/1,600,000/400,000 Token。¥13.50 是按冻结最高费率计算的抵扣前上界，不是账单。该预算尚未写入新 manifest 或批次账本；不改变 v8 的消费/unknown 记录，不重放 v8，也不代表样本暴露审阅已完成。须先确定新 cohort 暴露标记并通过其余门禁，才可派发新路线。

  首波后离线取证（2026-10-21）：固定 CSM 镜像中 `find_user` 的字节码实现为参数化 `SELECT`；实际隔离容器调用前后数据库指纹相同，清理通过。由于上游 schema 没有 `readOnlyHint`，原路线时评测目录按默认策略将它分类为写操作；现新增仅对精确镜像和 schema digest 生效的评测只读覆盖，漂移仍默认为写。原路线缺少工具回执，仍为 `unknown`、未评分且不重放；不能据此次审计补造回执或改变 v8 终态。容器集成文件 `5 passed`，离线完成，不含模型调用。

  修复候选预检（2026-10-21，HEAD `36c664944d378b5deee26e9feac8b87280ce4739`）：对冻结 v8 first-wave 运行 `run_public_trials.py --mode smoke --phase first-wave --check-images`，选中原固定 4 任务/8 路线槽位，manifest/profile/source/inventory 摘要匹配，镜像存在；`ledger_created=false`、模型调用 0、MCP 调用 0。证据 [`public-evaluation-first-wave-preflight-20261021-36c6649.json`](../../../evals/benchmarks/evidence/public-evaluation-first-wave-preflight-20261021-36c6649.json)。这是离线预检，不是新 cohort、路线尝试或评分；v8 unknown 未重放。候选同 SHA GitHub Actions [CI 39611406922](https://github.com/shuaibilx/DecAgent/actions/runs/39611406922) 已全部通过，耗时约 27 分钟。当前工作树有后续未提交文档改动，不能直接作为 live candidate；新批次仍需冻结干净候选并单独授权。
- [ ] 10.4 在冻结公开任务上执行 ReAct + Skill + MCP、一次规划 + 确定性执行器 + MCP、一次规划 + 确定性执行器 + Jev 语义判断三条路线及预注册消融。主质量比较每路线相同上限：200 Agent 模型请求、200 工具调用、1,000,000 token、30 分钟 Agent 机器时间（τ 另遵守官方 200 步上限）；生产预算作为独立副评。所有路线同任务/状态/权限/确认，逐试次重置；统计任务成功、错误副作用、模型请求/成本/延迟及配对差异；只有 Jev 相比同状态 Qwen 二次判断质量可接受且成本或延迟改善时才确认 Jev 收益，结构化代码规则不作为 Jev 的竞争对象。
- [ ] 10.5 分开执行标签 oracle、τ-Bench 官方用户模拟器与原员工真实交互样本，至少覆盖三种 primitive、无匹配、补充、拒绝和独立写入确认；模拟器和裁判使用本轮冻结通用模型 profile，分别记录角色/提供方/模型/调用/token/延迟/费用；正式对照关闭跨模型降级，不能使用 τ 上游默认 GPT/Claude。员工研究未完成时明确保留待办，不把模拟用户当真实审批者。
- [ ] 10.6 发布实测报告、自动覆盖—错误率、阈值—接管、模型调用/成本/延迟图和可复现命令；逐项标通过/未达标/证据不足，区间不支持时不宣称提速，不以报告存在替代未通过验收。

- [x] 10.7 执行 2026-10-19 新授权的 Qwen 离线定位与有界就绪复验：量化请求规模，区分可复现实现缺陷与未保存旧响应的未知原因；失败回归先行并修复，通过后以 `qwen-readiness-20261019-plan-execution-v1` 持久计数最多 3 次全新只读请求（最多一次格式修复）。正常返回缺 usage 同样停止；unknown/崩溃不得重置、重发或归零。保存实际编译结果、用量、诊断与门禁证据；未通过则保持 10.3/10.4 未放行。本项不重写 10.1 历史结论，Jev 累计预算不变。

**10.8 版本化通用模型 profile 与记账**

- [x] 10.8.A 为规划、结果回复、通用模型语义对照、τ 用户模拟器和裁判冻结完整角色 profile：采样参数、输出/工具选择契约、超时、provider routing、token/费用边界，并显式列出 unsupported 参数。
- [x] 10.8.B 保留 Qwen 主端点与固定 OpenRouter `deepseek/deepseek-v4.1-flash` 备用配置，通过现有 Jev OpenRouter key 引用凭证；每次真实尝试独立记录 requested/resolved provider/model、request/generation IDs、usage、费用、时长和安全失败类别，不记录 secret。
- [x] 10.8.C 验证 Qwen 瞬时失败最多两次、之后 DeepSeek 最多一次，未知预留占用原预算，正式对照关闭跨模型降级，模型/路由漂移建立独立实验组；未知 MCP 写入不重放。
- [x] 10.8.D 新建 Qwen 计费 profile v4 和公开 manifest v8，绑定用户确认适用于当前 endpoint 的官方标准 CNY 费率及来源/版本；普通输入 ¥0.8/M、输出 ¥2.7/M、缓存命中输入 ¥0.1/M。缓存 Token 可核验时精确计算，缺失时按普通输入费率给出 `frozen_rate_upper_bound`。账单快照分开报告 provider-billed gross/voucher/cash 与冻结费率计算值，均按原币保存；未获得的 invoice/voucher/cash 和未核验汇率保持缺失，不作估算。v3/v7 旧 profile、manifest、Jev 指纹及历史账本未改写；usage/cost completeness 独立。live report 升至 v3；全量 deterministic `1955 passed, 18 skipped`，Ruff 与 OpenSpec strict 通过。详见 [模型与真实评测证据](../../../generated/261021/任务整理/1434_模型与真实评测证据.md#task-10-8-d) 与 [费率来源核验](../../../generated/261021/模型计费/1441_千问平台费率核验.md)。
- [x] 10.9 实现全新 readiness 批次 `primary-readiness-routed-v1`：保留全部旧账本且不自动执行未结 attempt；新增批次总计最多 8 次请求、300,000 input+output tokens、DeepSeek $1，单请求 65,536 输入/8,192 输出。先测试 Qwen 最小完整响应/usage，再最小工具 schema，再生产只读 plan 编译；需要时按同一有界政策转 DeepSeek；有额度再测试流式。每个 attempt 记录 endpoint、model、role、阶段、attempt序号、耗时、状态码/安全错误类、usage、费率版本、reserved/actual费用与 unknown；no retries beyond per-step caps，格式修正最多一次，不调用业务 MCP 工具。只有至少一个模型返回 usage 完整并通过生产编译器的只读计划才算 readiness 通过。实测请求必须来自新的明确授权批次，超预算不派发。

**10.10 公开试次 runner、预算和恢复安全**

- [x] 10.10.A dry-run 校验冻结 manifest/source/inventory/profile/readiness/镜像并报告准确任务与路线分母，默认不创建 ledger、不调用模型/MCP，证据只写新文件。v5 smoke/full preflight 分母保持 24/286、48/572 route slots；v6/v7 使用明确 unknown 的 Qwen profile，新的 smoke preflight 为 24 个任务、48 个路线槽位、首波 4 个任务/8 个路线槽位，模型/MCP 调用与 ledger 创建均为 0。v7 同时冻结首波批次请求、token、工具和墙钟上限。phase 离线选择现支持 `first-wave`（4/8，冻结顺序）、`remaining-smoke`（20/40，排除首波）和 `full`（286/572）；所有结果同时报告所选分子与完整 cohort 分母，选择操作不创建 ledger、不派发模型或工具。
- [x] 10.10.B 固定 cohort、候选、profile 和批次身份；对 Agent/模拟器/裁判落实持久请求、Token、机器时间和逐请求费用账目，Jev attempt 绑定唯一既有 `.decagent/business-eval/global-jev-ledger.json`，切换路线状态库、恢复进程或候选不能重置累计 Jev 余额。隔离账本/计费/恢复回归 85 passed；详情见[验收证据](../../../generated/261021/任务整理/1434_模型与真实评测证据.md#task-10-10-b)。Qwen 费用按冻结 CNY profile 逐次计量且不作美元换算。用户确认的 v8 停止线为 500 次请求、5,000,000 Token、1,600 次工具调用及 4 小时；按最高冻结单价推导的抵扣前费用上界约 ¥13.50，不视为供应商账单。
- [x] 10.10.C 加入单一活动执行者租约、heartbeat 与 fencing generation；跨进程 resume 只能在 lease 过期后接管，旧 owner 不能再写入/dispatch，unknown/completed route 不 replay。
- [x] 10.10.D runner 已接入规范 supervisor/SQLite ledger、ledgered 模型回调、MCP gateway、route/batch deadline、官方 scorer 与 cleanup；初验收 149 passed、14 skipped（τ runtime 未配置）是历史快照。随后固定 τ runtime 达到 80/80 readiness，包含 EnterpriseOps environment/scoring、四域 route/session、τ process、live runner、账本、cancel 与 supervisor 的定向组 216 passed、0 skipped（2026-10-21）；证据见[验收记录](../../../generated/261021/任务整理/1434_模型与真实评测证据.md#task-10-10-d)。该结果仅证明工程接线与隔离测试，不是模型质量或公开分数。
- [x] 10.10.E 完成公开 runner 的并发/预算/unknown 恢复与故障回归，以及当前候选四域受控工具、官方评分和 cleanup 工程闭环。固定候选 HEAD `3a7938fd1b3e7a564de6310fc171d1be8f8adfdc` 的受控复验：EnterpriseOps CSM/ITSM 模块 4 passed（含 ReAct 真实只读 MCP 工具、原 verifier 与隔离清理）；τ retail/telecom 的 ReAct/Plan 4 passed（固定 train、原始 evaluator、受控 simulator/judge）；账本、supervisor、runner 与离线 verifier 回归 93 passed；清理失败和 τ 超时/取消回归 75 passed；共 176 个不同测试用例通过，修改测试的 Ruff 通过。没有 Qwen、OpenRouter DeepSeek 或 Jev 请求；结果只证明工程闭环，不是公开 benchmark 成绩。新增 EnterpriseOps route integration test 已包含在候选 `5a21ded64061ce5576b33c58dcec3266615e2a1a` 的同 SHA CI [39600167554](https://github.com/shuaibilx/DecAgent/actions/runs/39600167554)，所有 job 通过。该候选随后执行 v8 首波 8 条真实路线，2 failed、6 unknown、0 成功，8/8 清理验证；这次质量结果属于 10.3，未满足四域评分门槛。详见[受控复验记录](../../../evals/benchmarks/evidence/public-four-domain-engineering-closure-20261021-3a7938fd.json)、[真实首波报告](../../../generated/261021/2110_公开评测首波报告_v8.md)和[验收证据](../../../generated/261021/任务整理/1434_模型与真实评测证据.md#task-10-10-e)。
- [x] 10.10.F 在已派发的模型、工具与 route attempt 记录可审计但不泄露业务内容的诊断：失败阶段必须来自白名单，异常类型只保存类名，可用 HTTP 状态须有效；包含已知用量但响应契约失败的 settled model attempt；attempt 快照和新报告只导出安全白名单；旧账本以 nullable 列兼容迁移，unknown 清理补录保留原诊断且不可重放。2026-10-21 一致性补充已用真实 SQLite 回归验证（84 项定向、全量 deterministic 1970 passed/18 skipped，Ruff 通过），详情见[10.10.F 验收证据](../../../generated/261021/任务整理/1434_模型与真实评测证据.md#task-10-10-f)。

  首波后补充诊断：one-shot primary worker 的 `target`/`response` 失败阶段映射到白名单值，worker 异常类型仅保留经校验的类名；HTTP 超时等无 worker stage 的异常保持通用阶段。测试覆盖安全属性持久化和拒绝任意 stage/type。本次定向确定性回归 `142 passed`，完整 `evals/deterministic` 为 `1969 passed, 18 skipped (213.44s)`，隔离 EnterpriseOps 容器集成为 `5 passed`，全仓 Ruff 与 OpenSpec strict 均通过；跳过项如实保留，未发出任何模型请求，旧 unknown 不重放。

## 11. 整体回归与本机交付

[本组验收记录](../../../generated/261021/任务整理/1434_整体验收证据.md#group-11)

- [x] 11.1 在隔离数据库运行确定性与集成套件，包括旧 repair/business-2 和 MCP 现代/兼容协议、取消/租约/确认/未知回执；运行项目 ruff 检查并保存摘要，缺数据库造成 skip 不能当集成通过。本机复验：确定性 1,969 passed、4 skipped；隔离 PostgreSQL 集成 630 passed、24 skipped；Ruff 与 OpenSpec strict 通过。同 SHA GitHub Actions run [39592249617](https://github.com/shuaibilx/DecAgent/actions/runs/39592249617) 全部通过：Python 3.11/3.13 各 Ruff 通过、确定性 1,963 passed/18 skipped、PostgreSQL 集成 624 passed/30 skipped；独立隔离 Docker/SQL job 21 passed，另有 wheel/MCP、Compose、Workbench 通过。最新候选 `5a21ded64061ce5576b33c58dcec3266615e2a1a` 的 run [39600167554](https://github.com/shuaibilx/DecAgent/actions/runs/39600167554) 也全部通过，包含新增 EnterpriseOps route integration test。PostgreSQL 服务健康；9 个可选/平台限定集成 skip 已记录，不是缺数据库造成。证据见[整体回归记录](../../../generated/261021/任务整理/1434_整体验收证据.md#task-11-1)。

- [x] 11.2 构建发行包并在源码目录外验证新增 schema/模板/静态资源和启动入口；本机真实 MCP wire 场景验证新旧版本恢复与回退入口，确认没有重新引入御龙真实后端依赖。
- [x] 11.3 逐项核对需求—场景—测试—实测证据与指南，移除新业务默认路径上失效的重复队列逻辑但保留必要旧任务兼容；交付当前完成/待办清单，独立 Linux 部署仍标后续工作。

## Workflow follow-up

- 本 change 已获用户 apply 授权；按未完成任务继续实施并记录验证证据。
- 本 change 完成并经请求归档时再同步主规格；不要在实施中替本用户隐式归档历史 change。
- 独立 Linux 主机部署/验收、Pi 实现、真实公司 API/SSO 属于后续需求，不因本 change 文档完成而宣称已交付。
- 以后只更新相关任务状态、当前短状态和证据链接；逐次日志进入独立记录，独立新功能使用独立 change。
