# Tasks

本文件是 apply 的执行清单，已完成项以勾选及对应验证证据为准。源码与 OpenSpec 根均为 DecAgent 项目根目录，以下源码路径相对该目录。一般说明文档放入 DecAgent/generated/YYMMDD/HHmm_主题.md，更新既有文档时保留原路径。不得用运行中的业务数据库做测试。

默认执行顺序：1 → 2 → 3 → 4 → 5 → 6/7 → 8 → 10 → 11。第 9 组的数据冻结与工具实现可在第 2 组完成后并行开展，第 10 组依赖 5–9。每个编号均包含完成证据；性能任务只在条件满足时勾选。

## 历史执行快照（2026-10-20，首波 live 执行前）

任务总数 78，已完成 66，剩余 12：9.6.D–G、10.3–10.6、10.8.D、10.10.B、10.10.D–E。固定首波为 4 个任务、8 个 ReAct/Plan 路线槽位；离线清单、画像、readiness 与本地镜像预检通过。受门禁保护的 first-wave CLI 和四任务环境预检现已接通，但当前代码工作树不干净，精确候选 SHA 门禁阻止 live dispatch；本轮未创建 live ledger 或派发模型/MCP 请求。新增 4 条 EnterpriseOps 脚本路线到达原始评分与清理，四条均为脚本只读查询后的 0.0 `task_failure`，不计真实首波或公开 benchmark；τ Plan retail/telecom 使用各自真实只读工具，经原始评分与清理的锁定 worker smoke 为 2 passed，τ ReAct simulator/original-scorer smoke 为 2 passed。τ bridge 现拒绝不接受 `timeout_seconds` 的同步模型 callback，用户模拟器/裁判上限通过请求级 timeout cap 生效，但不能强制中止忽略 timeout 的任意 Python callback。公开 benchmark 仍为 0/286；`guest-sop-local-trial` 的 28 项任务尚未开始。9.6.B 已完成 CSM/ITSM 原始 verifier 工程验收，9.6.C 已完成 τ 固定 train smoke；这些工程闭环均不代表 Agent 成功执行或质量结果。最新全量确定性套件为 `1899 passed, 17 skipped (259.61s)`；7 个核心 PostgreSQL/MCP/预算/路由集成文件 `80 passed, 0 skipped`；Python 3.11/3.13 关键文件各 37 passed。证据分别见 `postgres-core-integration-20261020.json` 与 `python-matrix-smoke-20261020.json`。Ruff、OpenSpec strict、wheel/sdist build 和 CLI 入口通过。全量集成与核心浏览器 E2E 未验收；跳过项不算通过，也不等同远端 CI。下方更早进度为历史记录，以本快照及其后更新为准。

本轮新增 τ callback deadline 契约回归：旧实现下不带 `timeout_seconds` 的同步 callback 会被调用（新测试先复现）；现于 callback 执行前拒绝并关闭 worker。用户模拟器和裁判改为传递 90/120 秒单请求 timeout cap，与 route/batch deadline 取最小值，避免竞争请求继承临时改写的共享 channel timeout。六个相关 τ/公开执行/账本/模型派发/live-runner 文件共 `127 passed, 14 skipped`；全量确定性套件 `1899 passed, 17 skipped (259.61s)`。该修正不完成通用同步函数取消，不改变任何任务勾选，也没有模型、业务工具或评分器请求。

## 当前执行状态（2026-10-20，首波 live 执行后）

OpenSpec 勾选数仍是 `66/78`；本次真实试次没有满足成功闭环验收，因此没有勾选 9.6.F/G、10.3、10.4、10.10.D/E。候选 SHA `9bc3ee6654e46630c9ae8238a28eb6e34a088698` 的 GitHub CI run `39442012550` 全部通过，固定四域首波已实际进入规范账本，共派发 8 条 ReAct/Plan 路线。机器结果见 `evals/benchmarks/evidence/public-first-wave-live-20261020.json`，人类可读总结见 `evals/benchmarks/evidence/public-first-wave-live-summary-20261020.md`。

8 条路线状态为 4 unknown、4 failed、0 completed；8 条清理均 verified，8 条官方评分均为 `not_attempted`。CSM 的 `find_user` 工具调用为 unknown，工具目录 effect 标记为 `write`；不得据 cleanup verified 推断无业务副作用。Qwen 共登记 4 次请求，1 次用量可核实为 6,952 tokens，另外 3 次 usage unknown；没有供应商账单数据，任何一次都不能把费用记作 `$0`。Jev 与 judge 调用均为 0。unknown 路线不重试，另外 40 条 pending smoke 路线也不在本批次继续派发。

当前错误日志只保留 `model_dispatch_error`、`tool_dispatch_error`、`agent_failure`、`environment_failure` 这类分类，没有可判定异常阶段的诊断字段；不能离线归因网络、配置或供应商问题。后续真实批次须先完成离线根因定位与不含 prompt、响应正文或密钥的安全诊断增强，再重新冻结试次身份和额度。旧快照中的“live 尚未派发/仍为 0 route attempts”仅描述首波执行前状态，不代表当前状态。

## 1. 基线留存与兼容准备

- [x] 1.1 留存当前 business-2 MCP 运行契约、基线入口、相关测试与版本指纹，确认没有依赖已退役 CLI 的基线；交付基线 manifest，并验证同一固定任务可由留存入口执行。
- [x] 1.2 在 evals/integration 的独立数据库夹具中建立新旧任务并存样本，记录图版本和未知操作状态；验证夹具不读取/重置用户业务库。
- [x] 1.3 编写迁移与回退操作说明，列清“旧任务按旧版本恢复、新任务新版本、回退不降级 checkpoint”；用现有 worker claim/check_version 路径逐项核对文档。

## 2. 计划契约、可信模板与规划器

- [x] 2.1 新增 business/plans/contracts.py 的版本化计划/节点/绑定/条件/目标证据契约；在 evals/deterministic/test_business_plan_contracts.py 验证直线、分支、类型错误、未知字段和规模限制。
- [x] 2.2 从 business/questions.py 与当前业务契约整理可信模板注册表，固定 Choice/Noul/Score 语义、候选来源、无匹配和尺度；测试拒绝运行时改写说明/档位、空候选及伪造来源。
- [x] 2.3 实现 compiler.py 的依赖、可达性、条件边和合流校验及有界展开；测试未激活分支不阻塞、未来引用/未定义分支/循环/超过 32 节点与深度 4 被拒绝。
- [x] 2.4 更新 bindings.py 的类型化参数解析，`verified_memory` 仅使用稳定 `memory_kind + subject` 槽，并接入最终 MCP schema 校验；测试创建回执 ID→后续分派、可空值、过期候选、缺失/歧义记忆槽、占位 ID 和非法执行员工覆盖。证据：`test_business_plan_bindings.py`、`test_business_workflow_contracts.py` 与 `test_business_memory_gateway.py` 稳定槽/绑定测试通过。
- [x] 2.5 为现有 models/primary.py 增加 Planner 适配接口、计划 prompt/schema 和最多一次格式修复；用受控响应测试真实请求分类、拒绝非法输出、不生成任意代码及不隐藏内部重试。
- [x] 2.6 实现证据约束的完成判断与默认确定性结果渲染；扩展 test_business_completion_claims.py，验证未知提交/模型自称成功不能完成、合法辅助读取不阻塞目标。
- [x] 2.7 交付计划 schema、可信模板发布规则和参数来源示例文档；示例通过同一编译器校验，包资源测试验证安装后的 schema/模板可读取。

## 3. 持久解释器与新旧任务恢复

- [x] 3.1 按下一个可用 SQL 迁移号增加计划修订、节点运行和必要事件字段；验证旧 schema 11 数据可迁移、重复迁移幂等、现有 operation/approval/usage 证据不变。
- [x] 3.2 实现 business/plans/repository.py 的不可变 revision、稳定节点身份、CAS 修订和租约保护；集成测试验证并发修订只有一份生效、过期租约不能推进。
- [x] 3.3 实现 runtime.py 和 business-3 固定运行图，按就绪节点推进分支/合流/参数，持久累计尝试与控制步数；测试已知分支无需 primary、恢复不重置计数、上限后明确等待。
- [x] 3.4 接入依赖正确的 Jev 批量与输入上限，默认关闭推测；测试共享状态合批、同批答案依赖被拒绝、新工具结果后重新构题、未消费旁支不打断以及请求/问题分开计数。
- [x] 3.5 将 governed_write 接到现有 execution/operations/grants 和真实 MCP 客户端；在新节点日志写入前后注入崩溃，验证原 request_key/operation_id 核实、无重复副作用及无盲目重试。
- [x] 3.6 实现局部计划补丁与冻结区校验；测试新事实触发、已提交/待确认/未知节点不可抹除、已准备参数变化需旧操作失效、重规划版本及请求总上限。
- [x] 3.7 修改 worker/图版本分派，隔离 legacy business-2 恢复，新任务默认入口受开关控制；集成测试验证新旧并存、未知版本隔离、回退后新版本任务暂停/兼容恢复。
- [x] 3.8 更新执行状态与恢复文档，交付节点状态图及提交响应丢失时序图；根据崩溃测试记录核对每个恢复分支，保留历史修复回归。

## 4. 预算分离与实际请求记账

- [x] 4.1 扩展 storage/usage.py 及配置的 model_family/purpose/节点归属和原子分类额度，默认 primary 目标 3/上限 6、Jev 24、总请求 30；并发预算测试验证每个真实请求计入且不能超额预留。
- [x] 4.2 分离模型 guard 与非模型动作 guard，保留总费用、未知费用、时长、取消、租约及执行上限；扩展 test_model_budget.py 验证 primary 耗尽后合法 Jev/工具继续，整体硬限制仍阻止新动作。
- [x] 4.3 为重试、摘要/最终生成、无效响应和发送状态未知统一记账；测试本地未发送与可能已发送的区别、无效计划仍收费、旧 usage 不被猜造新分类及恢复不释放未知预留。
- [x] 4.4 更新预算配置/错误码与用户说明，记录第 4–6 次 primary 的异常原因；验证配置边界、认证扩额事件和原回执只读核实例外与文档一致。

## 5. 阈值策略与持久人工接管

- [x] 5.1 实现 decision_policy.py 的 Choice/Score 原值与 Noul 派生值、策略匹配和分布校验；测试 p=0.96→0.92、p=0.5、等号边界、NaN/越界/异常答案及模型版本漂移。
- [x] 5.2 实现策略发布/最低阈值与任务阈值覆盖，保存版本和生效事件；测试不能降低管理员下限、只允许已验证范围、缺策略不自动 primary fallback。
- [x] 5.3 扩展持久人工待办及认证回复接口，绑定 actor/task/node/revision/state/candidates；集成测试覆盖合法选择、Score 档位、Noul 是/否、无匹配、补充、拒绝和重复提交。
- [x] 5.4 实现原节点恢复前权限/新鲜度重验；测试候选撤销、不同员工、旧 revision、过期回复、服务重启与未响应持续等待均不产生错误副作用。
- [x] 5.5 对齐稳定槽记忆架构：关键决定低分等人、低置信度写入提议不得落库；已发布工作流按槽确定性读取，通用规划不筛选或自动跳过记忆。回归未激活旁支不接管、语义回复不能批准写入、低分提议不保存及无通用 Jev 读取入口。
- [x] 5.6 编写阈值与人工接管指南和两类确认流程图；核对 certainty/正确率/统计区间的说明，使用接口测试记录验证所有等待状态描述。

## 6. 工作记忆与上下文

- [x] 6.1 实现 task working memory 投影及可重建事实引用；集成测试验证事实已保存但投影未更新的崩溃恢复，人工更正替代推断但保留原审计。
- [x] 6.2 实现 business/context.py 的受限规划/判断输入，保留冻结节点、外部 Jev 决策、未知结果与必要证据；测试输入预算、必需字段不被截断、推断不冒充已执行、默认无摘要模型请求。
- [x] 6.3 增加独立业务记忆迁移、schema 与员工范围查询，支持确认、去重、版本、TTL 和撤销；集成测试覆盖跨员工隔离、30/7 天默认期限、当前业务事实不得缓存为授权。
- [x] 6.4 实现记忆写入的可信模板筛选及稳定槽读取契约；仅已发布工作流可绑定 `memory_kind + subject`，通用规划不自动检索记忆。测试 Jev 写入判断不能替代证据/本人确认、无效模板零调用、同槽去重及无槽零读取调用。
- [x] 6.5 将 `verified_memory` 绑定改为稳定槽解析，禁止模板保存 memory_id；每次待执行节点按当前员工和 scope 解析唯一有效记录并复核来源/版本/有效期。测试非发布工作流拒绝、缺失/歧义/撤销/过期/scope变化暂停、执行中更新不复用旧缓存、已发生操作不重放及历史 repair memory 语义不变。
- [x] 6.6 更新 memory/context 说明，交付事实—推断—经验与显式记忆槽的数据流图、计划绑定和解析上下文样例；敏感信息和注入文本测试验证不进入权限指令或通用 trace。

## 7. 验证流程发布与复用

- [x] 7.1 实现 workflow registry 的草稿/验证/发布/撤销及版本指纹；测试一次成功不能自动发布、无效报告或带历史审批/操作 ID 的模板被拒绝。
- [x] 7.2 实现显式流程与结构化输入匹配、可选可信 Jev 选择、真实参数重绑定；测试不唯一/缺字段/不适用路径，以及同模板不同员工和目录更新后的隔离。
- [x] 7.3 基于当前 create、directory/assign、get/set_priority 命令交付创建工单、目录分派、查看后按需调整优先级三个模板及分支回归；验证正常复用含结果渲染在内 primary_requests=0，异常局部重规划被单独分类。
- [x] 7.4 实现依赖漂移和撤销后的失效与安全边界暂停；测试待确认/未知操作仍能核实，不因模板撤销重放。
- [x] 7.5 编写流程模板编写、验证、发布和失效指南，附冷启动与复用对比流程图；所有示例通过注册表校验和已发布流程回归。

## 8. 工作台和 trace

- [x] 8.1 扩展 gateway/business.py 的计划/节点/决策/用量查询及脱敏导出；集成测试验证员工 ACL、CSRF、原员工回复约束和凭据不泄漏。
- [x] 8.2 更新 static/business 页面展示实际路径、未走分支、参数来源、三种确定性和阈值策略；用固定持久任务进行浏览器验收，验证刷新后与服务端一致。证据：`business_trace_rendering.cjs` 覆盖无策略时三种指标说明和有策略时阈值控件，`business_selection.cjs` 验证刷新/取消/恢复/预算请求乱序不抢占新任务；`test_business_trace_gateway.py` 13 项集成通过。隔离 PostgreSQL 固定任务由 Playwright CLI 登录员工后验证实际进入 `directory`、`read_ticket` 未走、参数来自 `task_input`；点击刷新并完整重载页面后重新选择任务，内容仍与服务端一致。截图：`output/playwright/conditional-workbench/.playwright-cli/page-2026-10-17T09-34-33-077Z.png`。本次 0 次模型请求。
- [x] 8.3 提供语义选择/补充/拒绝、阈值设置和本人记忆管理界面，保留独立具体写入确认；浏览器验证切换员工清理缓存、失效候选提示、断线恢复及重复点击无副作用。验收：`business_workbench_intervention_fixture.py` 仅创建临时 PostgreSQL 子库、合成员工/任务并监听 `127.0.0.1:8855`，模型请求为 0。Playwright CLI 验证员工切换后旧任务/审批/记忆不可见；候选过期后旧选择不采用；没有稳定记忆槽时仍可确认、修正、撤销个人记忆；策略从 0.90 改到 0.95 后 trace 即时回显修订 1；断线/刷新后重选原待办可恢复；同步双击延迟回复只有 1 次网络请求。新增 `business_semantic_workbench.cjs` 覆盖旧候选提示、重复点击和无槽记忆卡片；定向策略/trace 集成 `29 passed`，全量回归见 11.1。截图与图解见 `generated/261017/1905_工作台使用与隔离验收.md`。所有行为验证使用合成数据，不构成模型质量或生产环境结论。
- [x] 8.4 串联 task/plan/node/模型请求/策略/operation/receipt 的事件时间线；故障案例证明能定位重规划原因、未知费用和原操作，验证不依赖外部 trace 服务恢复。
- [x] 8.5 同步当前使用指南和 README 导航，交付架构图与员工/运行时/Jev/MCP 泳道图；按指南在隔离本机实例完成提交、暂停、回复和恢复，验证链接/命令与截图一致。证据：README 已链接当前工作台验收和外部用户自托管方案；指南包含架构/泳道图、语义待办和阈值截图。通过 `business_workbench_lifecycle_fixture.py` 启动临时 PostgreSQL 子库、生产 Gateway/Worker 与 loopback MCP，从浏览器新建 supervisor 合成工单任务，人工确认具体 set_priority 操作，Worker 经 MCP 提交并核实服务端回执；最终目标 high、revision 2、`server_verified=true`。浏览器重载并重新选择任务后仍显示成功，模型请求 0。全程仅合成数据，fixture 数据库已清理；完整过程写入 `generated/261017/1905_工作台使用与隔离验收.md`。不代表真实模型质量或生产部署验收。

## 9. 评测数据、统计和基线工具

- [x] 9.1 为 EnterpriseOps-Gym CSM/ITSM 主 benchmark 与 τ-Bench 补充 benchmark 建立可追溯 manifest，固定仓库版本、任务/域 ID、许可、数据库快照、工具/API 版本及容器镜像摘要；验证许可或环境不明时明确阻止正式记分，不擅自重新分发原始数据。证据：`evals/benchmarks/manifests/source_manifest.json`、`validate_source_manifest.py` 默认来源/哈希校验通过；`--for-scoring` 在镜像未本地拉取时正确阻止正式记分。镜像本机可用性仍是正式评测前置条件。
- [x] 9.2 保留两个 benchmark 的官方划分，建立可复现的完整任务清单及资源受限子集规则；按任务族标注流程深度、工具依赖、状态依赖、结构化分支、运行时语义分支、对话澄清和异常恢复，验证答案/目标状态不进入 Agent 输入且未运行任务如实列出。证据：`manifests/task_inventory.json` 覆盖 EnterpriseOps 206 条主任务与全部官方 split、τ-Bench 所有域及零售/电信 test 子集；每条任务包含版本化启发式 facet、配置工具名和 `not_run` 状态，不包含原始提示或 gold 字段。`agent_input.py` 的 EnterpriseOps allowlist 与 τ-Bench simulator-turn 边界由 `test_benchmark_input_boundary.py` 验证；标签明确为 `heuristic_unreviewed`，不作为模型输入或实测答案。
- [x] 9.3 固定 DecAgent 业务契约专用判断集及至少 40 开发/验证、80 留出流程场景，覆盖中文、全部 primitive、动态候选、权限、故障、人工与记忆；仅作针对性补充，不替代 EnterpriseOps-Gym 主流程成绩；交付标签来源/人工核对清单和场景族隔离检查，未核对项不得标人工验证。证据：`business/frozen-v2.json` 保留按场景族隔离的 40/80 工作流集并覆盖 Choice/Noul/Score；新增 `business/memory-decisions-v1.json` 提供 40/80 记忆写入、读取必要性与相关性案例，覆盖中文与 Choice/Noul。`test_business_memory_dataset.py` 验证 checksum、分割隔离、标签不进入模型输入和契约类型；`evals/benchmarks/README.md` 提供标签来源与人工复核清单。所有标签仍为 synthetic reference、数据集标记 `not_run`，未声称人工复核或实测准确率。
- [x] 9.4 在 `decagent/evaluation/business` 实现语义决策原始预测缓存、离线阈值网格扫描、95%/100 自动样本选择规则及策略候选报告；阈值只适用于当前可信注册表中的语义决策族。证据：`test_business_threshold_scan.py` 覆盖零自动样本、同覆盖优先更高阈值、未达标、注册版本失效、同一输入重复拒绝/不同输入独立缓存，以及离线 CLI 无新增模型调用；合成标签只产出诊断报告，不能发布策略。
- [x] 9.5 实现 Wilson 区间、任务/组配对 bootstrap、误/漏接管、OOS 误执行及人工交互类型统计；用手算小数据与固定 seed 验证分母/聚类正确。证据：`test_business_threshold_scan.py` 验证 Wilson 数值、任务簇配对 bootstrap 可复现、误接管/漏接管/OOS 误执行，以及语义接管/参数补充/写入确认/未知结果核实的等待统计；阈值报告逐点输出实测曲线数据，零自动样本保持 null，不填造数值。
### 9.6 三路线公平比较、公开评测控制面与官方计分闭环

总项 9.6 只有以下子任务全部满足后才算完成。此前合并描述保留为验收范围；拆分用于准确标记工程闭环与真实任务证据，不降低原要求。

- [x] 9.6.A 复用冻结 business-2 MCP 包，在相同 service-ticket 语义种子、治理条件和隔离数据库下完成 execution-only 历史基线配对；保留 schema 11 与当前 schema 17 的兼容隔离，不修改冻结源码，并核对 seed/case digest、身份/权限、工具和治理摘要。证据：`evals/benchmarks/evidence/business2-execution-only-pair-20261019-rerun-54tests.json` 为 `execution_only_eligible`，性能证据资格仅限确定性执行，不作模型质量结论。
- [x] 9.6.B EnterpriseOps 每次独立重置后采集私有数据库指纹并调用原 verifier，区分任务失败、环境失败和评分失败；未测中间副作用标 `not_measured`。验收：CSM/ITSM 各对冻结 smoke 任务执行两次新容器/数据库重置，核对稳定指纹、MCP 读取及原始任务 verifier；未执行任何任务动作时，两域 verifier 均返回已评分的预期 `task_failure`，清理均核实成功，私有 SQL/标签不进入模型输入。环境启动失败与评分器失败分类由 `test_business_route_execution.py` 覆盖。证据：`evals/benchmarks/evidence/enterpriseops-official-verifier-smoke-20261020-csm.json`、`enterpriseops-official-verifier-smoke-20261020-itsm.json`；本地 Docker 集成 `2 passed`，评分/路由分类定向套件 `92 passed`。这不是 Agent 成功执行或公开基准质量结果；首波仍未运行。
- [x] 9.6.C τ 子进程桥使同一权威环境服务用户模拟器、Agent MCP 工具与状态同步，先持久化完整 `SimulationRun` transcript，再交给原始 official evaluator；ReAct/Plan 均可通过 τ simulator 澄清。锁定 τ2 `96a3ef9`、Python 3.12 worker 的工程验收：retail ReAct、telecom ReAct、retail Plan 三条 train smoke 均通过真实隔离工具环境、用户 simulator、原始评分器和 session/environment cleanup；Plan 路线调用 `get_order_details`，评分输入与 SQLite 持久 transcript 完全一致。τ role-handoff 数为 5/5/7。新增长 task ID 边界后，`test_business_evaluation_catalog.py` 与完整 `test_business_tau_process.py` 为 `45 passed (100.52s)`。回调为脚本注入，没有外部模型请求、live public-trial ledger 或 official test split 得分；因此只完成 τ adapter 工程闭环，不代表 80 条官方 test 任务或真实模型表现。
  补充复核（2026-10-20）：锁定 τ Python 3.12 worker 后，`test_real_pinned_tau_route_runs_user_turn_clarification_and_original_evaluator` 的 retail/telecom 参数化 ReAct 用例为 `2 passed (30.27s)`；本复核仅确认用户模拟与原始评分 smoke 可重复，不增加 official test 成绩或首波 live 试次数。
  运行语义补充（2026-10-20，历史进度）：τ 用户模拟器一次返回多个用户工具调用时，先在同一权威环境按序执行整批调用，再以一个 `MultiToolMessage` 触发一次模拟器后续回合；会话轨迹保留批次边界供 Agent 上下文与官方评分共用。此前逐个工具结果后重新调用模拟器会改变官方对话状态。脚本化模型回调下的固定 τ2/Python 3.12 worker 与公开会话回归 `39 passed`，新增覆盖批次执行、身份/形状校验、真实 worker 转换和官方评分兼容；无外部模型请求。此时尚未完成最终 route smoke，闭环验收见当前 9.6.C 项。
  运行计数补充（2026-10-20）：已把冻结 τ2 半双工 `step_count` 的角色交接单位接入 `EvaluationPlanSession`、`ControlledRoutes` 和 SQLite 公开 route ledger；ReAct Agent action、Plan 外部节点（含 `decide`）、每次 User simulator reply 与每个 environment 工具批次分别计数。用户批量工具仍按工具数计 MCP 调用、按一个 Environment handoff 计 τ step。step 上限先于对应模型模拟器/业务工具调用执行，账本恢复不清零，unknown route 回执保留已计数值。role counter 本身的测试未覆盖正式公开路线；当前 τ bridge 闭环及适用范围见 9.6.C 与 9.6.G。
  跨域 train 工程闭环补充（2026-10-20）：锁定 τ2 `96a3ef9` / Python 3.12 worker 上，retail ReAct、telecom ReAct、retail Plan 三条隔离路线均执行原始 τ 环境与模拟用户澄清，通过原 `evaluate_simulation` 并验证 session/environment cleanup；脚本 callback 下 role-handoff 分别为 5、5、7。扩展 telecom 时暴露官方任务 ID 213 字符被 `EvaluationScope` 160 字符边界误拒，现仅将 case ID 上限设为 256、benchmark/version 仍为 160，并以 213 接受/257 拒绝的回归锁住边界。锁定 Python 3.12 worker 后 `test_business_evaluation_catalog.py` 与完整 `test_business_tau_process.py` 合计 `45 passed (100.52s)`；其中新增路线与边界的首次定向回归为 `4 passed (41.42s)`。没有外部模型请求、live 公开试次账本或 official test split 任务。它完成 τ 工程适配器子项 9.6.C。后续 2026-10-20 独立补上 Plan telecom：两域 Plan read-tool 路线 `2 passed`，证据 `evals/benchmarks/evidence/tau-plan-cross-domain-smoke-20261020.json`；四域统一故障注入与真实首波仍由 9.6.G/10.10.D–E 覆盖，不能据此宣称公开评测完成。原证据同步于 `evals/benchmarks/evidence/tau-step-accounting-20261020.json`。
- [ ] 9.6.D 依据公开政策与工具契约审核并冻结自然适用 Jev 模板，配对相同状态下 Jev 与固定通用模型语义判断；结构化条件走规则，无适用场景标 N/A。部分进度（2026-10-20）：审阅首波 CSM 原始提示和 pinned `retrieve_knowledge` MCP schema，确认“为产品问题选择适用文章”是自然语义节点；用户工单数量比较及 ITSM 明确字段由代码处理。版本化模板 `csm_knowledge_article@1.0.0-csm-knowledge-1` 现仅注册到受信 `EvaluationCatalog`：编译器要求同一真实 `retrieve_knowledge` 读结果、字面量 `state=published`、无 `limit`，运行时把该节点记录的原始参数和确切返回候选一起交给 Choice；只接受实际返回的已发布 ID，提供 `no_match`，不截断候选、不授权写入；超过 24 个候选或上下文超过 16,000 字符时标记不适用。真实 pinned CSM 容器工具目录核对确认 12 个工具及实际 schema；上游没有 `readOnlyHint`，隔离目录只对匹配审阅 schema 的 `retrieve_knowledge` 使用受限只读覆盖，生产 PluginSpec/HTTP 不变。容器 cleanup verified，0 业务调用、0 模型请求。新增受信 registry/编译器/路由覆盖 8 passed，模板构造器 6 passed，公开 Session 相关套件 36 passed；脚本 callback 已通过 ControlledRoutes/生产 PlanRuntime 选择候选并映射到返回的 integer ID，仍不是外部模型结果。first-wave dry-run 的 `jev_semantic_status` 已修正为 `reviewed_csm_natural_case_pairing_pending`，首波冻结路线仍是 ReAct/Plan 共 8 个槽位。证据 `evals/benchmarks/evidence/jev-semantic-template-audit-20261020.json`、`jev-semantic-template-registry-20261020.json` 和 `public-evaluation-phase-first-wave-csm-reviewed-dry-run-20261020.json`。真实受控 runner 对同一冻结状态和候选完成 Jev/Qwen 配对尚未运行，因此本任务未完成，无 Jev 质量/性能结论，公开成绩仍为 0/286。
- [ ] 9.6.E 固定公开试次 manifest、暴露组、角色/模型 profile、每试次与全批资源、冷/热复用、显式记忆消融和计时；检查标签与私有评分材料不进入 Agent 输入。部分进度：v7 冻结清单保留 24/286 分母和四域首批 4 例/8 路线槽位；Qwen 总请求/token、Agent/模拟器/裁判分角色请求/token、工具调用和墙钟上限现绑定到 cohort digest，并由 ledger v5 在跨路线/恢复时检查。证据 `public-evaluation-dry-run-smoke-v7-20261020.json` 仅验证配置读取，尚无真实任务。暴露分组、复用/记忆消融执行和输入边界的公开任务验证仍未完成。
- [ ] 9.6.F 接通真实模型与工具 callback、三路线调度、官方用户模拟器/裁判、最终回复、评分错误分类、未知状态处理和环境清理；Qwen unknown 终结当前 route，Jev unknown 暂停全局新增 Jev 请求。
  进度（2026-10-20）：`ControlledRoutes` 已提供 session-scoped model/gateway factory；新增 `LedgeredToolGateway`，可把生产 PlanRuntime 经过的工具调用接入公开 ledger 的 reserve/dispatch/settle。unknown 工具结果会终止路线并跳过官方评分，评分/清理状态与业务终态分开保存；unknown 路线可补充清理证据但不能改成成功。定向回归覆盖未知工具不调用 scorer、canonical first-wave ledger 和跨进程监督锁。真实 provider 回调、跨路线调度、官方 τ 用户/裁判闭环及真实首波仍未接通。
- [ ] 9.6.G 完成四域首批 8 个 ReAct/Plan 路线试次及每域官方评分/清理闭环，再补齐固定 24 例 smoke；只有自然适用且已审核的语义节点可增加 Jev 配对。
  进度（2026-10-18）：新增一次性 EvaluationScope/EvaluationCatalog、独立 SQLite journal 和 Enterprise/τ session factory，原始工具复用同一生产 compiler/binding/PlanRuntime；生产 PluginSpec/Registry/HTTP 无新增注册入口。成功/错误原响应持久化，明确工具错误可由 ReAct 恢复或进入计划 on_error，传输未知写入不会重放；恢复核对输入与 provenance 并复用冻结 input_schema。τ 内存 MCP 至 PlanRuntime 端到端、Enterprise session 指纹、错误恢复和分阶段计时已有回归；全量确定性 1493 passed/4 skipped、全量 PostgreSQL 596 passed/24 skipped，随后评测专属修复按定向套件复验。EnterpriseOps Docker readiness 另跑 2 passed，真实 PostgreSQL/认证 MCP 路线另跑 2 passed。无新增模型请求，正式任务仍 0/286。business-2 历史服务适配、独立恢复状态验证、可信语义模板、真实模型配对/消融与用户模拟器费用证据仍待补齐，因此该项未勾选。
  复核补充（2026-10-18）：`test_business_route_execution.py` 增加三项失败来源分类断言，分别区分任务失败、环境失败和评分失败；定向套件 51 项通过。它验证评测执行器分类边界，不替代公开 benchmark 官方任务 verifier。
  兼容性审计补充（2026-10-19）：冻结 business-2 manifest 固定历史源码 schema 11；归档 `Database.check_version()` 要求迁移序列精确为 1–11，而当前代码 schema 为 17。将 9.6A 明确为双隔离库、同语义种子和前置摘要核对的补充历史基线；不得直接把旧包接到当前数据库，也不得改写旧包迁就新 schema。随后完成的 adapter、配对运行和准入校验状态见本 tasks 后面的 9.6A 执行补充；9.6 总项仍未完成。
  静态兼容审计（2026-10-19，后续纠正）：`evals/benchmarks/evidence/business2-static-compatibility-20261019.json` 的两侧 seed_counts 实为 0/0，不能证明 6 人/5 类种子相同；原文件保留追溯，不作为 9.6A 运行证据。
  本轮补充（2026-10-18）：已按确认实施 τ2/MCP 独立依赖锁与每试验子进程桥，保留原 Environment.get_response、schema、JSON 参数类型、文本错误和上游可变状态元数据；真实零售/电信 MCP、生产 PlanRuntime 两步参数绑定、网络/凭据隔离、有界 stdio、故障不重放及 Windows 进程树回收已有验证。定向 142 passed/1 skipped（旧应用内直接 import τ2 可选测试；真实子进程测试已通过），Ruff、strict、锁与来源门禁通过，wheel 含桥接模块。其余历史基线、语义模板、完整模拟器/官方评分和正式配对仍未完成；不将 80/80 初始化当任务得分。详见 generated/261018/评测/1928_τBench独立运行时桥与验证.md。
- [x] 9.7 实现累计 Jev 1 美元账本预留、读取和未知费用停止机制，并保留主模型请求/token；测试历史费用不可重置、零额/超额/未知预留/中途停止后不能继续及恢复前置检查。证据：`test_business_evaluation.py` 覆盖 ledger、GlobalAttemptBudget 和报告用量；benchmark README 区分 live、scripted 与缓存回放的计费口径。
- [x] 9.8 为公开计划路线实现实际工具证据的确定性最终回复与一次受控 Qwen 总结回调，冻结并核对输出策略摘要；测试无总结的零调用、真实来源引用、超过输出/输入限制、未知写入拒绝、错误/未知总结用量停止、与 ReAct 的公平配置和官方评分隔离。总结计入 Agent primary 与执行时间，不伪造 goal.description，不删除 response_check 任务；真实模型门禁通过前仅使用离线回调验证。证据：`final_response.py`、`test_business_final_response.py` 18 passed；相关组合139 passed、全量Ruff通过、独立规格及代码复核通过。unknown后跳过所有评分，ReAct与Plan同UTF-8上限；manifest v2保留286正式案例及24冒烟，v1归档。`generated/261019/评测/1433_EnterpriseOps评分与真实结果回复.md` 说明注入式回调与实测边界，未新增真实模型请求。

## 10. 真实质量和效率验收

- [x] 10.1 完成主模型就绪诊断与放行决策（勾选表示检查流程已执行，不表示模型通过）：保留 `qwen3.8-flash`，最多使用 3 次获准的新请求且包含一次格式修复；记录每次请求和账本，超时或派发/用量未知立即停止，不切换模型。2026-10-18 的 unknown 请求不重试。实际结果：2026-10-19 共 3 次请求；首次最小连通请求成功（1302 tokens）；计划请求得到已知 `tool_schema` 拒绝（12900 tokens，当时按 v2 零费率 profile 记录 $0；2026-10-20 更正：这不是供应商费率或账单金额，不能作为成本证据）；一次格式修复超时，usage/费用未知，立即停止。无计划通过生产编译器、无 MCP 业务工具调用。三次请求合计费用保持 unknown；不得把超时计为零。最新证据：`evals/benchmarks/evidence/qwen-primary-readiness-20261019.json`、`qwen-plan-readiness-20261019.json`、`qwen-plan-format-repair-20261019.json`。此段只记录 10.1 早期批次；后续 10.9 routed readiness 已通过，当前真实评测入口仍受 10.10 的批次预算、route callback、官方计分及清理门禁约束。原 2026-10-18 unknown 请求仍单独保留且未重试。
- [x] 10.2 完成冻结数据的阈值资格诊断与决策：只有经审核的验证数据具备每决策族至少 100 个自动样本时才执行阈值选择；不满足时如实保留 95% 正确率目标、报告无合格策略且不使用 holdout 补样。当前 synthetic auxiliary 数据最高每族 40 个工作流样本、20/10 个记忆样本，已证实不具备资格，因此本轮不产生生产阈值。证据：`evals/benchmarks/evidence/eligibility-20261019.json`（`not_eligible`、`qualified_policy=null`、0 live calls、holdout 未用于调参）。这表示资格评估完成，不代表阈值评测通过或模型准确率达标。
- [ ] 10.3 先执行四域首波（CSM、ITSM、τ retail、τ telecom 各 1 条；ReAct/Plan 共 8 个路线试次），通过真实模型、工具、原官方计分及清理闭环后，再补齐 24 例固定 smoke；每域至少完成一个官方计分流程并通过评分错误处理检查。门槛通过后才运行 EnterpriseOps 全部 206 条和冻结 τ official test 80 条。固定任务、模板、模型、策略和快照；提交每域/暴露组/未暴露组/任务族的分子分母和区间。模型或资源不足时保留准确未运行清单，不冒充全量或实测结果。DecAgent 自有数据只作为辅助回归，不进入公开基准主分数或阈值资格。10.9 routed readiness 已通过；首波批次额度已由用户确认，但账本执行和 live callback 未验收前不得派发。
  历史门禁说明：10.7 的旧 readiness 授权因 unknown 按原规则停止且没有重放。其后独立 routed readiness（2026-10-19 `routed-model-readiness-20261019-102641.json`）的技术检查通过；其中旧 profile 记录的 Qwen `$0` 不是供应商费率或账单证据，不用于成本统计。用户随后已批准首波 Qwen 用量上限（500 次/500 万 Token/4 小时，分角色和每路线另有限额），该授权不重开历史 unknown。当前真实任务仍为 0/286；新批次仅可在统一 runner、持久账本、可取消 deadline、工具/官方评分/清理门禁全部通过后启动。
  运行时进度（2026-10-18）：固定 τ2 原始 test 的 retail/telecom 各 40 条已在独立 Python 3.12 环境完成两次新鲜初始化一致性检查，共 80/80；固定源码/域数据 279 文件与源归档逐项比对。无网络/模型调用、正式任务尝试仍 0/286。Python 3.13 的 audioop 缺失、MCP 与原锁 idna 冲突均记录；按用户选择实施独立进程桥，不升级原锁。
  就绪证据已保存为 evals/benchmarks/evidence/tau2-runtime-readiness-20261018T110840Z.json；实际桥接验证见 9.6。完整公开任务目标、模拟对话与官方评分仍未执行，不据协议回归填写成功率。
- [ ] 10.4 在冻结公开任务上执行 ReAct + Skill + MCP、一次规划 + 确定性执行器 + MCP、一次规划 + 确定性执行器 + Jev 语义判断三条路线及预注册消融。主质量比较每路线相同上限：200 Agent 模型请求、200 工具调用、1,000,000 token、30 分钟 Agent 机器时间（τ 另遵守官方 200 步上限）；生产预算作为独立副评。所有路线同任务/状态/权限/确认，逐试次重置；统计任务成功、错误副作用、模型请求/成本/延迟及配对差异；只有 Jev 相比同状态 Qwen 二次判断质量可接受且成本或延迟改善时才确认 Jev 收益，结构化代码规则不作为 Jev 的竞争对象。
- [ ] 10.5 分开执行标签 oracle、τ-Bench 官方用户模拟器与原员工真实交互样本，至少覆盖三种 primitive、无匹配、补充、拒绝和独立写入确认；模拟器和裁判使用本轮冻结通用模型 profile，分别记录角色/提供方/模型/调用/token/延迟/费用；正式对照关闭跨模型降级，不能使用 τ 上游默认 GPT/Claude。员工研究未完成时明确保留待办，不把模拟用户当真实审批者。
- [ ] 10.6 发布实测报告、自动覆盖—错误率、阈值—接管、模型调用/成本/延迟图和可复现命令；逐项标通过/未达标/证据不足，区间不支持时不宣称提速，不以报告存在替代未通过验收。

- [x] 10.7 执行 2026-10-19 新授权的 Qwen 离线定位与有界就绪复验：量化请求规模，区分可复现实现缺陷与未保存旧响应的未知原因；失败回归先行并修复，通过后以 `qwen-readiness-20261019-plan-execution-v1` 持久计数最多 3 次全新只读请求（最多一次格式修复）。正常返回缺 usage 同样停止；unknown/崩溃不得重置、重发或归零。保存实际编译结果、用量、诊断与门禁证据；未通过则保持 10.3/10.4 未放行。本项不重写 10.1 历史结论，Jev 累计预算不变。验收结果：离线诊断修复与独立安全复核通过；单次新只读请求在 2026-10-19 03:09 UTC 返回 transport_error，服务端 usage/cost unknown，持久门禁记 1 次 unknown 并封锁本授权后续请求；业务工具调用 0。未就绪，10.3/10.4 不放行。证据：`evals/benchmarks/evidence/qwen-readiness-plan-execution-v1-20261019.json`；旧 unknown 未重放。

### 10.8 版本化通用模型 profile 与记账

- [x] 10.8.A 为规划、结果回复、通用模型语义对照、τ 用户模拟器和裁判冻结完整角色 profile：采样参数、输出/工具选择契约、超时、provider routing、token/费用边界，并显式列出 unsupported 参数。验收：`evaluation_model_profiles_v2.json` 为四角色无密钥 profile；采样值、unsupported seed、输出/工具契约、timeout、routing、token 及价格来源均进入 fingerprint；`PrimaryModel.from_evaluation_profile` 显式应用支持的值，`test_business_model_profiles.py` 覆盖请求参数与模型身份绑定。该项只完成 profile 冻结与通用客户端构造，不代表公开评测 callback 已接通。
- [x] 10.8.B 保留 Qwen 主端点与固定 OpenRouter `deepseek/deepseek-v4.1-flash` 备用配置，通过现有 Jev OpenRouter key 引用凭证；每次真实尝试独立记录 requested/resolved provider/model、request/generation IDs、usage、费用、时长和安全失败类别，不记录 secret。验收：`.env.example` 与 `Settings` 将 fallback 默认关闭并只允许 OpenRouter endpoint/凭证；`build_budgeted_primary` 为每次主/备派发分别预留账目，记录请求 profile、resolved model、server ID、用量、费用、时长和失败类别。`test_primary_route_retries.py`、`test_model_routing_config.py` 与 `test_conditional_model_budget.py` 覆盖账目/凭证隔离及恢复身份；无真实 fallback 调用在本项验收中发生。
- [x] 10.8.C 验证 Qwen 瞬时失败最多两次、之后 DeepSeek 最多一次，未知预留占用原预算，正式对照关闭跨模型降级，模型/路由漂移建立独立实验组；未知 MCP 写入不重放。验收：路由定向套件 `34 passed`，覆盖 Qwen 两次上限、单次 DeepSeek、Retry-After 与不可重试错误、预算拒绝、未知预留保留及正式 cohort 禁止 fallback；对照 profile digest 会拒绝模型/路由 drift，公开试次未知工具/路线不能 replay。该验证使用受控响应，不代表真实 DeepSeek 性能。
- [ ] 10.8.D 新建 Qwen 计费 profile 版本，纠正 v2 的零费率假设；保留 v2/原始 evidence 作为历史，不把其 `$0` 记录当供应商账单。为新 profile 绑定可核验的账户账单或费率来源及版本，分别记录抵扣前 API 费用、代金券抵扣和实际支付；provider 未提供账单金额时仅用冻结费率计算并标记 computed，费率来源不可核验时费用标记 unknown。分离 usage_complete 与 cost_complete，不得把已知 token 用量推导成零费用。补充 profile 指纹、请求结算、报告与历史兼容测试。**进度（2026-10-20）：** 已加入 `evaluation_model_profiles_v3.json` 和 v6/v7 cohort，Qwen 三个角色的费率为显式 unknown；v2/v5/v6 保留。`ModelReply`、路线结果、公开账本及 EnterpriseOps response-check scorer 支持 token 已知、成本未知；unknown cost 不会计作 `$0`，且不会阻止基于已知执行证据的官方评分。新的 first-wave ledger callback 允许用户已授权的 Qwen 角色在请求/Token/墙钟硬上限内派发并以 null 费用结算，Jev unknown-price 仍禁止派发。尚未核实该账户/endpoint 适用地域和实收费率，也未实现每笔 gross API fee、voucher credit、cash paid 的独立字段及报表；本任务保持待办，Qwen 成本和模型成本比较仍不可用。
  补充（2026-10-20）：公开账本测试中的 Qwen primary/user-simulator 示例原先以 `Decimal("0")` 模拟结算，已更正为 `None`，并验证请求 token 已知但聚合费用完整性为 false；这些测试不再暗示 Qwen 有零费率。供应商计费仍需账单或适用费率才能量化。
  最新进度（2026-10-20）：`ModelReply` 新增有验证的 `ProviderBillingEvidence`，公开账本按原币种保存逐请求 gross fee、供应商记录来源/ID、实际提供的 voucher credit 和 cash paid；USD gross 会作为抵扣前 provider-billed 成本，voucher 不冲减模型 API 成本。缺失分项仍为 null；非 USD 金额原样保存且无可核验汇率时 USD 成本仍 unknown。`billing_snapshot()` 按原币种输出路线级费用/抵扣/实付与缺失分项数，不做汇率或跨账单分摊。测试覆盖 USD 账单、券全额抵扣、实付为零、CNY 未换算及费额不一致拒绝。接口只接受逐请求证据，尚无 Qwen endpoint 的自动账单读取、账户时间窗汇总、汇率证据或最终报告汇总；因此 10.8.D 继续未完成，不能据此发布真实成本结论。
- [x] 10.9 实现全新 readiness 批次 `primary-readiness-routed-v1`：保留全部旧账本且不自动执行未结 attempt；新增批次总计最多 8 次请求、300,000 input+output tokens、DeepSeek $1，单请求 65,536 输入/8,192 输出。先测试 Qwen 最小完整响应/usage，再最小工具 schema，再生产只读 plan 编译；需要时按同一有界政策转 DeepSeek；有额度再测试流式。每个 attempt 记录 endpoint、model、role、阶段、attempt序号、耗时、状态码/安全错误类、usage、费率版本、reserved/actual费用与 unknown；no retries beyond per-step caps，格式修正最多一次，不调用业务 MCP 工具。只有至少一个模型返回 usage 完整并通过生产编译器的只读计划才算 readiness 通过。实测请求必须来自新的明确授权批次，超预算不派发。验收证据：`evals/benchmarks/evidence/routed-model-readiness-20261019-102641.json`，connection/tool_contract/plan/stream 全部通过；5 次实际 Qwen 请求、29,858 tokens、报告费用 $0，0 次 DeepSeek 和业务 MCP 调用。计划阶段首个响应为带 usage 的 `tool_schema` 拒绝，唯一一次结构化修正经生产 compiler 接受只读 directory 计划；流式响应及 usage 完整。派发前本地 provider 标签错误留下的失败记录有 0 次请求，通过仅允许 known-not-sent 的安全恢复追加归档后继续；原 3 次尝试与 2026-10-18 unknown 均未改写。就绪批次上限及剩余额度保留在同一 SQLite ledger 中。
### 10.10 公开试次 runner、预算和恢复安全

计费口径更正（2026-10-20）：`evaluation_model_profiles_v2.json` 将 Qwen 输入/输出单价设为 0，源于先前对账户“免费额度”的误解。用户已确认 API 正常计费、代金券抵扣账单；10.9 readiness evidence 中显示的费用 `$0` 是按该零费率 profile 记录，不是账单核验结果。原 profile 与 JSON evidence 不修改；在 10.8.D 完成新 profile/费率来源前，这些费用不得用于成本比较。token 用量和历史 unknown 按原记录保留。

- [x] 10.10.A dry-run 校验冻结 manifest/source/inventory/profile/readiness/镜像并报告准确任务与路线分母，默认不创建 ledger、不调用模型/MCP，证据只写新文件。v5 smoke/full preflight 分母保持 24/286、48/572 route slots；v6/v7 使用明确 unknown 的 Qwen profile，新的 smoke preflight 为 24 个任务、48 个路线槽位、首波 4 个任务/8 个路线槽位，模型/MCP 调用与 ledger 创建均为 0。v7 同时冻结首波批次请求、token、工具和墙钟上限。phase 离线选择现支持 `first-wave`（4/8，冻结顺序）、`remaining-smoke`（20/40，排除首波）和 `full`（286/572）；所有结果同时报告所选分子与完整 cohort 分母，选择操作不创建 ledger、不派发模型或工具。证据：`evals/benchmarks/evidence/public-evaluation-dry-run-smoke-v5-20261019.json`、`public-evaluation-dry-run-full-v5-20261019.json`、`public-evaluation-dry-run-smoke-v6-20261020.json`、`public-evaluation-dry-run-smoke-v7-20261020.json`、`public-evaluation-dry-run-smoke-v7-model-ledger-20261020.json`、`public-evaluation-phase-first-wave-dry-run-20261020.json`、`public-evaluation-phase-remaining-smoke-dry-run-20261020.json`、`public-evaluation-phase-full-dry-run-20261020.json` 与 `evals/deterministic/test_business_public_trials.py` phase 选择测试。预检不等同于真实执行。
- [ ] 10.10.B 真实执行固定批次身份和全局 Agent/模拟器/裁判请求、token、费用、时间上限；每个 Jev attempt 绑定既有 `.decagent/business-eval/global-jev-ledger.json`，换 state path/mode/恢复进程不能重置累计余额。部分进度：v7 cohort 的首波上限进入 cohort digest；ledger v6 在同一 `BEGIN IMMEDIATE` 事务内执行批次准入和 route/model/tool/time 预留，并持久记录累计计数、开始/截止时间与停止原因。新增 canonical public-trial ledger 和 supervisor lock 路径校验；`open_first_wave_public_trial_ledger` 仅在持有项目规范 OS lock、模式为首波 smoke 且现有规范 Jev ledger 可打开时，创建或恢复唯一首波 ledger。新增 `LedgeredModelCallback` / `LedgeredModelAdapterFactory`，将单次模型 attempt 绑定 frozen role/profile、完整输入 token 上界、路线/批次 deadline、reserve→dispatch→settle/unknown；账本保存费用来源 `provider_billed` / `frozen_profile` / `unknown`，可在旧 v6 ledger 上加列而不重置 cohort/余额。经用户明确授权的 Qwen primary/user_simulator/judge 当前允许以 null 费用预留，在 v7 请求/Token/时间上限内 dispatch；实际费用仍 unknown 时按 null 保存、`cost_complete=false`，不计为 `$0` 且不能用于成本比较。Jev 不适用此例外，须保留已知单次费用上界并绑定原累计 `$1` 账本。逐笔 gross API fee、voucher credit、cash paid 证据结构已定义但供应商账单获取尚未接入。first-wave live CLI 和统一 session/provider adapters 已接通，候选、画像、readiness、镜像和 τ runtime 有 fail-closed 门禁；尚无实际派发或账单记录，故本项不勾选。
  最新进度（2026-10-20）：新账本模型请求结算可选附带经过类型验证的逐请求原始账单证据，金额按 Decimal 文本保存，记录 gross、currency、voucher、cash、来源与记录 ID；USD gross 与实际 USD 费用必须一致。未提供的 voucher/cash 不会被推测，非 USD 不会被隐式换算；`billing_snapshot()` 只按原币种聚合并公开覆盖率。旧 v6 cohort/ledger 可通过加列保留原身份。该数据仍只在 callback 明确提供时落账；供应商账单获取尚未接通。
  模型身份补充（2026-10-20）：`ModelReply.model` 允许为 null，`LedgeredModelCallback` 在 resolved model 缺失时将账本 `actual_model` 保留为 null，不把请求 profile 的 model 名冒充上游实际模型。已返回且与冻结 profile 不一致的实际模型名仍记账后触发 profile drift，并在唯一批次账本持久写入 `model_profile_drift`，使后续路线的事务准入失败。`test_missing_upstream_model_identity_is_recorded_as_null_not_profile_identity` 与 `test_reported_upstream_model_drift_stops_the_frozen_batch` 覆盖两条边界；真实 provider 适配仍待 live runner。
  Jev unknown 跨路线补充（2026-10-20）：新增公开账本 API 回归：第一条路线的 Jev 派发进入 unknown 后，第二条仍运行中的路线调用 `reserve_model_attempt(role="jev")` 会被共享累计账本拒绝，且不会生成第二笔本地或全局 reservation；unknown 原 reservation 保留。与模型派发、单路线/phase 执行定向套件共 `81 passed`，Ruff、OpenSpec strict（6 changes）通过。该测试确认底层跨路线门禁，不代表真实 runner 或 Jev API 派发已经验收；10.10.B/D 仍待完成。
  路线评分门禁补充（2026-10-20）：`ControlledRoutes` 捕获 `PublicModelProfileDrift` 后将本路线记为失败并跳过官方评分，避免评分器追加同批模型请求；以 injected drift 回调验证不调用 scorer、不派发工具。定向路线与派发测试通过；本地真实 provider 和官方评分闭环仍未接通。
  评分持久化补充（2026-10-20）：route ledger 新增 nullable `score_evidence_json`，scored 状态必须提交带来源、0–1 官方分数、成功标记与副作用计数的最小摘要，摘要 digest 必须匹配；来源按冻结 benchmark 限定为 EnterpriseOps 原始 verifier 或 τ 原始 evaluator。旧 v6 route 表通过加列迁移，缺失的历史摘要保持 null；unknown route 不能补评分，只能补 cleanup。`test_business_public_trials.py` 验证摘要跨 resume 保留、错误 benchmark 来源被拒、旧表加列及 unknown 终态不变，整文件 `42 passed`。真实模型与工具输出没有进入这些 score summaries；live runner 尚未存在，因此该接口尚不能产生公开任务实测报告。
  当前工作区回归（2026-10-20，历史快照）：全确定性套件 `1806 passed, 13 skipped`；相关集成回归 `4 passed, 2 skipped`，两项 PostgreSQL 路由集成因没有配置专用 disposable `DECAGENT_TEST_DATABASE_URL` 而跳过；Ruff 与 OpenSpec strict 均通过。首波 dry-run 为 4 任务/8 路线且没有创建账本或派发请求。该快照不提升 9.6/10.3/10.10 的真实评测任务状态。
- [x] 10.10.C 加入单一活动执行者租约、heartbeat 与 fencing generation；跨进程 resume 只能在 lease 过期后接管，旧 owner 不能再写入/dispatch，unknown/completed route 不 replay。验收：`test_business_public_trials.py` 覆盖存活 owner 拒绝并发恢复、过期接管、旧 owner 的 route/dispatch 写入被 fencing、unknown/completed route 不重放；真实多机/进程故障演练仍由 10.10.E 覆盖。
- [ ] 10.10.D 将执行器实际连接到资源 reserve/dispatch/settle、可取消 deadline、真实模型/工具 callback 与官方评分/清理；Qwen usage unknown 只终结当前 route，Jev usage/费用 unknown 停止组内所有新增 Jev 调用。部分进度（2026-10-20）：`ControlledRoutes` 新增 `on_agent_start` 顺序 hook；machine-time ledger 记录一次性 `agent_started_at_utc` 并从真实 Agent 回路起点重锚 deadline，环境准备仍计入批次墙钟但不计入每路线 Agent 时间。回归覆盖 hook 顺序、持久 deadline 和重复调用不续期。新增 per-route `execute_public_route()`，仅接受 first-wave ReAct/Plan；claim 前验证规范 supervisor OS lock、规范 smoke ledger 和 ledger-bound model adapter；gateway 返回时校验 ledger/trial/route identity，拒绝直接未计量 callback。`start_route_with_machine_time()` 在同一 SQLite 事务原子提交 route claim 和 machine-time reservation；超限失败仍保存已有 score 与 cleanup。新增 `execute_public_phase()`，按冻结四任务与 ReAct/Plan 顺序遍历 8 槽，只派发 pending route，跳过终态，未知不重放；已有 batch stop 时不创建 trial/session。调度器定向用例覆盖 unknown 后继续其他路线、终态恢复跳过、持久 stop，以及真实 SQLite ledger 重开后已完成路线不重放；`test_business_public_execution.py` 为 12 passed。该入口尚未接入 batch CLI、真实 provider/session callbacks，`remaining-smoke`/`full` live scheduler 未实现；任意阻塞工具和同步 callback 的强制取消、四域官方模型评分与 cleanup 仍未闭环，因此本任务不勾选。
  进度（2026-10-20）：工具侧已通过 `gateway_factory` 连接持久 reserve/dispatch/settle；派发后异常成为 unknown，`ControlledRoutes` 不再对该路线调用官方 scorer。账本分别记录业务终态、score 状态、cleanup 状态和 evidence digest。新增模型账本 callback；它要求每次真实 callback 显式接收 timeout，并把模型 profile timeout、路线机器时间截止与批次截止取最小值。`PrimaryModel.respond(timeout_seconds=...)` 将该值应用于 OpenAI sync transport，且通过测试检查客户端恢复原 timeout。机器时间截止存入 ledger，尚未有实际 route runner 自动 reserve/settle。规划、ReAct、Jev、τ 用户模拟器、裁判 callback factory 与官方评分清理尚未由同一 live runner 接通；完整网络请求取消/子进程树取消、Jev unknown 跨路线阻断与故障注入仍未验收，因此本项保持待办。
  补充（2026-10-20）：新增 LedgeredModelAdapterFactory，将 planner/ReAct/semantic/summarizer/τ user simulator/official judge 的回调口按冻结角色、manifest trial ID 与独立输入 token 上界包装到 LedgeredModelCallback，供 ControlledRoutes 的既有 model_adapter_factory 使用。新增 scripted PlanRuntime 路线闭环测试：一次模型计划、一次 MCP 读取，模型与工具账本均结算成功；没有真实 provider 请求。新增用户批准额度内 Qwen known-usage/unknown-cost 的账本结算测试，费用为 null、成本完整性为 false；Jev 费用未知仍不派发。`PublicModelAdapters` 接管暴露的 callback client 生命周期，在官方评分与用量读取后幂等关闭；关闭失败单独记作 cleanup failure，并保留已有评分证据。尚未接入真实 provider client factory、EnterpriseOps judge 的同账本适配或统一 supervisor runner。
  进度（2026-10-20）：`ControlledRoutes` 不再共用 `max_steps=64`；PlanRuntime 控制步单独限制为最多 32 个已编译节点，ReAct 轮次独立限制为最多 200 次模型请求。模型 callback 账本分别数真实模型请求，工具 gateway 账本分别数 MCP 调用。τ 官方 `tau_steps` 仍须由 live route/session 从冻结 resource profile 明确传入并验收；此次实现没有把 ReAct 轮次或 MCP 工具数冒充 τ 步数。该限额拆分只做了离线路线回归，不代表 supervisor 可取消的 route deadline。
   deadline 接线补充（2026-10-20）：持久 route/batch deadline 现传入 `LedgeredToolGateway`，在预留前、标记派发后和调用上游前检查；调用请求收到 route/batch/适配器上限的最小值。EnterpriseOps 请求使用请求级 HTTPX timeout；τ MCP `read_timeout_seconds`、parent callback timeout 与子进程 bridge deadline 继续取最小值，bridge 超时会终止其拥有的 τ worker 子进程树。派发后异常保持 unknown，禁止同路线继续或评分。相关确定性套件 `191 passed, 11 skipped`；此次复验为定向契约测试，不证明 provider/session factories 已接入，也不覆盖任意忽略 timeout 参数的同步 callback 强制取消。跨路线 Jev unknown 现有底层拒绝测试，但尚未接到真实 Jev runner；四域真实闭环、phase CLI 与完整监督取消仍未完成，故 10.10.D 保持未勾选。
   计时语义纠正（2026-10-20）：新增失败优先测试发现 route dispatch 到 Agent-start 之间的环境准备仍会递减 route deadline，与已批准的“准备阶段只计入 batch wall-clock”政策不一致。`route_remaining_seconds()` 现在在 Agent-start 尚未持久化时返回完整 route reservation；`deadline_provider` 仍取 batch 剩余墙钟最小值。Agent-start hook 持久化后才按真实截止时间递减 per-route machine time。新回归先按现状失败，再随修复通过；账本/路线/scheduler 定向回归 `129 passed`。修复后全量 `evals/deterministic` 为 `1850 passed, 14 skipped (286.53s)`。live route 未运行。
   PostgreSQL 补充：在一次性隔离 PostgreSQL 17 容器中运行 `evals/integration/test_business_route_execution.py` 为 `2 passed (21.20s)`；覆盖生产 PlanRuntime 对接真实 loopback MCP 与应用 PostgreSQL，但不是 public-trial ledger/provider/scorer 集成，也不算 benchmark 成绩。临时容器已停止并删除；已有业务与测试容器保持运行且未变更。
  真实 provider callback 进度（2026-10-20）：新增 `public_models.py`，为冻结 primary profile 构造单请求 planner/ReAct callback，为 τ 的 user-simulator/judge 转发器保留原始工具 schema、usage、服务端请求 ID，并由 `public_first_wave_model_adapter_factory` 按角色封装进既有 reserve→dispatch→settle/unknown 账本；工厂不配置 fallback，client 由 route adapter 生命周期关闭。planner 的返回经生产 compiler 校验，ReAct 每次只允许一个已注册工具或有界终止动作。定向 callback/runner 回归 `144 passed`，Ruff、OpenSpec strict 通过。仅为注入响应测试，未发送 provider 请求；尚未接入 public CLI、真实 session/trial factory、EnterpriseOps/τ 官方评分与 cleanup 的统一 live phase，也没有真正取消任意忽略 deadline 的同步 callback，因此 10.10.D 仍待办。
- [ ] 10.10.E 在隔离环境完成跨进程竞争、崩溃/接管、预算边界、profile drift、unknown 恢复及四域首批试次验证；任何正式 `--execute` 仅在全部适配器和门禁就绪后启用。不得把 auxiliary CLI 当作 206+80 runner。部分进度：新增真实多进程竞态测试，证明 lease 过期时 contender 仍不能越过旧监督进程持有的 OS 锁派发；旧 owner 恢复后被 fencing，锁释放后规范恢复将已派发模型尝试及路线标记 unknown，且拒绝重放。四域模型/工具/官方评分闭环、profile drift 端到端和完整批次故障注入仍未验收。
  进度（2026-10-20）：新增 `PublicSupervisor` OS 文件锁，Windows 使用 `msvcrt`、POSIX 使用 `flock`；独立子进程测试证明锁在持有者退出前阻止第二个监督者取得发送资格。定向确定性套件 `evals/deterministic/test_business_public_trials.py`、`test_business_route_execution.py`、`test_public_supervisor.py` 合计 88 passed，涉及文件 Ruff 通过。旧监督进程挂起时 lease 过期后接管、回调/HTTP 阻塞取消、四域官方评分与清理故障注入、PostgreSQL 集成和真实八路线均未完成；该次复核因未配置 `DECAGENT_TEST_DATABASE_URL` 跳过了 PostgreSQL 集成，不能记作通过。

- [x] 10.10.F 在模型、工具与 route unknown 路径记录可审计但不泄露业务内容的诊断：失败阶段必须来自白名单，异常类型只保存类名，可用 HTTP 状态须有效；attempt 快照和新报告只导出安全白名单；旧账本以 nullable 列兼容迁移，unknown 清理补录保留原诊断且不可重放。证据：`public_diagnostics.py`、账本与路线实现；`test_business_public_diagnostics.py`、`test_public_model_dispatch.py`、`test_business_public_trials.py`、`test_business_public_execution.py`、`test_live_public_runner.py`。定向回归 `167 passed`，完整确定性套件 `1911 passed, 18 skipped`，Ruff 与 strict OpenSpec 校验通过；没有重跑旧 unknown，也没有 live 调用。旧报告 v1 保持不变，新报告 schema 升至 v2。该项不代表真实评测门禁开放。

历史进度检查点（2026-10-20，后由下方记录 supersede）：当时 dry-run selector 已提供并实测 `first-wave` 4 例/8 槽、`remaining-smoke` 20 例/40 槽、`full` 286 例/572 槽；当时它只选择冻结 trial，持久 phase runner 与 resume 尚未实现。后续已新增仅限 first-wave 的 `execute_public_phase()` 与单路线受控执行入口，详见下方“首波 phase scheduler 验证”；provider/session factory、live CLI、真实评分与四域故障闭环仍待完成。

本轮最终验证补充（2026-10-20）：最新 `uv run --extra dev pytest evals/deterministic -q` 为 `1803 passed, 13 skipped (258.87s)`；全仓 Ruff 和 OpenSpec strict validation 通过（6 passed, 0 failed），`git diff --check` 通过。此次与计费证据相关的 model dispatch + public ledger + route execution 定向套件为 113 passed。此前 `1799 passed` 是计费证据聚合接口实现前的快照。新增 phase 证据为 `public-evaluation-phase-first-wave-dry-run-20261020.json`（4/8）、`public-evaluation-phase-remaining-smoke-dry-run-20261020.json`（20/40）和 `public-evaluation-phase-full-dry-run-20261020.json`（286/572）；三者均 0 live calls、0 ledger。无真实模型请求，正式 benchmark 仍为 0/286，`--execute` 继续关闭。

前一轮工程验证（2026-10-20）：全量 `evals/deterministic` 为 `1776 passed, 13 skipped`，全仓 Ruff 通过；新增旧进程恢复/接管竞态测试通过。使用一次性隔离 PostgreSQL 容器运行 `evals/integration/test_business_route_execution.py` 为 `2 passed`。首跑暴露集成测试把两路线标成不同 scripted model 的 fixture 错误；将二者改为相同模型身份后复跑通过。临时容器已停止并删除，其他开发/业务容器保持原状。该集成只验证生产 PlanRuntime/MCP 执行兼容，不是公开 benchmark 路线或模型评分。

模型身份 drift 修正后验证（2026-10-20）：`test_model_profile_drift_stops_route_before_official_scoring` 与既有 unknown-model/unknown-tool 不评分测试及 `test_public_model_dispatch.py` 合计 `18 passed`；涉及 Python 文件 Ruff 通过。该定向集通过不替代全量回归；当前未发生任何 live Qwen/Jev 调用，正式评测仍为 0/286，`--execute` 继续 fail closed。

本轮新增工程验证（2026-10-20）：全仓 Ruff 通过；全量 evals/deterministic 最近一次为 1789 passed, 13 skipped，早于新加入的模型资源生命周期变更。新增定向套件 12 passed，覆盖 Qwen unknown-price 在已批准请求/Token/墙钟硬上限内按 null cost 结算、Jev unknown-price 拒绝派发，以及 `ControlledRoutes` 在官方评分后关闭 managed model resources、关闭失败标记 cleanup_failure；包含实际 `LedgeredModelAdapterFactory` 与 scripted PlanRuntime 路线测试。前一轮独立临时 PostgreSQL 容器中 `evals/integration/test_business_route_execution.py` 为 2 passed；覆盖认证 loopback MCP/生产 PlanRuntime，不代表 benchmark provider/model callback 集成。PR #1 的 Build package/container、Python 3.11、Python 3.13 checks 均通过，但只验证 release workflow 改动，不覆盖当前未提交评测代码。公开任务仍为 0/286；没有新增 Qwen、DeepSeek 或 Jev 请求；--execute 仍关闭。

## 11. 整体回归与本机交付

- [x] 11.1 在隔离数据库运行确定性与集成套件，包括旧 repair/business-2 和 MCP 现代/兼容协议、取消/租约/确认/未知回执；运行项目 ruff 检查并保存摘要，缺数据库造成 skip 不能当集成通过。验收：确定性套件 `1398 passed, 3 skipped`；集成套件连接本机临时 PostgreSQL（`DECAGENT_TEST_DATABASE_URL` 指向专用测试库）`594 passed, 22 skipped`，耗时 20:53；`uv run --extra dev ruff check decagent evals` 全部通过。两个套件分开运行，避免同名测试模块在一次 pytest 收集时冲突。跳过项保留为环境专属/可选 Docker、已固定 benchmark 镜像或 MCP CLI 验证，不把缺数据库的 skip 计为通过；业务数据库未被用于测试。测试容器 `decagent-openspec-261017-pg` 为临时实例，验收后删除。

11.1 最新复验补充（2026-10-20）：在 MCP 503 传输错误分类修复后，`uv run --extra dev pytest evals/deterministic -q` 为 `1811 passed, 14 skipped`；完整 PostgreSQL 集成为 `604 passed, 24 skipped`，两 worker 用时 `1309.91s`（21:49）。前一次串行集成为 `603 passed, 24 skipped, 1 failed`；唯一失败由 pinned MCP SDK 将 HTTP 503 编码为 `INTERNAL_ERROR` 导致分类错误，现已修复，实际 503 HTTP fixture 和整个 `evals/integration/test_business_mcp_client.py`（`6 passed`）复验通过。全仓 Ruff 与 OpenSpec strict（6 changes）通过。临时 PostgreSQL 容器已清理。两 worker 在 Windows 需要 `PYTHONUTF8=1` 与 `PYTHONIOENCODING=utf-8`，否则 xdist worker 启动时可能因本地路径编码失败；这是测试启动环境约束，不改变测试分母。上述仅完成工程回归，不构成任何真实公开基准模型分数或 Jev 收益证据。

11.1 跨平台 CI 复核（2026-10-20）：候选 `1004f7a7c7f58addce34d4c88d4a0ff9f6fec998` 的 GitHub Python 3.13 确定性 job 有 87 项因任务清单 LF/CRLF 原始字节摘要不匹配而失败；已修复 builder 与 loader 的规范化校验，并加入模拟 Linux checkout 的回归。修复后本机确定性测试 `1903 passed, 17 skipped`，专用 PostgreSQL 集成 `604 passed, 24 skipped`，全仓 Ruff/OpenSpec strict 通过；修复后精确候选 SHA 的远端 CI 尚待运行。公开评测未启动。
- [x] 11.2 构建发行包并在源码目录外验证新增 schema/模板/静态资源和启动入口；本机真实 MCP wire 场景验证新旧版本恢复与回退入口，确认没有重新引入御龙真实后端依赖。验收：`uv build` 生成 sdist/wheel；wheel 安装至 `%TEMP%\decagent-package-261017\site`，从源码目录外执行 `python -m decagent --help`，并验证 `decagent.cli:main` 入口、数据库 schema、Skill、manifest、流程 schema 和工作台静态资源均在发行包内。`ops/mcp/verify_wheel.py` 实际连接安装后的 wheel 和 loopback MCP 服务，验证 MCP SDK 2.3.0 / 新协议 `2026-07-28`、14 个工具、prepare/commit/receipt，0 次模型调用。定向集成 `test_business_mcp_server.py`、`test_business_conditional_mcp.py`、`test_business_conditional_worker.py`：`17 passed`，覆盖兼容协议 `2025-11-25`、敏感操作审批、真实 MCP 操作未知回执恢复，以及 business-2 恢复/business-3 回退。`decagent`、`pyproject.toml` 与 `requirements.lock` 未发现御龙运行时依赖；OpenSpec strict validation 通过。临时 PostgreSQL 已移除；源码外 wheel 安装目录保留在 `%TEMP%\decagent-package-261017` 供复核，不属于仓库源码或发布产物。
- [x] 11.3 逐项核对需求—场景—测试—实测证据与指南，移除新业务默认路径上失效的重复队列逻辑但保留必要旧任务兼容；交付当前完成/待办清单，独立 Linux 部署仍标后续工作。证据：`generated/261018/评测/2252_评测阶段审计与剩余门槛.md`；business-3 使用独立条件图与 PlanRuntime，旧 queue 执行仅由 business-2 兼容路径使用，定向 PostgreSQL 分派/恢复 `6 passed`。审计逐项保留 9.6 与 10.1–10.6 待办和 Linux 后续项；没有把环境/协议冒烟当正式模型成绩。

## Verification commands and scope

### 2026-10-18 已确认计划与最新实施证据

- 本轮复核（2026-10-18 22:52，北京时间）：全量 Ruff 通过；当前确定性套件 `1528 passed, 13 skipped (208.49s)`；路由执行与失败分类定向 `51 passed (27.71s)`；business-2/business-3 分派 PostgreSQL 定向集成 `6 passed`；EnterpriseOps CSM/ITSM 环境冒烟 `2 passed`；τ retail ReAct/Plan 澄清及原评测器协议冒烟 `2 passed`（脚本回调，仅协议证据）；对照 manifest 可复现，OpenSpec `validate --changes --strict` 为 `5 passed, 0 failed`。本轮 Qwen/Jev 新请求为 0，公开任务正式尝试仍为 `0/286`。完整 PostgreSQL 集成重跑在约 23% 时中断，没有整体结果；临时数据库已移除，不能记为通过。


- 9.6A（完成，execution-only）：复用冻结 business-2 MCP 包，历史 schema 11 与当前 schema 17 各自使用隔离数据库，以相同版本化种子初始化并核对实际业务行；在实际 MCP handshake、完整 tool schemas、身份/权限 probes 与治理预检一致后执行同一冻结创建工单样例。强化后的 gate 可重算来源、种子、case、身份、工具及治理摘要，并校验获准的精确参数、权限边界、请求人、行数、业务工单字段与两侧结果一致性。最新证据 `evals/benchmarks/evidence/business2-execution-only-pair-20261019-rerun-54tests.json` 为 `execution_only_eligible`；两边各写 1 张相同业务字段工单，模型/Jev 调用 0。配对 runner 的 DSN 通过 stdin bootstrap 而非 argv 传递；失败清理已覆盖。定向 54 tests、Ruff 通过，独立只读复核通过。该证据只证明执行兼容性，不证明模型质量、性能或 Jev 收益；9.6 总项仍待 B–F。
- 9.6B（部分完成，2026-10-19）：已交付默认 EnterpriseOfficialScorer，导入前按固定archive逐文件核对上游代码；同一次session传原私有configs/database/context，原verifier的all-passed决定任务成功，error/异常/无效结果归评分失败。fresh状态不匹配在模型/工具/verifier前拒绝，初末指纹与有界评分摘要持久化；未测副作用为null/not_measured。Judge逐请求核对用量，unknown持续阻断并保留未知消费；route遇任何模型用量未知均跳过评分，防止追加裁判调用。原VerifierEngine合成SQL/tool/response 4 passed，CSM/ITSM真实Docker重置+新适配器合成检查2 passed（16.39s），相关离线组合139 passed，独立复核通过。均非正式任务得分；原官方任务+真实模型+完整verifier同次闭环仍待放行后的正式试次，本分项与9.6总项不提升为全部完成。
- 9.6C：τ 子进程保持用户模拟器、Agent MCP 工具、数据库状态和原始 evaluator 在同一个隔离环境；官方 `SimulationRun` 使用的完整 transcript 先写入试次 SQLite，再从该快照交给原 evaluator。固定 retail train 工程冒烟中 ReAct 与 Plan MCP 的澄清路线通过，原 evaluator 产生了数值评分；这些脚本响应只验证协议，不能作为模型成功率。语义路由在结构化分支上被正确拒绝；缺少经过审查的语义模板时，该类 Jev 路由保持 N/A。
- 9.6D：没有将模板缺失伪装成 Jev 样例；尚无可用于公开基准的经审查可信模板，配对语义比较待办/N/A。结构化分支由规则处理。
- 9.6E：`ComparisonTrial` 已固定 exposure group 与 resource-profile digest，并增加公开对照 manifest 生成器 `evals/benchmarks/build_trial_manifest.py`。清单固定 206 个 EnterpriseOps 与 80 个 τ-Bench 全量案例、另列 20+4 个冒烟案例及每试次隔离策略；当前 `pre_registered_not_run`。任务族 exposure 映射仍未独立审查，所有案例显式 `unassigned`；冷/热与记忆消融仅是预注册方案，尚无正式配对结果。
- 9.6F：固定任务清单与受控容器/τ 路由冒烟基础已在；仍须完成正式代表案例的原 verifier 流程、受控 scorer-error 检查后才能放行全量评测。历史 business-2 execution-only基线已完成；跨域完整运行与正式配对仍未完成，因此 9.6 不能勾选。
- 10.1：加入先经当前编译器校验的只读格式示例后，执行获准的单次重测；仅发出 1 次千问请求，超时并保留未知用量。证据 `evals/benchmarks/evidence/live-primary-retest-20261018T054122Z-d2bfd4d7.json`。不再追加千问请求，不切换模型；Jev 累计余额仍为 `$0.992531602`，本轮未新增 Jev 费用。固定 EnterpriseOps 镜像已拉取且 `validate_source_manifest.py --for-scoring` 通过；它只证明来源与镜像准备，不能证明正式流程成绩。
- 10.2：自有集仅用于辅助回归，不用于阈值定策。`eligibility-diagnostic` 已产出 `evals/benchmarks/evidence/eligibility-20261018.json`：工作流六族各 40 条、记忆三族 20/10/10 条，均不足 100；标签未独立复核，旧预测属于 v1，不能混用 v2。此证据为样本上限诊断，准确率/阈值为 null，不冒充已执行阈值扫描，因此任务仍未勾选。
- 10.3–10.4：公开对照预注册清单现固定 EnterpriseOps-Gym 206 例主评、τ-Bench 80 例补充评测，正式尝试仍为 0；依赖主模型的实网运行因 readiness 超时及未知用量停止。脚本化 train 路线冒烟不改变正式分母，也不代表模型得分。自有合成样例只在辅助回归栏报告。
- 10.5：使用者参与六个合成场景只作单人探索性证据；不能替代原员工真实交互研究，该项仍待办。
- 10.6、11.3：先交付阶段证据和一致性清单；未运行的正式评测、真实员工研究及后续独立 Linux 验收均保持待办，不以报告文件存在代替验收。

以下是 apply 阶段的基础检查，从 DecAgent 根执行；真实模型评测按第 10 组任务和累计预算进行。

~~~powershell
python -m ruff check decagent evals
python -m pytest evals/deterministic -q
python -m pytest evals/integration -q
~~~

集成测试前必须把 DECAGENT_TEST_DATABASE_URL 指向专用可派生测试数据库，不使用业务库连接；需要数据库的 skip 记录为未执行。新评测命令在第 9 组实现并写入文档，不在本计划伪造已存在的 CLI 子命令。真实调用始终遵守累计账本和预算。

### 2026-10-20 公共部署 Runner 边界复核

按计划检查公共 GitHub Actions 发布路径后发现 `main` 仍有旧 `deploy-local` self-hosted job，而公共仓库 Runner 数量和本机 Runner 服务/进程均为 0。PR [#1](https://github.com/shuaibilx/DecAgent/pull/1) 仅删除该部署 job，保留 CI 和 GitHub-hosted GHCR 发布；Build/container、Python 3.11、Python 3.13 checks 均通过后于 2026-10-20 03:05 UTC 合并，commit `0c747a396a360cc168d62c261f5d3dcd329cfb82`。合并后 GitHub API 再次确认 `main` 没有 `deploy-local`/`self-hosted` job，仓库 Runner 数量仍为 0；本机无 Runner 服务或进程。未触碰现有 Compose 容器。未来可信 CD 迁入私有部署仓库，不由公共仓库直接调度本机 Runner。

## Workflow follow-up

- 本 change 已获用户 apply 授权；按未完成任务继续实施并记录验证证据。
- 本 change 完成并经请求归档时再同步主规格；不要在实施中替本用户隐式归档历史 change。
- 独立 Linux 主机部署/验收、Pi 实现、真实公司 API/SSO 属于后续需求，不因本 change 文档完成而宣称已交付。

### 2026-10-19 公开评测实施进度（未改变任务勾选状态）

- 已按选择将公开评测列为后续首要目标；Jev 只评估自然存在且经审核的语义决策，当前清单不伪造 Jev 案例。
- 10.8 部分：建立无密钥的 Qwen/Jev 角色 profile manifest，并校验 profile 指纹；公开对照关闭跨模型 fallback。独立 attempt 现记录 provider、model、endpoint、price version、角色内 attempt 序号、profile digest、可用 request/generation ID、HTTP status 与 latency。由于这些字段尚未通过真实 provider callback 接到正式 route 调度，fallback/重试和逐次真实 usage 的端到端验收未完成，10.8 保持未勾选。
- 10.10 部分：公开试次 runner 默认 dry-run；新增仅写入新 evidence 文件的报告选项，dry-run 下拒绝 `--state`，适配器未接通时 `--execute` fail closed。SQLite ledger v2 补充工具调用/机器时间的预留、结算、unknown 与恢复约束；model、tool、auxiliary role 配额独立检查。该账本尚未接入真实的 `ControlledRoutes`/benchmark 调度，且 τ step、live timeout 与三路线完整预算执行仍未闭环，10.10 保持未勾选。
- 2026-10-19 预检证据：smoke 清单 24 例（EnterpriseOps 20、τ 4）与 full 清单 286 例（EnterpriseOps 206、τ 80）均通过来源/profile/readiness/本地镜像预检。route slots 分别 48 和 572；两者均为 dry-run、0 模型请求、0 MCP 请求、0 ledger、0 benchmark attempt，不计任务成功或失败率。证据：`evals/benchmarks/evidence/public-evaluation-dry-run-smoke-20261019.json`、`public-evaluation-dry-run-full-20261019.json`。
- 验证：公开 runner、profile、配对边界与 inventory 定向 pytest 46 passed；涉及文件 Ruff 通过。9.6、10.3–10.6 仍未完成，不能将 dry-run 预检表述为正式冒烟或全量结果。

### 2026-10-20 公共试次身份与 resolved provider 修正（不改变任务勾选）

`PublicComparisonTrial` 现在在不改变公平性 fingerprint 的情况下绑定规范 manifest trial ID。phase scheduler 和单路线入口在任何 route reservation 前验证该 ID、benchmark、版本、case ID、exposure、资源策略、回复策略及全部角色 profile digest；被绑定到其他任务或 profile 的试次会 fail closed。模型账本仅记录 callback 实际返回的 `actual_provider`，上游未提供时保留 null，请求 provider 仍记录在冻结 profile 中。先加入身份回归再实现修复；route/comparison 定向 suite 为 `32 passed`，实际 provider 缺失断言先失败、改动后通过。该修正不连接真实 provider/session factory，不改变 9.6/10.10.D 的状态，也不开放 `--execute`。正式公开基准仍为 0/286。

最新验证（2026-10-20）：完整 `evals/deterministic` 为 `1858 passed, 14 skipped (287.53s)`；全仓 Ruff 通过，OpenSpec strict 为 6 passed/0 failed，`git diff --check` 通过。`run_public_trials.py --phase first-wave --check-images` 仍为 `preflight_passed_no_execution`（4 固定任务、8 route slots、0 ledger、0 model、0 MCP）；同一命令加 `--execute` 仍在创建 ledger 前 fail closed。此次无 Qwen、DeepSeek、Jev 调用，未改任务勾选或基准分数。

### 2026-10-20 τ step 独立计数与上限

实现 `tau_step_attempts` 持久表与 `PublicTrialLedger.reserve_tau_step()`，路线 resource snapshot 独立返回 τ step 与 MCP 工具数。固定 tau2 commit `96a3ef9` 的 `Orchestrator.step()` 定义作为来源：逐个 Agent/User/Environment handoff 计一；批次工具按一个 Environment handoff 计步，模型/MCP attempt 仍另计。`ControlledRoutes` 在对应的模型 simulator 或业务工具调用前占用 step，Plan `decide` action 也计入；路线触顶停止后续模拟动作，官方 scorer 仍受单独 judge 预算约束。已有 unknown route 不重放，resume 保留 step count；supervisor receipt 与执行结果须和持久 ledger 数值一致。

先写回归并观察到失败后补齐：独立 200 步边界与 ledger 恢复、step 上限阻止工具 dispatch、τ 用户多工具批次只计一 Environment step、Plan semantic decision 计一个 Agent step，以及 unknown receipt 保留账本计数。受影响四个 deterministic 文件首次通过 `145 passed`；新增两条精确映射测试通过 `2 passed`。最新完整 `uv run --extra dev pytest evals/deterministic -q` 为 `1861 passed, 14 skipped (289.55s)`；全仓 Ruff 通过、OpenSpec strict 为 `6 passed, 0 failed`、`git diff --check` 通过。详细证据：`evals/benchmarks/evidence/tau-step-accounting-20261020.json`。14 个 skip 仍单列，不作为 PostgreSQL/live benchmark 通过。此工程子项不等于正式模型试次，公开任务仍 0/286、live `--execute` 未开启。

### 2026-10-20 受门禁保护的 first-wave CLI（未执行真实路线）

`live_public_runner.py` 补齐只读候选检查：工作树必须干净，GitHub 上必须存在同一个 HEAD SHA 的已完成成功 `CI` workflow。`run_public_trials.py --execute` 只接受 `--mode smoke --phase first-wave`、一个新的 evidence 报告路径和规范 τ Python；始终强制检查 readiness/runtime、两个本地摘要固定的 EnterpriseOps 镜像、主模型凭据及关闭跨模型 fallback。四个冻结任务的 τ/Enterprise 环境、评分输入边界和清理会在打开规范账本前全部预检；缓存的预检试次随后交给既有 supervisor、账本、ControlledRoutes 和 `execute_public_phase()`，不增加第二套执行引擎。dry-run 仍为默认，`--state` 不能改写规范账本路径。

新增回归覆盖候选 SHA 与 CI 匹配、脏工作树在 GitHub 查询前拒绝、CLI 缺少 phase/report 时不触发候选 gate、live 参数强制 images/runtime/readiness 检查、报告新建及四域环境先于 ledger 的顺序。`test_live_public_runner.py`、public ledger、model callback 和 route execution 定向 suite 为 `151 passed`；相关 Ruff 通过。`run_public_trials.py --phase first-wave --check-images` 为 4 固定任务/8 槽、0 ledger/模型/MCP 调用，摘要固定 EnterpriseOps 镜像本机可用。当前实际工作树含未提交修改，`--execute` 会在候选 gate 处停止；没有创建账本、模型 API 请求或业务工具调用。四域受控模型响应闭环、强制取消、故障注入及官方评分/清理仍未验收，10.10.D/E 和 9.6.F/G 继续未勾选，公开任务仍为 0/286。

首次实际构造四域环境时发现 telecom policy 不是通用 `policy.md`：固定 τ 上游 `telecom/environment.py:get_environment()` 会组合 `main_policy.md` 与技术支持 policy。评测 runner 原先按 retail 路径硬编码，导致 telecom 环境未开始初始化即失败。新增 TDD 回归并改为从原始 τ environment `get_policy()` 读取完整政策；修复后重新创建并清理 CSM、ITSM、τ retail、τ telecom 四个真实隔离环境均通过。证据 `evals/benchmarks/evidence/public-first-wave-environment-preflight-20261020.json` 明确标为 environment-only，账本未打开、Agent route/官方评分未运行、模型请求和 benchmark 工具 dispatch 均为 0。新 τ policy adapter 回归和 live runner suite `8 passed`；这修复并验证环境前置条件，但不等于任何正式路线或任务得分。

随后为 live CLI 的配置读取增加 `.env` 项目根隔离，避免 `--root` 指向 DecAgent 时误加载启动目录（例如 Waku）的模型配置；`Settings.from_env()` 返回前会恢复 `PYTHON_DOTENV_DISABLED` 原值。专门回归使用两个不同 `.env` 验证只读取所选项目配置。该后续改动的 live-runner/τ policy 定向套件为 `9 passed`，全仓 Ruff 和 OpenSpec strict 通过。上方 `1893 passed, 16 skipped` 是 `.env` 隔离改动前的完整 deterministic 快照，不能冒充含此新增回归的全量结果。

### 2026-10-20 τ 模型回调 deadline 传递

`_StdioChannel` 现在在 τ `parent_call` 前计算剩余 deadline，并传给接受 `timeout_seconds` 的 callback；`_tau_model_callback`、`ObservedUsage` 和 `LedgeredModelCallback` 将其一路传递，并与 profile、route、batch 截止时间取最小值。三个新增回归先验证失败，再修复后定向 suite `39 passed`。不会强制取消忽略 timeout 参数的任意同步 callback，统一 route supervisor 和工具传输取消仍未实现，因此 10.10.D/E 继续待办。

首波只读预检写入 `evals/benchmarks/evidence/public-evaluation-phase-first-wave-preflight-20261020-timeout-ready.json`：4 个固定任务、8 条 ReAct/Plan 路线槽位，镜像检查通过，账本创建 0、模型请求 0、MCP 请求 0。修改后完整 `evals/deterministic` 为 `1817 passed, 14 skipped (243.70s)`；全仓 Ruff、OpenSpec strict（6 passed）及 `git diff --check` 通过。`evals/integration` 为 `9 passed, 619 skipped`，没有配置专用 `DECAGENT_TEST_DATABASE_URL`，依赖隔离 PostgreSQL 的用例未执行。该预检是 dry-run，不是首波执行。

### 2026-10-20 首波 phase scheduler 验证（任务仍未完成）

新增 `execute_public_phase()` 后，定向 `test_business_public_execution.py` 为 `12 passed`，覆盖冻结的 8 槽顺序、Qwen unknown 路线不重放但继续其他 pending 路线、恢复时跳过已终态路线、持久 batch stop 后不加载 trial/session，以及真实 SQLite ledger 关闭并恢复后已完成路线不重放。完整 `evals/deterministic` 为 `1837 passed, 14 skipped (268.23s)`；`uv run --extra dev ruff check decagent evals` 通过；`openspec validate --changes --strict` 为 `6 passed, 0 failed`；`git diff --check` 通过（Windows LF/CRLF 提示不构成 whitespace error）。这不是 live phase 运行；无新增模型/MCP 请求，公开任务仍 `0/286`，provider/session factory、工具及同步 callback 强取消、四域真实评分与 cleanup 仍待完成，10.10.D/E 和 9.6.F/G 不勾选。


### 2026-10-20 CI 第二轮失败与修复（不勾选任务）

远端 run `39435322121` 在候选 `2b65a2a1afc21074385caa3874c139833e363bfb` 的 Python 3.11 deterministic job 有 84 个失败，原因是离线测试依赖未随仓库提交、由本机 `.env` 或下载缓存提供的外部 benchmark 附件。`load_public_run_plan()` 的来源校验保持默认开启；纯离线测试显式传 `check_source_files=False` 与 `check_runtime=False`，新增回归确保默认预检对缺失来源仍失败。live CLI 未更改，仍执行 runtime、source、image、readiness 和候选 SHA 检查。

同时将 50ms lease expiry 测试改为直接写入已过期 UTC 时间，避免 CI 机器调度慢于测试时限而造成非确定性失败。最新全量本地 deterministic 为 `1904 passed, 17 skipped`，受影响测试组复验进行中；远端 CI、对应 OpenSpec strict、PostgreSQL、本机全仓最终结果待本候选提交后复验。没有模型请求或正式试次派发，benchmark 仍为 `0/286`。

### 2026-10-20 CI 第三轮失败及修复（任务仍未勾选）

候选 `60ea6f6dce5855cb078c4796abeaa3708d988247` 的 CI run `39438714094`：Python 3.11 deterministic 失败 `3 failed, 1901 passed, 17 skipped`，Python 3.13 因 fail-fast 取消；package/images、wheel/MCP 协议、offline Compose 和 workbench browser jobs 通过。三个失败分别来自默认 dry-run 读取本地 live `.env`，以及 τ process 仍以原始字节检查 Linux LF inventory。`run_public_trials.py` 的 dry-run 已关闭 live runtime/source-file 检查；live 执行分支继续显式校验两者。τ bridge 和官方 runtime verifier 改用共享 CRLF 规范化 `task_inventory_sha256`。新增 Linux LF 回归后相关 suite `91 passed, 13 skipped`，τ 独立解释器 verifier CLI 回归 `1 passed`；最新完整 deterministic `1906 passed, 18 skipped (224.22s)`，远端最终候选复验待完成。pinned τ Python 3.12 的 `verify_tau_runtime.py` 初始化核验 80/80 ready，0 模型请求；Ruff、OpenSpec strict 与 `git diff --check` 通过。

按计划核对本机发布安全边界：仓库没有 self-hosted Runner 注册（GitHub API `total_count=0`），本机无 Runner 服务/进程；默认分支 release job 只在托管 GitHub runner 发布 GHCR。没有需要关闭的公共本机部署 job，也没有停止现有 DecAgent 容器。此状态不代表私有 Runner/CD 或外网部署已完成。
