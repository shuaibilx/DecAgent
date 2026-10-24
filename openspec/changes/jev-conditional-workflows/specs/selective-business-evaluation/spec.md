# Spec Delta

## Purpose

以可复现的完整模拟业务工作流评测，验证条件计划、Jev 运行时语义判断、置信度门控和人工接管是否改善任务质量及执行效率，并明确 benchmark 适用范围、统计不确定性、人工成本与未通过的指标。

## ADDED Requirements

### Requirement: Reproducible full-workflow benchmark environments

EnterpriseOps-Gym CSM/ITSM MUST 作为主要端到端工作流 benchmark；τ-Bench MUST 作为对话、澄清和用户模拟的补充 benchmark。评测 MUST 固定来源、版本/提交、许可、任务 ID、工具/API 版本、容器镜像摘要和初始数据库快照。原始任务目标、工具和成功条件 MUST 保持不变；部分运行 MUST 报告任务清单和覆盖范围，不得声称完成全量 benchmark。孤立意图分类数据 MUST NOT 作为完整 Agent 工作流成功的主要证据。EnterpriseOps 的 206 条固定任务均须保留在总报告中，并分别报告冒烟/开发已暴露任务和未暴露任务；后者可以单列为未暴露子集，但 MUST NOT 称为独立 holdout。τ-Bench 官方 test 80 条 MUST 冻结，不用于模板、提示或策略调优；τ-Bench train 只用于开发与冒烟。

#### Scenario: EnterpriseOps primary evaluation
- **WHEN** 执行主要端到端评测
- **THEN** 使用固定版本的 EnterpriseOps-Gym CSM/ITSM 原始任务、容器化 MCP 服务及其最终状态 verifier，并从规定的数据库初始状态运行

#### Scenario: Fresh EnterpriseOps snapshot is checked by the original task verifier
- **WHEN** 每次为冻结的 CSM/ITSM 任务创建新的隔离容器和数据库
- **THEN** 在任何 Agent 输入前采集私有状态指纹，并将该任务原始 verifier 仅交给 evaluator；任务未执行时记录官方 `task_failure`，环境或评分器异常分别分类，未测的中间副作用保持 `not_measured`

#### Scenario: Tau dialogue supplement
- **WHEN** 执行 τ-Bench 对话或澄清补充评测
- **THEN** 固定域、任务和版本；使用原始 Tool/ToolKit 工具语义或由所有策略共用的 MCP 适配器，单独记录用户模拟器的模型调用和费用。用户模拟器及必要的模型裁判使用配置的千问服务；不启用 τ 上游默认 GPT/Claude 模型或隐式重试

#### Scenario: Tau user simulator emits multiple tool calls
- **WHEN** τ 官方用户模拟器在一个回复中发出多个用户侧工具调用
- **THEN** 评测器 MUST 在同一权威环境中执行整批调用，将全部原始结果按调用顺序作为一个 `MultiToolMessage` 交回用户模拟器；所有结果返回前 MUST NOT 再调用用户模拟器，且完整顺序与 requestor 身份 MUST 保留在评分轨迹中

#### Scenario: Tau simulator receives the pinned telecom user-tool catalog
- **WHEN** 固定 τ telecom 官方用户模拟器向 evaluator 请求包含 30 个 user-tool schema 的模型输入
- **THEN** callback MUST 在不超过 32 个工具 schema 的有界请求契约内接受目录，并继续执行冻结输入 Token 上限；模型返回的单批 user-tool calls 仍受独立 8 项调用上限约束。超出 schema 上限时必须在 provider dispatch 前拒绝并保留可审计的未派发状态

#### Scenario: Smoke cases stay visible in the final report
- **WHEN** EnterpriseOps 冒烟或开发阶段的任务进入后续正式评测
- **THEN** 它们仍计入 206 条总体覆盖清单，同时和未暴露任务分组报告；不得在看到结果后删除、替换或重新归类任务

#### Scenario: Benchmark subset
- **WHEN** 资源限制只允许执行 benchmark 的固定子集
- **THEN** 报告选择规则、全部任务 ID 和未运行部分，不修改样本目标来迎合某条策略

#### Scenario: Pre-registered metadata-only pilot sample
- **WHEN** 从冻结的 v10 inventory 生成本阶段 24-task pilot
- **THEN** 仅按 benchmark、domain、split、exposure 和 case ID 以固定 seed `sop-pilot-20261024-v1` 做 SHA-256 稳定排序，选择 CSM 10、ITSM 10、τ retail test 2、τ telecom test 2；不得读取任务 prompt、目标状态、reference action 或评分材料来抽样、替换或排序。每任务运行 ReAct 与一次规划确定性路线，另为全部 CSM 安排 primary semantic 与 Jev semantic 配对槽，冻结总分母 68；失败、unknown、N/A 均保留。

#### Scenario: Upstream and MCP dependency locks conflict
- **WHEN** 固定 τ2 运行时依赖与应用 MCP SDK 不能安装在同一环境
- **THEN** 保留各自依赖锁，通过每试验独立、无模型凭据且禁止外网的专用子进程桥连接原始工具与 MCP；保留原 schema、参数、结果、错误和可变状态元数据，限制帧大小/等待时间，协议或传输失败不重放工具，关闭与启动失败均回收所属子进程

#### Scenario: All official tasks initialize offline
- **WHEN** 80 个官方 test 任务可加载且两次全新初始化状态一致
- **THEN** 只记环境就绪范围，正式任务尝试数和任务成功率保持独立，不把初始化当任务完成

#### Scenario: τ smoke uses development data
- **WHEN** 选择 τ-Bench 冒烟任务
- **THEN** 从每个域固定官方 train 顺序选择前 2 条；官方 test 80 条保持未触碰，直到任务、模板和报告配置冻结

### Requirement: Exposure labels use case-level evidence

公开评测 MUST 基于精确任务级证据分配 `development_exposed` 与 `not_exposed`；未经审核的任务族启发式 MUST NOT 扩大开发暴露范围。当前审计确认仅 CSM `task_20260106_161859_265_00364ece_0e836ce8` 与 ITSM `task_20260115_201951_323_391f1eff_659b9dae` 两个 EnterpriseOps case 进入项目实测，其余 EnterpriseOps 204 个 case 以及 τ-Bench 官方 test 80 个 case 属于项目级 `not_exposed` 子集。该分类 MUST NOT 改写历史 manifest，也 MUST NOT 被表述为独立 holdout；官方任务仍须保留在分母。τ-Bench train 只用于开发和冒烟，官方 test MUST 在计划、模板、模型和环境配置冻结后执行。

#### Scenario: Reviewed exposure assignment is retained without family expansion
- **WHEN** 冻结新的公开评测 manifest
- **THEN** 将两个已实测 EnterpriseOps case 标记为 `development_exposed`，其余 204 个 EnterpriseOps case 与 80 个 τ-Bench official test case 标记为 `not_exposed`；不根据未审核的 workflow-family 启发式传播标签，且不修改历史 manifest 或官方任务分母

### Requirement: Isolated public tool registration reuses the production interpreter

公开基准 MUST 使用可信评测代码构造的活动作用域目录，复用生产编译器、绑定器和 PlanRuntime。此目录 MUST NOT 能通过生产 PluginSpec/HTTP 任务参数注册。原工具名称、schema、参数与响应 MUST 保留；没有适用的可信 Jev 模板时 MUST 拒绝决定节点，不临时生成问题。隔离日志和副作用 MUST 仅属于当前试验；完成信号 MUST 区分 execution_finished 与官方 verifier 证明的业务成功。

#### Scenario: Public tool accepts a business user ID
- **WHEN** 隔离评测工具的原 schema 包含 user_id 等业务资源参数
- **THEN** 编译与执行按原 schema 接受；生产路径禁止覆盖认证身份的规则不变

#### Scenario: Evaluation scope expires or an upstream write becomes uncertain
- **WHEN** 活动作用域关闭，或原始写调用发生无法核实的传输异常
- **THEN** 关闭作用域不能继续编译/执行，未知写入保留原节点与日志并停止，不产生新请求重放

#### Scenario: Frozen benchmark identifiers retain their source value
- **WHEN** 固定 benchmark 任务 ID 超过一般 metadata 长度但不超过 256 个字符
- **THEN** 隔离作用域保留完整、未规范化的 case ID；benchmark/version 仍限制为 160 字符，超过 case ID 上限的值在加载任务前被拒绝

#### Scenario: Plan finishes but the official goal fails
- **WHEN** 解释器完成了计划节点但官方最终状态 verifier 不通过
- **THEN** 只记录计划执行结束，业务任务失败；不能由计划的自定义完成条件覆盖官方结果

#### Scenario: Upstream returns a known tool error
- **WHEN** 原工具通过成功的 MCP 传输返回 isError=true
- **THEN** 原始错误完整持久化，ReAct 可按原观察恢复，计划使用显式 on_error；不能将已收到的工具错误改为传输未知或伪装成成功

### Requirement: Preserve typed MCP output semantics across evaluator routes

隔离评测适配器 MUST 保留原始 MCP 响应，并按冻结的 output schema 验证成功结果。ReAct 和计划路线 MUST 接收相同的类型化业务值；原始响应仍可审计，规范化不能覆盖或改写它。结果封装使用版本化的 `enterprise-mcp-observation-v2`。

#### Scenario: MCP encodes a schema-declared boolean as a database bit
- **WHEN** 固定工具 output schema 在某字段只声明 boolean，而成功 MCP JSON 结果在该字段返回整数 `0` 或 `1`
- **THEN** 评测适配器保留原始 MCP envelope，将业务 value 中的字段映射为 `false` 或 `true`，记录 JSON Pointer，并让 ReAct 与计划路线观察同一规范值

### Requirement: Fail closed outside supported boolean normalization nodes

评测适配器 MAY 将精确整数 `0` 或 `1` 规范化为 boolean，但仅限内联 `properties`、schema-valued `additionalProperties` 和对象 `items`。它 MUST NOT 隐式展开 `$ref` 或组合关键字；未转换值仍须通过冻结 schema 的严格校验，否则 MUST 以输出契约错误失败关闭。

#### Scenario: A boolean is declared behind an unexpanded reference
- **WHEN** output schema 通过 `$ref` 声明 boolean，而响应对应节点返回整数 `0` 或 `1`
- **THEN** 适配器不进行隐式转换，保留原值，并由严格 schema 校验拒绝该结果

#### Scenario: MCP returns a value outside the boolean wire compatibility rule
- **WHEN** 字段返回 `2`、其他非布尔类型，或其 schema 也允许 integer/number
- **THEN** 不进行隐式 boolean 转换；纯 boolean schema 的非法结果仍按 output-schema validation failure 处理，联合类型按原 JSON 类型保留

### Requirement: Separate data splits and prevent answer leakage

系统 MUST 保留 benchmark 官方任务划分；无官方划分时按业务流程/任务族隔离开发、验证和测试。期望状态、reference actions、标签、答案及 verifier 私有字段 MUST NOT 出现在待测模型输入中。测试集 MUST 不参与模板、阈值或策略调优，评测记忆不得跨测试样本学习。

DecAgent 自有 synthetic reference 集 MUST 仅用于辅助回归与契约诊断，MUST NOT 用于主要业务成绩、阈值定策或运行策略发布。独立阈值验证队列不足时 MUST 保留人工处理和未验证状态，不通过扩大冻结样本或读取 holdout 标签补足门槛。

#### Scenario: Internal regression cases are available
- **WHEN** 自有合成回归集或历史预测可被执行
- **THEN** 结果只列为辅助回归；公开工作流成绩仍来自固定 EnterpriseOps-Gym 与 τ-Bench，不能把不同数据版本的旧预测作为当前验证结果

#### Scenario: Related tasks cross intended split
- **WHEN** 多个任务属于同一业务流程族、状态模板或近重复改写
- **THEN** 它们被放在同一 split 或明确排除，不能同时用于阈值选择和测试证明

#### Scenario: Verifier data is present
- **WHEN** benchmark 包含 expected actions、目标数据库状态或评分器私有字段
- **THEN** 模型只接收允许的原始输入与合法选项，评分器单独读取隐藏标签

#### Scenario: Exposure groups remain evaluation metadata
- **WHEN** 使用经过案例级审阅的公开评测 manifest 生成 dry-run 和 Agent 请求
- **THEN** 暴露组只用于评测配对与结果分组，不进入 Agent 请求或 dry-run 报告；目标状态、reference actions 与私有评分字段继续只由独立评分边界读取

### Requirement: Distinguish semantic and workflow performance

EnterpriseOps-Gym MUST 用于主要企业工作流结果；τ-Bench MUST 只用于其覆盖的客户服务对话和用户澄清补充分析。流程评测 MUST 使用固定初始状态、身份、目标、工具和可验证结果。自有中文模拟流程、公开 benchmark 与语义决策子集 MUST 分别标识，任何节点准确率 MUST NOT 被称为完整任务成功率。

#### Scenario: Correct category but wrong side effect
- **WHEN** 模型正确分类请求却在流程中修改错误资源
- **THEN** 语义分类可计正确，但业务任务失败并计入错误副作用

#### Scenario: Benchmark results are generalized
- **WHEN** 某一公开域或语言达到目标
- **THEN** 报告只对已测任务、域和版本作结论，不推断未测的中文公司业务或其他流程

### Requirement: Route decisions by determinism and evidence

静态可预判分支 MUST 在计划中表达；明确的结构化运行时条件 MUST 由代码规则判断。Jev MUST 仅用于依赖真实工具结果或用户上下文的语义判断，且问题定义 MUST 引用已发布模板、候选 MUST 来自真实且已授权的数据。Jev MUST NOT 替代权限判断、写入确认、回执核实或明确的状态/SLA 规则。

#### Scenario: Structured runtime condition
- **WHEN** 工具返回有明确 schema 的状态码、SLA 或布尔字段
- **THEN** 执行器按版本化规则选择分支，不调用 Jev

#### Scenario: Semantic runtime condition
- **WHEN** 前序 API 返回的自由文本或用户描述必须映射到受限的业务候选分支
- **THEN** 系统只在可信模板和当前授权候选范围内请求 Jev；低确定性或无匹配进入人工处理

#### Scenario: CSM knowledge article choice uses only returned candidates
- **WHEN** CSM 任务要求为用户问题选择已发布的知识文章，且 pinned MCP 工具返回了候选文章
- **THEN** 评测 MUST 仅在隔离 `EvaluationCatalog` 中注册 `csm_knowledge_article@1.0.0-csm-knowledge-1`，并只将同一 `retrieve_knowledge` 节点的实际已发布候选、原始查询参数和 `no_match` 交给 Choice 模板；编译器 MUST 要求查询显式过滤 `state=published` 且不设置 `limit`，运行时 MUST 验证选择的整数 ID 确属该节点返回的已发布记录；不得自行截断候选或暴露 verifier 标签，选择 MUST NOT 授予写入权限；工具契约不匹配、有限查询、无已发布候选、超过 24 个候选或上下文超过 16,000 字符时 MUST 将节点标记为 `not_applicable`。此模板 MUST NOT 通过生产 `PluginSpec`、HTTP 或用户提供的 manifest 注册

#### Scenario: Plan already determines the branch
- **WHEN** 首次规划已能依据现有事实确定唯一分支
- **THEN** 直接执行已验证计划，不增加 Jev 或后续主模型判断

### Requirement: Paired fair baselines and ablations

评测 MUST 在相同冻结任务上比较 ReAct + Skill + MCP、一次规划 + 确定性执行器 + MCP、一次规划 + 确定性执行器 + Jev 语义判断。计划路线的 MCP 调用由执行器依据计划和类型化参数直接构造，不要求每个工具调用都请求 LLM。语义分支以相同状态、可信问题和候选配对比较 Jev 与主模型二次判断；结构化分支单独与代码规则比较，不要求 Jev 胜过代码。失败样本不得事后替换。

#### Scenario: Same workflow across strategies
- **WHEN** 多种策略运行同一冻结任务
- **THEN** 使用相同任务目标、数据库快照、工具实现、身份权限和必要确认规则，不能给某策略额外答案或权限

#### Scenario: Private harness work affects wall time
- **WHEN** 试验需要创建环境、恢复快照、运行官方评分或清理资源
- **THEN** 分别记录准备、Agent 区间、私有评分、清理和总墙钟时间；Agent 机器延迟排除环境准备、评分、清理和已观测的人工/模拟器等待，初始化失败明确标记未启动，不能作为低延迟成功样本

#### Scenario: Dispatched tool outcome is unknown but elapsed time is measured
- **WHEN** 工具调用已标记派发，随后出现 transport 或 deadline 异常，无法确认远端结果
- **THEN** 工具及路线仍必须保留 `unknown` 终态并禁止自动重放，同时保存从派发到异常被观察到的有限非负耗时；不得将其记录成成功、补造结果摘要或据此推断远端副作用状态

#### Scenario: Reuse cost comparison
- **WHEN** 报告零主模型复用收益
- **THEN** 分列首次编排和模板验证成本、命中率及复用执行成本，不将所有成本隐藏到准备阶段

#### Scenario: Jev compared with a second primary-model decision
- **WHEN** 运行时语义分支分别由 Jev 和主模型作出判断
- **THEN** 使用同一输入与选项测量质量、延迟和真实用量；质量未达到目标或没有可测成本/延迟改善时，不宣称 Jev 优于主模型

#### Scenario: Jev replay uses the exact primary plan and decision input
- **WHEN** 对同一试次安排 primary semantic full-workflow 与 Jev semantic execution-only 路线
- **THEN** Jev MUST 接收 primary 路线本进程保留的同一冻结 Plan；两路线只有在 Plan digest 和 decision-input digest 均相同时才计为配对。若 primary 尚未产生可复用 Plan、运行失败或评分失败，Jev 槽 MUST 保留为 failed/incomplete，不得把缺失证据记成 `not_applicable`。仅当当前执行进程已持有 primary receipt 且其已完成、明确没有 decision-input digest 时，Jev 槽才可标记 `not_applicable`。若恢复后 primary 已终态但原 Plan/决策输入不可恢复，且账本没有持久化的“未观察到语义决策”证据，Jev 槽 MUST 标为 incomplete；不得仅凭缺失的内存对象推断 N/A，也不得为配对而重规划、打开环境或派发模型/工具。N/A 单独计数，不作为零成本成功或 evaluated-route 成功率分母；路线覆盖率须另行报告。

#### Scenario: ReAct is not truncated by production-specific limits
- **WHEN** 三条路线进行主质量对照
- **THEN** 每条路线都使用相同评测上限与终止条件；生产预算的结果放在独立副评中

#### Scenario: Frozen business-2 baseline crosses database schema versions
- **WHEN** 冻结的 business-2 MCP 基线与当前计划执行器运行同一服务台样例
- **THEN** business-2 必须使用冻结源码要求的 schema 11 隔离数据库，当前执行器使用其固定版本要求的隔离数据库（当前为 schema 17）；两个环境从同一版本化服务台语义种子初始化，并在执行前核对规范化初始数据、员工身份/角色/权限、工具语义和治理配置摘要。不得改写冻结 business-2 源码、不得让两个环境共用不兼容数据库，历史基线单独报告且不并入公开基准三路线主结果；前置摘要不匹配时该样例不得计为公平配对。

### Requirement: Evidence-grounded final responses

公开计划路线 MUST 默认从当前试次实际工具返回确定性渲染最终回复；计划目标、预期状态和 verifier 私有字段 MUST NOT 被当作已执行事实。最终回复策略 MUST 在运行前冻结并进入试次公平身份。预注册公开输出契约无法由确定性渲染满足时 MAY 经显式注入的 Qwen 回调总结一次；请求/token/费用/延迟 MUST 全部计入 Agent primary，用量未知立即停止且不重试。总结 MUST 只接收公开任务/政策与真实观察，MUST NOT 基于官方评分失败触发或修改任务。

#### Scenario: Deterministic evidence is sufficient
- **WHEN** 已完成计划的完整工具证据可在预注册输出限制内表达
- **THEN** 返回带来源引用的确定性结果，不追加主模型请求，不用 goal.description 伪造最终回复

#### Scenario: Narrative or bounded output requires summarization
- **WHEN** 公开输出契约预先要求叙述，或完整证据超过确定性输出限制，但能放入总结输入预算
- **THEN** 最多调用一次受控 Qwen 总结，验证回复与来源引用并计入 Agent primary；没有回调、证据超预算或未知工具结果时明确停止，不删任务或静默截断

#### Scenario: Summary usage is unknown
- **WHEN** 总结请求失败且 token 或费用未知
- **THEN** 保留已派发请求及未知用量，不重试、不发布完整费用或零 LLM 复用结论

#### Scenario: Unknown agent usage would be followed by an official judge
- **WHEN** 规划、语义判断或总结的用量未知，而任务原评分器可能调用模型裁判
- **THEN** 跳过评分及所有后续裁判请求，保留未评分状态与 score/run=null，不以确定性评分器的偶然拒绝代替此门禁

#### Scenario: ReAct and Plan share an output budget
- **WHEN** 两条路线使用同一个冻结输出策略
- **THEN** 最终回复都按该策略的 UTF-8 字节上限校验，不给 ReAct 额外字符额度或更宽输出限制

#### Scenario: Official score is private and final
- **WHEN** 最终回复交给官方 verifier
- **THEN** 不将 verifier 配置、gold 或评分结果送回总结器，也不根据评分重写回复

### Requirement: Pre-registered route ablations

新路线 MUST 按预先冻结的配置区分门控、冷启动/已验证复用和记忆消融。每个试次使用独立初始状态；未命中缓存、模板成本和失败任务不能从结果中删除。

#### Scenario: Workflow reuse costs are visible
- **WHEN** 报告热复用收益
- **THEN** 首次规划与模板验证成本、命中率及热执行成本分开报告，且每次任务仍读取新业务事实和权限

#### Scenario: Memory ablation is isolated
- **WHEN** 比较启用或禁用记忆
- **THEN** 只使用试验前冻结、显式绑定的记忆配置，不允许前一测试样本写入并影响后一测试样本

### Requirement: Equal evaluation and production resource profiles

主质量比较 MUST 对所有路线使用相同评测上限：每任务最多 200 次 Agent 模型请求、200 次工具调用、1,000,000 个模型 token、30 分钟 Agent 机器时间；τ-Bench 同时遵守官方每任务 200 步上限。生产预算 MUST 作为独立副评使用当前生产限制。用户模拟器及裁判用量单列于 Agent 请求，同时计入总成本和总墙钟。

#### Scenario: ReAct has equal completion opportunity
- **WHEN** ReAct 与两条计划路线进行主质量对照
- **THEN** 三条路线遵守同一宽松上限，不把生产 primary hard limit 6 次用于提前截断 ReAct

#### Scenario: Production limits are measured separately
- **WHEN** 展示生产预算副评
- **THEN** 使用当前生产配置并与主质量比较分表呈现，不混合两种预算的分母或结论

### Requirement: Validation-selected thresholds with quality target

默认策略 MUST 在验证集上选择自动部分点估计正确率至少 95%、有效自动样本至少 100 的候选中覆盖最高者；并列取更高阈值。每个适用决策族 MUST 独立核查。无合格候选或样本不足 MUST 不发布合格默认策略，不降低质量目标。

#### Scenario: Several eligible thresholds
- **WHEN** 多个阈值都满足验证正确率与样本量条件
- **THEN** 选择自动覆盖率最高者，记录所有候选指标和选择理由

#### Scenario: High accuracy on too few automatic samples
- **WHEN** 阈值只自动处理 5 条且全部正确
- **THEN** 报告 5/5 及其区间并标样本不足，不能以 100% 宣称默认策略通过

#### Scenario: Validation target never met
- **WHEN** 所有候选阈值的自动正确率均低于 95% 或有效自动样本不足
- **THEN** 保留人工/待验证策略，不放低目标或使用测试标签重新选阈值

### Requirement: Frozen test and honest statistical uncertainty

正式测试 MUST 冻结模型实际版本、问题/计划/策略与数据清单。自动部分测试正确率低于 95% MUST 标为未达目标；比例 MUST 报告分子分母和 95% 统计区间，相关节点 MUST 按任务/组处理相关性。confidence 阈值 MUST NOT 被当作正确率或统计区间。

#### Scenario: Test fails after validation succeeds
- **WHEN** 验证所选策略在冻结测试集上只有 93% 自动正确率
- **THEN** 报告未达到 95% 目标，不重新挑选测试阈值并覆盖原结果

#### Scenario: No automatic sample
- **WHEN** 某阈值让所有样本转人工
- **THEN** 自动正确率标为不可计算，覆盖率为零，不能记作 100% 正确

#### Scenario: Clustered multi-node tasks
- **WHEN** 一个任务产生多个相关 Jev 判断
- **THEN** 流程差异的统计按任务或数据组计算，不将这些节点当独立任务夸大样本量

### Requirement: Correct counterfactual threshold evaluation

单判断阈值扫描 MUST 可复用同一原始预测；缓存 MUST 绑定状态、问题、候选/权限和实际模型版本。人工选择改变后续分支时 MUST 执行对应新轨迹，不能据一条固定轨迹声称测量了其他阈值的完整流程结果。

#### Scenario: Offline threshold sweep
- **WHEN** 对固定输入的同一 Jev 答案扫描 0、0.50、0.70、0.80、0.90、0.95、0.99
- **THEN** 系统不重复付费请求，只重新计算门控与单判断指标

#### Scenario: Human correction changes branch
- **WHEN** 另一个阈值使人工纠正早期决定并改变后续工具状态
- **THEN** 系统重新推进相应流程，只对输入完全一致的判断复用缓存

### Requirement: Distinguish intervention types and human assumptions

报告 MUST 分开语义接管、参数补充、强制写入确认和未知结果核实，统计中断次数、等待与全任务无人介入完成率。标签模拟人工 MUST 明确标为理想人工假设，不能证明实际员工正确率或响应速度。

#### Scenario: Oracle simulation
- **WHEN** 用标准答案模拟人工纠正
- **THEN** 报告标记 oracle，不把模拟等待时间或完美选择当真实生产效率

#### Scenario: Required write confirmation
- **WHEN** 所有语义判断均自动完成但写入需要员工确认
- **THEN** 可计语义自动覆盖，但该任务不能计为全程无人介入完成

### Requirement: Complete quality cost and latency report

报告 MUST 包含自动覆盖/准确率、误接管、漏接管、范围外误执行、任务成功、错误副作用、两类模型请求/问题/token/费用、重规划、复用和上下文量，以及机器耗时 p50/p95、人工等待和总耗时。结果 MUST 按决策族、语言、风险及主要候选规模分列。

#### Scenario: Aggregate masks weak family
- **WHEN** 总体自动正确率达到 95% 但某适用决策族未达标
- **THEN** 报告明确该族未通过，不能用总体分数给它发布合格策略

#### Scenario: Certainty calibration plot
- **WHEN** 绘制 Jev 分数分桶实际正确率
- **THEN** 图注明这是确定性与正确性的实测关系，不把集中度直接当可计算 ECE/Brier 的正确概率

### Requirement: Bounded real evaluation spending

所有真实评测 MUST 读取并遵守累计请求/Token/费用账本。未知用量不得释放预留；Jev、通用模型、用户模拟器和裁判须分角色记账，脚本、缓存与实网结果明确区分。若用户已明确授权 Qwen 批次的请求数、Token 和墙钟上限，primary、user simulator、judge 三类 Qwen callback MAY 在该冻结上限内派发而不设金额上限；费率或单次账单不可核验时费用 MUST 保持 null/unknown、`cost_complete=false`，且 MUST NOT 将此类样本用于成本优劣结论。Jev MUST 始终受已知费用上界与累计 `$1` 账本保护；Jev 费用 unknown 时 MUST 阻止全部新增 Jev 请求。

#### Scenario: User-authorized Qwen usage has unknown billed cost
- **WHEN** Qwen 费率未核实但用户已批准带请求、Token 和时间停止线的固定批次
- **THEN** 仅在冻结的批次与路线用量上限内允许 primary、用户模拟器和裁判派发；费用预留和结算金额保持 null，已知 Token 可参与业务评分，报告标记费用不完整且不作成本比较；Jev 仍不得绕过已知 `$1` 费用账本

#### Scenario: Budget insufficient for remaining test
- **WHEN** 剩余额度无法覆盖下一批的有界预留
- **THEN** 停止新付费请求，保存已完成样本与未完成清单，不伪称完成全量测试

#### Scenario: Model failure with missing usage
- **WHEN** 网络异常后不能确认本次费用
- **THEN** 保留未知用量和既有核实约束，不能把异常自动计为免费并继续消耗预算

#### Scenario: Readiness request becomes unknown
- **WHEN** 千问就绪请求出现超时或无法确认派发/用量
- **THEN** 对 10.7 已完成批次保留未知记录并停止该历史授权；后续独立批次只可按本需求定义的重试次数、备用模型与硬预算执行，不能重开历史授权或将 unknown 视为免费

#### Scenario: Separately bounded routed diagnosis
- **WHEN** 一个新的模型就绪批次经独立批准启动
- **THEN** 优先测试 `qwen3.8-flash`，同一纯模型步骤在 transient failure 后最多重试一次，之后可调用一次 `deepseek/deepseek-v4.1-flash`；该批次不超过 8 次请求、300,000 input+output tokens、DeepSeek $1。10.7 的账本与 unknown 不得重置或重放

#### Scenario: Resume a known local preflight failure
- **WHEN** readiness 阶段因本地检查失败，持久账本证明所有已建 attempt 均未派发且 token/费用为零
- **THEN** 系统仅通过显式恢复操作在原授权上继续，先归档失败身份/阶段/结果并保持请求、token、费用硬上限；任何已派发、unknown 或已通过阶段均拒绝重绑或重放

#### Scenario: Explicit separately bounded follow-up authorization
- **WHEN** 用户于 2026-10-19 明确批准先离线定位修复，再最多 3 次全新只读探测
- **THEN** 系统在修复回归通过后以 `qwen-readiness-20261019-plan-execution-v1` 独立持久计数，发送前占用名额、最多一次格式修复，保留此前 unknown，并在本轮任何超时或未知用量后停止；不得通过重新运行命令重置授权或继续未知请求

#### Scenario: Valid response without usage
- **WHEN** 正常返回的 ModelTurn 缺少用量，无论候选计划能否编译
- **THEN** 系统记录已收到响应但用量未知，不启动同端点格式修复或业务执行；仅当仍在独立有界恢复政策内且保留的最坏情况预留覆盖全部费用与 Token 上限时，才可将备用模型作为新的单独请求，否则停止

#### Scenario: Bounded fallback after a transient provider failure
- **WHEN** 选定 Qwen 请求因暂时性网络、限流或服务端故障失败
- **THEN** 至多对 Qwen 再请求一次；随后可调用一次固定 DeepSeek fallback。每次派发分别持久预留和结算；认证/权限/请求参数错误不重试同一配置

#### Scenario: Unknown request followed by bounded fallback
- **WHEN** Qwen 响应在断连后 unknown 且固定费用预留已被持久占用
- **THEN** 未知预留继续占用总请求/Token/费用预算；仅在账本仍能为单次 fallback 做有界原子预留时允许纯模型调用，否则停止。未知 MCP 写入永不通过该策略重放

#### Scenario: Frozen model profile for official comparisons
- **WHEN** 同一官方 benchmark 对照组运行三条 Agent 路线
- **THEN** 同组所有路线和通用裁判/模拟角色使用冻结模型、参数、端点及 provider-routing profile，关闭跨模型降级；改变主模型后登记独立实验组，不能合并成绩

### Requirement: Measurable release conclusions

安全与恢复验收 MUST 保证禁止副作用为零。普通成功非复用任务至少 90% 落在 1–3 次主模型请求内，正常适用复用任务 MUST 为零。效率声明 MUST 基于质量达标后的配对结果：任务成功率不低于同主模型逐步基线、主模型请求中位数及机器 p50 下降、p95 和总模型成本不增加；不达标或证据不足 MUST 明示。

#### Scenario: Fast but incorrect strategy
- **WHEN** 新路线延迟下降但质量或副作用要求不通过
- **THEN** 报告不能将其作为合格高效方案，相关验收保持未通过

#### Scenario: Uncertain improvement
- **WHEN** 性能点估计改善但区间不能支持稳定改善
- **THEN** 报告标记未证明而非承诺提速倍数，并保留全部结果

#### Scenario: Local deployment only
- **WHEN** Windows 本机与 Docker/WSL 测试通过但没有独立 Linux 主机
- **THEN** 独立 Linux 部署验收继续标待办，不由本轮评测自动完成

### Requirement: Public benchmark preflight fails closed

公开 benchmark runner MUST 默认只做离线 dry-run，并在允许执行前校验冻结任务 manifest、
来源 manifest、任务 inventory、角色化 model profiles、模型 readiness 和所需的固定镜像。
dry-run MUST NOT 创建执行 ledger 或触发模型/MCP 请求；真实执行在评测适配器、provider
callback、官方 scorer、清理逻辑和资源门禁接通前 MUST 被拒绝。dry-run 证据只能以新文件
写入 benchmark evidence 目录，不能覆盖既有证据。

#### Scenario: Read-only preflight
- **WHEN** 用户以默认参数或 `--mode smoke/full` 启动公开评测 runner
- **THEN** runner 校验冻结输入并报告准确任务分母、profile/manifest 指纹及零 live 调用，
  不创建 ledger 或修改任务执行状态

#### Scenario: Incomplete live adapter
- **WHEN** 上游工具适配、真实模型计量或官方评分闭环尚未连接
- **THEN** `--execute` 必须 fail closed，且不得创建新 ledger 或向任何模型/业务工具派发

#### Scenario: Bounded live diagnostic precedes pilot freeze and dispatch under a shared budget
- **WHEN** 通过 `--phase diagnostic` 对已暴露的四个固定开发案例做实时模型诊断
- **THEN** 只允许同一干净且已通过同 SHA CI 的候选，最多 12 次通用模型请求、600,000 Token 和 2,700 秒；Qwen 最多 6 次/600,000 Token/¥13.50，Qwen Agent、模拟器和裁判分别受冻结的角色限额约束；批次工具调用上限为 1,600，每路线资源仍独立限制。Qwen 费用预留、已结算费用和 unknown 预留须在同一持久账本事务中准入/结算，越线后停止所有后续派发。诊断开始时间和绝对 deadline MUST 在创建环境/预检前持久化；预检耗尽 deadline 时 MUST 写入批次停止原因并跳过所有未派发路线。诊断结果和 runtime pilot manifest MUST 写入 Git 忽略的 `.decagent/evaluation/` 状态目录，避免冻结证据让候选 SHA 变脏；报告必须包含 4 个任务、8 个路线槽、实际时间和模型/数据/源代码指纹；旧 `first-wave` live phase 必须拒绝。Pilot manifest 冻结及执行均要求通过该候选和相同 profile、inventory、source manifest 与执行代码摘要校验的完成诊断证据，并将该报告 SHA-256 和剩余额度绑定进冻结 manifest。诊断与 pilot 共用一周累计的 500 次 Qwen 请求、500 万 Token、1,600 次工具调用、4 小时和 ¥13.50 上限；pilot 的新账本必须先扣除诊断已用值及 unknown 预留，机器时间按已耗秒数向上取整，任何 phase 或 state 路径都不得重置累计额度。实现 MUST 使用 checkout 之外的规范用户级授权记录，将当前预算 ID 固定到唯一项目路径、候选 SHA、diagnostic cohort、pilot cohort 和对应报告摘要；新的候选/cohort 不得取得新预算，已登记账本丢失时 MUST 拒绝重建。诊断结束后，pilot 只能使用与授权中 diagnostic report SHA 相同的证据。

该 bounded diagnostic MUST 对每个固定案例先调度 `plan_mcp`，再调度 `react_skill_mcp`，使有限的 Agent 请求额度优先验证“计划生成、MCP 执行、官方评分”就绪门槛。此顺序只适用于 diagnostic，不改变正式对照路线定义或结果分母。

#### Scenario: Plan readiness receives the first diagnostic budget
- **WHEN** 每个固定开发案例进入 bounded diagnostic 调度
- **THEN** `plan_mcp` 是该案例首个派发路线，`react_skill_mcp` 保持在其后；路线遇到预算停止时不得继续派发该路线的后续模型、工具或评分调用

#### Scenario: Zero-dispatch diagnostic candidate successor
- **WHEN** 已授权 diagnostic 在任何模型、工具、机器时间或 τ 步骤派发前因可复现的代码/计划契约缺陷而失败，随后代码修复形成新的 CI 候选
- **THEN** 系统仅在旧账本证明 8 个路线均保持 `pending`、模型/工具/机器时间/τ 尝试均为零、无评分/清理结果且旧批次未停止时，允许将活动 diagnostic 绑定到新候选和新 cohort；必须保留旧账本及 supersession 审计记录，并沿用旧 `started_at` 与绝对 deadline，不能获得新的一份 2,700 秒或其他资源上限。新候选仍必须通过对应同 SHA CI。任何已预留或已派发尝试、非 pending 路线、unknown、停止批次或无法验证的账本均禁止候选接替；它们必须保留原始资源占用并按正常失败/unknown 规则终止。

#### Scenario: Immutable first-wave smoke scope
- **WHEN** runner 加载公开冒烟 manifest
- **THEN** 它必须校验固定的 CSM、ITSM、τ retail、τ telecom 四个首波 case ID 与 ReAct/Plan 路线顺序，报告 4 个任务和 8 个路线槽位；即使 manifest digest 被重算，改动首波身份也必须被拒绝

#### Scenario: First-wave phase is selected in frozen order
- **WHEN** 离线预检选择 `first-wave` phase
- **THEN** 只选择冻结的四个 case，顺序为 CSM、ITSM、τ retail、τ telecom，共 8 个路线槽位，同时保留 smoke cohort 的总分母 24/48

#### Scenario: Remaining smoke excludes the first wave
- **WHEN** 离线预检选择 `remaining-smoke` phase
- **THEN** 只选择剩余 20 个固定 smoke case、40 个路线槽位，并报告完整 smoke 分母 24/48

#### Scenario: Full phase requires the full cohort
- **WHEN** 离线预检选择 `full` phase
- **THEN** 只接受 full manifest，选择 286 个任务、572 个路线槽位；phase 选择不创建账本、不派发模型或工具

#### Scenario: First-wave live scheduler resumes only pending routes
- **WHEN** 所有 live 门禁已通过，首波 phase 新建或恢复执行
- **THEN** 调度器按冻结的 CSM、ITSM、τ retail、τ telecom 顺序和 ReAct、Plan 路线顺序遍历 8 个槽位；只派发状态为 pending 的路线，跳过 completed/failed/unknown/scoring_error 等终态，任何非终态的旧路线都必须先通过规范 ledger 恢复流程处理；当前路线 Qwen 用量 unknown 后不得重放该路线，但可继续其他 pending 路线；持久批次停止原因存在时不得继续派发

#### Scenario: First-wave report separates selected results from the cohort denominator
- **WHEN** 固定首波运行结束或因已记录的停止条件停止
- **THEN** 报告 MUST 同时给出所选 4 个任务/8 条路线及完整 smoke cohort 的 24/48 分母，并逐路线保留 failed、unknown、score 与 cleanup 状态；主模型、用户模拟器和裁判的实际用量、unknown 预留及费用完整性 MUST 分开报告，unknown 费用不得计作零，未执行路线不得算作失败或成功，也不得据子集结果声称全量分数或 Jev 收益

### Requirement: Public evaluation budgets are durable and route-scoped

每个公开 route MUST 在 dispatch 前预留模型请求、token、费用、工具调用和机器执行时间，
并在获得测量结果后结算。Agent、用户模拟器和裁判的请求/token 配额 MUST 分开累计。
角色/model profile、attempt 序号、provider/server 标识、endpoint 与价格版本按可获得范围
逐次持久化；凭证、提示词正文和私有评分材料不得进入公开评测 manifest 或 ledger。

#### Scenario: Tool and machine-time limit
- **WHEN** 新工具调用或执行时间预留将超过冻结 route 限额
- **THEN** ledger 拒绝 dispatch；额度不可通过重启或重新打开 ledger 清零

#### Scenario: Interrupted or unknown dispatch
- **WHEN** provider/tool attempt 在 dispatch 后中断，或其用量/结果未知
- **THEN** 未决额度保留为 unknown，route 不得自动 replay；仅可基于可验证原回执的显式恢复流程继续

#### Scenario: Diagnostic CNY fee hard stop
- **WHEN** 诊断批次准备派发 Qwen 请求、收到计费用量，或请求进入 unknown
- **THEN** dispatch 前必须把该请求的 profile 费用上界原子预留到批次 ¥13.50 上限；结算后按冻结费率的抵扣前 API 费用或可核验提供方费用记录实际金额，unknown 保留原预留。累计 actual+reserved 超限、价格或金额无法核验、或结算显示超限时，持久停止整个批次；报告分别列实际费用、预留费用及总额度，不得将 unknown 记为零。

#### Scenario: Natural Jev eligibility
- **WHEN** 基准任务没有自然适用且已审核的可信语义决策模板
- **THEN** Jev 对该任务标记 N/A，不得为了增加 Jev 样本而生成不自然判断节点

### Requirement: Official score evidence survives process recovery

公开 route MUST 将官方评分的有限报告字段与业务终态、cleanup 状态分开持久保存，并绑定官方评分来源及其 evidence digest。评分摘要仅允许包括基准来源、[0,1] 分数、成功判定及可测的禁止副作用计数；MUST NOT 保存或向 Agent 暴露 verifier 配置、gold、隐藏目标状态或评分解释材料。unknown route MUST NOT 被补记为已评分或成功。旧账本只能通过不重置 cohort 身份的加列迁移升级；历史缺失的评分明细 MUST 保持 unknown/absent。

#### Scenario: Scored route resumes with its official summary
- **WHEN** 官方评分与 cleanup 已记录，进程随后重启
- **THEN** route ledger 返回原评分来源、分数、成功标记和副作用计数，且摘要 digest 与持久记录一致

#### Scenario: Verifier details remain private
- **WHEN** 官方评分完成并保存报告摘要
- **THEN** Agent 输入仍不包含评分结果、verifier 内容或隐藏标签；报告摘要不能反向改写工具调用或最终回复

#### Scenario: Unknown route cannot acquire a score during cleanup recovery
- **WHEN** 路线因已派发的模型或工具结果变为 unknown，之后只补记 cleanup
- **THEN** route 仍为 unknown，评分证据保持 absent，业务终态不得改写

### Requirement: Public model attempts are independently ledgered

每个公开模型请求 MUST 经过单次受控 callback。callback MUST 在发送前验证冻结 profile 与完整输入 token 上界，并预留请求、token 和费用资源。只有能证明请求未发送的本地错误 MAY 释放预留。派发后的异常或无完整用量 MUST 将 attempt 和路线标记为 unknown；该路线不得重放、调用后续工具/模型或评分器，unknown 预留保持占用。

#### Scenario: Model dispatch is reserved and settled once
- **WHEN** 受控模型 callback 收到一次已冻结 role/profile 的公开请求
- **THEN** 它先预留输入上界、最大输出 token 与已知费用上界，再记录 dispatch，最后按一次 provider 结果结算，不隐藏重试

#### Scenario: Provider request is proven not sent
- **WHEN** 本地校验或 dispatch admission 在调用 provider 前明确失败
- **THEN** 仅该 attempt 可记录 `not_sent` 并释放对应预留，且 provider callback 调用次数为零

#### Scenario: Route model factory binds the frozen trial and role
- **WHEN** `ControlledRoutes` 为某条公开路线创建 planner、ReAct、semantic 或 τ callback
- **THEN** callback 必须绑定 manifest trial ID、对应冻结角色 profile 和当前 route ledger；每次实际模型请求只由该 callback 执行一次派发并记录 attempt

#### Scenario: Enterprise response-check uses the ledgered official judge
- **WHEN** pinned EnterpriseOps 原始 verifier 执行 `response_check`
- **THEN** evaluator 必须使用当前 route 的冻结 `judge` profile 和 ledgered callback；官方裁判请求、Token、费用来源与耗时按独立角色结算，费用 unknown 保持 null，usage unknown 使评分失败且不得重放；verifier 的隐藏配置与输出解释不得进入 Agent 输入或公开报告

#### Scenario: Preconfigured Enterprise judge cannot bypass route accounting
- **WHEN** Enterprise scorer 已预先注入裁判客户端，但当前受控路线没有将它绑定到该路线的账本 callback
- **THEN** 路线仍必须要求当前 route 的 ledgered callback，并在拒绝非 ledgered 客户端后停止评分；不得把预注入客户端视为已满足派发、预算和身份审计要求

#### Scenario: Model usage becomes unknown after dispatch
- **WHEN** 已 dispatch 请求以异常、无效响应或缺少完整 token usage 结束
- **THEN** attempt 和 route 保留为 unknown，不重放、不调用后续工具或官方评分；账本保留原预留

### Requirement: Public model deadlines and cost evidence are bounded

callback MUST 将 profile timeout、route 剩余机器时间与批次剩余墙钟的最小值传给模型传输层。没有可核验价格或可靠费用上界的 profile MUST 在派发前拒绝。费用 MUST 标明 provider billed 或冻结费率计算；费率不可核验且 provider 未提供金额时保持 unknown。账户 voucher/credit 不得替代抵扣前 API 费用。

#### Scenario: Effective model timeout is bounded by trial deadlines
- **WHEN** callback 即将派发且 profile、route 或 batch 剩余时间中任一更短
- **THEN** 传给模型 transport 的 timeout 等于三者最小值；过期 route 不会派发

#### Scenario: Agent machine-time starts after environment readiness
- **WHEN** 监督执行器已创建隔离业务环境并完成 τ 初始用户模拟，准备进入 Agent route
- **THEN** 它在调用 Agent 前持久记录唯一的 `agent_started_at_utc`，并从该时间重设该 route 的 machine-time deadline；环境准备时间仍计入批次墙钟，但不消耗 Agent 的每路线机器时间上限

#### Scenario: Agent machine-time start cannot be moved after dispatch
- **WHEN** route 的 Agent machine-time 已开始，或其 machine-time attempt 未处于 dispatched 状态
- **THEN** ledger 不得重设开始时间或延长 deadline；恢复时沿用原持久化时间，不得通过重复调用 start hook 获得新额度

#### Scenario: Unknown-price profile has no safe reservation bound
- **WHEN** Jev profile 或未获用户批次授权的模型 profile 没有可核验费率且未提供可靠费用上界
- **THEN** callback 在创建 provider dispatch 前拒绝请求，不把 token-only 用量或账户 voucher 当作零费用

用户已明确批准的 Qwen primary、user-simulator、judge 角色属于受限例外：只能在冻结的批次请求、Token 和墙钟硬上限内 dispatch，费用仍为 null/unknown，且不得参加成本结论；此授权不给 Jev 使用。

### Requirement: Public business tool calls honor persisted deadlines

公开执行器 MUST 将 route 剩余机器时间与 batch 剩余墙钟的最小值传给业务工具传输层，并限制单次调用不超过适配器上限。EnterpriseOps HTTP 请求、τ MCP 调用及 τ bridge 请求 MUST 使用该有效 timeout；τ bridge 超时 MUST 回收其拥有的 τ worker 子进程树。派发后发生超时或其他无法证明上游未执行的异常时，工具 attempt 与路线 MUST 记录为 unknown，不得重放该调用、继续调用后续工具或官方评分；cleanup 状态仍独立保存。执行器 MUST 拒绝没有 timeout-aware 契约的外部工具 gateway。

#### Scenario: EnterpriseOps HTTP request receives the effective deadline
- **WHEN** route 或 batch 剩余时间短于 HTTP client 默认 timeout
- **THEN** 该剩余时间作为请求级 HTTP timeout 传入；请求超时后路线保留为 unknown，后续工具与评分不派发

#### Scenario: Environment preparation does not consume Agent machine time
- **WHEN** 已 dispatch route，但受控 Agent-start hook 尚未执行
- **THEN** route deadline provider 返回完整预留的 Agent machine-time，仍受 batch 剩余墙钟约束；Agent-start hook 后才递减 route 额度

#### Scenario: τ MCP and worker process honor the effective deadline
- **WHEN** τ 工具调用或同步 parent callback 的剩余 route/batch 时间更短
- **THEN** MCP/bridge 请求使用该剩余时间；若 bridge 仍超时，τ worker 子进程树被回收，已派发 attempt 不自动重放

#### Scenario: Gateway without timeout support fails closed
- **WHEN** 公开评测 runner 收到不接受请求级 timeout 的业务工具 gateway
- **THEN** runner 在任何上游工具 dispatch 前拒绝该依赖

#### Scenario: Provider billed amount differs from voucher cash payment
- **WHEN** provider 返回 gross API charge，且账户另以 voucher/credit 抵扣
- **THEN** attempt 保留 gross cost 与 `provider_billed` 来源；不能以现金实付额替代 API cost，若逐笔 gross 未提供则 cost 仍为 unknown

#### Scenario: Incomplete public runner remains disabled
- **WHEN** 模型 callback、完整 route 取消、τ 子进程回收、官方评分或 cleanup 仍未接通
- **THEN** 以上单次 callback 边界不得被描述为完整评测执行器，`--execute` MUST fail closed

### Requirement: Public Jev attempts share the existing cumulative ledger

公开评测中的每个真实 Jev 请求 MUST 在派发前向既有累计 Jev 账本预留费用，并在公开 route ledger 中关联同一 reservation ID。所有历史探针、校准、smoke、full 和 route MUST 使用项目既有 `.decagent/business-eval/global-jev-ledger.json`。正式执行 MUST NOT 自动创建、重置或允许通过换账本路径、manifest、mode、route 或进程恢复获得新的累计额度；账本缺失、损坏或存在 unknown/pending 记录时 MUST 停止 Jev 派发。Jev 用量 unknown MUST 阻止所有后续 Jev 请求；Qwen 用量 unknown 只终结当前 route，并允许调度其他尚未开始且无需复用该 unknown route 状态的工作。

#### Scenario: Existing cumulative Jev balance is applied
- **WHEN** 公开 route 请求 Jev 判断
- **THEN** 系统从既有累计账本做原子预留，单 route 的 `$1` 检查不能替代全局累计上限；结算后的费用、token、模型和归属均关联到该试次

#### Scenario: New state path cannot reset cumulative Jev budget
- **WHEN** 执行请求指向另一份空账本、缺少既有账本或账本身份与项目固定账本不符
- **THEN** 执行在派发前失败关闭，不能初始化新余额、改写历史支出或释放 unknown 预留

#### Scenario: Jev usage becomes unknown across routes
- **WHEN** 任一试次 Jev 请求的实际用量或费用无法确认
- **THEN** 当前 route 停止，所有后续 Jev route 暂停；已有非 Jev 的独立 pending route 可在批次总额度内继续

#### Scenario: Qwen usage is unknown for one route
- **WHEN** 某路线的 Qwen 请求出现 unknown 用量
- **THEN** 该路线进入 unknown，停止该路线后续 MCP、模型和裁判调用；调度器可继续其他未开始路线，且不重跑该 unknown 路线

### Requirement: Public execution ownership is leased and fenced

真实公开评测 ledger MUST 仅允许一个活动执行者持有带过期时间的租约。所有 reserve、dispatch、settle、route transition 和恢复写入 MUST 校验 owner ID 与单调递增的 fencing generation。恢复 MUST 在现持有者租约有效时拒绝接管；租约过期后才可原子递增 generation、将旧执行者的未决工作保留为 unknown，并继续处理其他 pending routes。失去 fencing token 的旧进程 MUST NOT 再派发请求、结算结果或覆盖状态。执行者 MUST 通过独立 heartbeat 续租，不能仅凭 SQLite 事务串行化来声称有进程所有权。

#### Scenario: Resume while another worker is alive
- **WHEN** 第二个进程尝试恢复一个租约未过期的公开 ledger
- **THEN** 恢复被拒绝，当前进程与 route 状态不变，不能把活跃请求误判为 unknown

#### Scenario: Stale worker is fenced after takeover
- **WHEN** 原租约过期且新进程已取得更高 generation
- **THEN** 原进程的任何后续写入或 dispatch 均被拒绝；其已派发 attempt 不重放并按 unknown 保留

#### Scenario: First-wave execution requires canonical process lock and ledgers
- **WHEN** 首波 live runner 打开或恢复受控执行账本
- **THEN** 当前监督进程必须持有项目规范 OS 生命周期锁，并使用规范首波 route ledger 与现有累计 Jev ledger；替代 state path、缺失 Jev ledger、未持锁或非 smoke cohort 均在任何 dispatch 前失败关闭，且不得初始化或复制额度

#### Scenario: First-wave ledger is bound to one verified candidate
- **WHEN** live runner 创建或恢复首波 cohort ledger
- **THEN** 它必须在任何 route claim 前持久记录精确通过同一 SHA GitHub CI 的 candidate SHA；恢复必须使用完全相同的 SHA，route dispatcher 和 report builder 都必须核对该绑定。已有 route/model/tool attempt 但缺少 candidate SHA 的历史 ledger 必须拒绝绑定和继续执行，不得从新进程、外部报告或当前 HEAD 推断或补写其历史候选身份

#### Scenario: Candidate-bound first-wave ledgers preserve prior cohorts
- **WHEN** 显式执行一个冻结 cohort 与已通过 CI 的 candidate SHA
- **THEN** ledger 路径必须由该 cohort digest 与 candidate SHA 在规范目录中确定；同一二元组只能恢复同一 ledger，任意路径覆盖必须失败关闭。不同候选的执行使用独立、可审计的批次 ledger，不能覆盖或改写旧候选的 unknown；历史 v7 ledger 保持原样，Jev attempt 仍进入同一累计全局账本

#### Scenario: Route dispatch requires ledger-bound model and tool adapters
- **WHEN** 首波 ReAct 或 Plan route 被监督执行器领取
- **THEN** 执行入口必须验证规范 supervisor 锁、规范 smoke ledger、route 仍为 pending，且 model adapter 与 tool gateway 均绑定同一 ledger、trial ID 和 route；直接传入 planner/ReAct 等未计量 callback 必须在领取 route 前拒绝

#### Scenario: 公开试次必须与冻结清单身份一致
- **WHEN** live runner 构造对照试次或领取其 route
- **THEN** runner 在任何 route reservation 前核对 manifest trial ID、benchmark、版本、case ID、exposure group、resource profile、response policy 与各角色冻结 profile digest；账本 ID 不进入对照指纹

#### Scenario: Route claim and machine-time reservation are atomic
- **WHEN** 批次准入或 route machine-time 预算不允许开始一条 pending route
- **THEN** route claim 与机时 reservation 在同一事务失败回滚，route 保持 pending 且不创建 machine-time attempt；成功时二者共同提交

#### Scenario: Machine-time overrun preserves independent score and cleanup evidence
- **WHEN** route 已返回受控执行与 cleanup 结果，但实际 Agent machine-time 超出冻结上限
- **THEN** route 以失败终态记录 `resource_overrun`，不得标记 completed；可用的官方分数和 cleanup evidence 仍分别持久保存

### Requirement: Live provider transport is isolated under the supervisor fence

每个真实公开评测模型请求 MUST 先由持有规范 route ledger 的监督进程完成预算预留和 dispatch 标记，再交给一次性子进程执行。子进程 MUST 仅处理一个已授权请求，不得持有 route/预算 ledger、创建可复用模型 client，或接触当前角色以外的模型凭证。子进程 MUST 在 provider 请求前取得规范 worker guard，并确认启动它的监督进程仍存活。新监督进程 MUST 在打开或恢复 route ledger 前持有规范 supervisor lock，并确认没有前一监督进程遗留的 worker guard。达到请求 deadline 时，监督进程 MUST 终止它拥有的整个子进程树；派发后的超时继续按 unknown 处理，不得重放。

#### Scenario: Expired supervisor cannot overlap a live provider worker
- **WHEN** 前一监督进程的主锁已释放，而该进程启动的一次性 provider worker 仍持有规范 worker guard
- **THEN** 新监督进程在打开或恢复 ledger 前失败关闭；worker 退出并释放 guard 后才允许重新取得 supervisor lock

#### Scenario: Provider worker starts after its supervisor exits
- **WHEN** 一次性 worker 在确认调用方已退出后才开始执行目标函数
- **THEN** worker 不得发送 provider 请求，并在释放其 guard 后退出

#### Scenario: One-shot provider request reaches its deadline
- **WHEN** provider 请求超过 route/model/batch 计算出的有效 deadline
- **THEN** supervisor 终止 worker 及其子进程树，model attempt 和 route 保留 unknown，后续调用不被重放

#### Scenario: POSIX termination catches descendants racing worker shutdown
- **WHEN** POSIX worker deadline 终止其进程组，同时 worker 正在创建一个后代进程
- **THEN** supervisor 在有界等待 worker exit sentinel 后再次终止该进程组，并在 reap worker 前有界确认 Linux 进程组内没有活动成员（zombie 不视为仍可运行）。进程表读取不完整也视为无法确认；若硬时限内仍有活动成员或无法确认，必须返回 unresolved tree error，不能声称清理成功；该操作不重新派发模型请求，已派发 attempt/route 仍保留 unknown

### Requirement: Frozen profiles capture actual request behavior

公开模型 profile MUST 为每个角色记录模型请求中实际采用的采样参数、输出/工具选择契约、超时及 provider routing。每个参数 MUST 标为固定值或明确不支持；客户端 MUST 对支持的固定参数显式发送值，对不支持参数不得发送；不得依赖未记录的 SDK/provider 默认值。每次 attempt MUST 保存 requested profile 与可取得的 resolved provider/model/server identity。无法取得的 resolved 值显式记录为空；profile、routing 或 resolved model 在组内发生变化时 MUST 停止拼接成绩并启动独立实验组。

#### Scenario: Profile contains unsupported parameter explicitly
- **WHEN** 服务不支持固定 seed 或 provider routing 字段
- **THEN** profile 记录该字段为 unsupported 及原因，客户端不发送该参数，attempt 仍记录其余实际请求配置

#### Scenario: Provider resolves an unexpected model
- **WHEN** 服务端返回的实际模型与组内已记录模型不一致，或实际身份不可获得
- **THEN** attempt 保存实际可用身份或 null，runner 标记 cohort drift/身份不足并停止将该结果与原组拼接

#### Scenario: 不从请求 profile 推断实际上游
- **WHEN** callback 没有收到上游响应中的 provider 身份
- **THEN** 请求 provider 仍由冻结 profile 记录，实际 provider 身份保持 null，runner 不得将请求 provider 复制到 resolved identity 字段

#### Scenario: Frozen supported request parameters are sent explicitly
- **WHEN** 通用模型 profile 将 temperature 和 top_p 固定为数值，且 seed 标记为 unsupported
- **THEN** 通用模型客户端显式发送 temperature/top_p、完全省略 seed，并把 timeout 与 tool choice 绑定到相同 profile

#### Scenario: Timeout recovery creates an immutable model cohort
- **WHEN** 有证据表明模型请求在冻结 profile timeout 附近终止，且需要评估更长的请求时限
- **THEN** 新请求时限必须写入新的 role profile 指纹和 cohort manifest；历史 profile、manifest、route attempt、usage reservation 与终态保持不变，不得重放旧 unknown；只允许调整目标角色的 timeout，其他角色沿用各自冻结值

### Requirement: Billable model cost is distinct from account credits

模型成本比较 MUST 使用可核验的抵扣前 API 计费金额。账户代金券/credit 抵扣及实际现金支付 MUST 分别记录，代金券覆盖账单 MUST NOT 被解释为 API 零费率。每个成本值 MUST 标记为供应商/账户账单实际值、基于冻结费率的计算值或 unknown；费率来源及版本 MUST 进入 profile 指纹。已知 token 用量与 unknown 费用 MUST 可以同时存在。未获核验的零费率不得用于 Jev 与通用模型的成本结论；历史 profile 与原始账单/请求证据保持不可变，通过更正说明隔离其错误假设。

#### Scenario: Voucher covers a billed Qwen request
- **WHEN** Qwen API 请求产生费用并由账户代金券抵扣
- **THEN** 成本报告保留抵扣前 API 费用、抵扣金额与账户实付金额，模型成本对照使用抵扣前费用

#### Scenario: Provider evidence is available only at request granularity
- **WHEN** callback 收到带原币种的逐请求账单证据
- **THEN** 持久账本分别保存原始 gross API fee、实际提供的 voucher credit 与 cash paid、币种、来源和供应商记录 ID；路线账单快照按币种聚合并给出证据覆盖率，未提供的分项保持 null，不从账户余额推算，也不把账户或时间窗口总额分摊到路线

#### Scenario: Frozen Qwen standard rate prices CNY usage by cache status
- **WHEN** 新 cohort 使用适用于配置 endpoint 的冻结 Qwen 标准实时费率，输入/输出用量已知，且响应提供缓存命中 Token 数
- **THEN** versioned model profile 保存 `CNY`、费率来源与版本、普通输入/输出费率及缓存命中输入费率；账本按命中数计算抵扣前 API 成本并保留 CNY，不推测兑换率，也不把该费率计算值称作供应商账单或 voucher 实付

#### Scenario: Cached-token detail is omitted from model usage
- **WHEN** 冻结费率含较低缓存命中价，但该响应没有可验证的缓存命中数量
- **THEN** 账本仍可保存完整 Token 用量与成本完整性，按普通输入费率计算保守上界并标记为 `frozen_rate_upper_bound`；不得假定命中缓存或按优惠价结算

#### Scenario: New pricing cohort preserves historical profile digests
- **WHEN** 新的币种/缓存费率需要版本化模型 profile 与 manifest
- **THEN** 新 cohort 使用 profile/manifest 的新 schema 版本，旧 v3 profile 与 v7 manifest 原字节和指纹保持可读取，Jev 原 profile 指纹与累计美元账本保持不变

#### Scenario: Provider bills in a currency without a verified exchange rate
- **WHEN** 逐请求账单提供非 USD 原始金额，但没有已核验汇率
- **THEN** 持久账本保留原金额与币种，USD 成本仍为 unknown，成本报告不得自行换算

#### Scenario: Usage is known but no billable rate is verified
- **WHEN** 响应提供输入/输出 token，但没有实际费用且 profile 无可核验费率
- **THEN** token 用量照常结算，费用标记为 unknown，不记为零且不据此得出成本优势

#### Scenario: Historical zero-price profile is encountered
- **WHEN** 评测读取基于账户“免费额度”假设的历史 Qwen profile
- **THEN** profile 仅作为历史配置证据保留，其零费率不得作为实际账单或成本比较输入

### Requirement: First-wave batch-wide resource ceilings

固定首波四任务、八条 ReAct/Plan 路线 MUST 在持久账本执行冻结 manifest 额度。Qwen 批次最大值为 500 请求、5,000,000 token、14,400 秒及 1,600 次工具调用；Agent、用户模拟器、裁判的最大请求/token 分配分别为 300/3,000,000、160/1,600,000、40/400,000。新 live-capable cohort MAY 冻结更低的正整数额度，但 MUST NOT 超限，角色请求与 token 配额 MUST 分别加总至批次总额。自定义额度 MUST 使用支持派发的 v9 或更新版本。额度须进入 manifest digest 与 cohort identity，由账本按冻结值执行；历史 v7-v10 默认 manifest 不变。预算跨路线和恢复累计；未知用量保留预留。

#### Scenario: A fresh cohort freezes lower batch limits
- **WHEN** 新四域 cohort 的 manifest 冻结了不高于已批准最大值的批次与角色上限
- **THEN** manifest 校验角色分配精确加总且各值为正整数后，将这些较低额度写入 digest、cohort identity 与持久账本；该 cohort 只可按冻结的较低值派发，不能继承旧 cohort 的额度或 unknown 预留

#### Scenario: Retry and per-route limits remain independently accounted
- **WHEN** 路线执行重试、格式修复、受控总结或下一次 Agent/tool/τ action
- **THEN** 模型请求按实际角色累计至批次额度；每路线 200 次 Agent 请求、200 次工具、1,000,000 token、1,800 秒与冻结 τ 步数仍分别生效；达到任一上限即拒绝新派发，未知用量保留预留

#### Scenario: Later benchmark phases receive separately frozen budgets
- **WHEN** 运行剩余 24 个 smoke 任务、206+80 正式集或额外语义/消融试次
- **THEN** 使用经估算并单独冻结的批次额度，不复制首波额度作为授权

#### Scenario: First-wave role or aggregate ceiling is reached
- **WHEN** 新请求会超过任一角色或首波批次请求、token、工具、墙钟上限
- **THEN** 派发前拒绝该请求并持久记录停止原因和未开始路线，不重置或重放 unknown

#### Scenario: Batch resumes after process restart
- **WHEN** 首波在进程重启后恢复
- **THEN** 恢复此前累计的各角色请求/token、工具调用和墙钟，不重新取得完整额度

#### Scenario: Execution counters retain their own meanings
- **WHEN** 路线执行一次计划节点、一次模型调用、一次 MCP 工具调用或一个 τ 官方交互步
- **THEN** 计划控制步、角色模型请求、工具调用和 τ 官方步数分别计数，不能由共同循环上限互相替代；τ 的计数语义必须来自冻结的 τ 运行时契约

### Requirement: τ step attempts follow the pinned role-handoff contract

公开 τ 路线 MUST 按冻结的 τ-Bench 半双工角色交接单位独立预留与计数 `tau_steps`，不得从 Agent 模型请求数或 MCP 工具调用数推导。固定版本 `96a3ef9` 的一次 Agent、User 或 Environment role handoff 是一个 step；一个 User 或 Agent 发出的多工具批次仍是一个 Environment step。DecAgent ReAct 的 Agent action、Plan 的对外工具/澄清/语义决策 action、每次 simulator reply、每个工具批次须分别映射到对应 handoff 单位。每个 handoff attempt 在调用相应模型模拟器或业务工具副作用前原子写入路线账本；达到冻结上限后，路线不得继续派发后续 Agent action、模拟器回合或业务工具，允许按独立 judge 额度运行官方评分来评估已结束的有限轨迹。step 账本 MUST 跨账本恢复保留，unknown 路线不得重放。

#### Scenario: User multi-tool batch consumes one environment step
- **WHEN** τ 用户模拟器一次返回多个用户工具调用
- **THEN** 工具 ledger 按实际调用数分别计量，τ step ledger 只为该批环境交接记一个 Environment step；完成批处理后每次新的模拟器回复另记一个 User step

#### Scenario: Plan semantic decision is an agent action
- **WHEN** PlanRuntime 执行已审查的 `decide` 节点并请求 Jev 或通用语义模型
- **THEN** 在语义 callback 之前先预留一个 Agent τ step，且它不会替代实际模型请求或工具调用计数

#### Scenario: τ step cap prevents another simulated action
- **WHEN** 路线已用尽冻结 `tau_steps` 上限并准备下一次 Agent/User/Environment handoff
- **THEN** 账本拒绝新 step attempt，并在对应模型模拟器或业务工具 dispatch 前终止 τ 模拟；允许的官方 judge 仍使用独立请求/token 预算，step 计数在账本重新打开后不清零

### Requirement: Public failure diagnostics are allowlisted and immutable

公开评测 attempt 诊断 MUST 使用白名单阶段码、异常类名、100–599 HTTP 状态码及有限的诊断原因码，且 MUST NOT 保存异常消息、请求/响应正文、工具参数或凭证。route 仅可镜像允许列表中的原因码、阶段码和异常类名；快照只导出安全字段。升级 MUST 追加 nullable 列，历史缺失保持 null。unknown 清理不得改写失败证据或重放。可信证明模型请求未发送的 attempt MUST 保持 `not_sent`，HTTP 状态为 null，且不改变 route 状态。

#### Scenario: Model callback fails after dispatch
- **WHEN** 已派发模型请求后遇到超时或传输异常，且服务端 usage 无法核实
- **THEN** attempt 与 route 进入 unknown，记录 `model_callback`、安全异常类名及可获得的 HTTP 状态码；异常消息和模型输入/输出正文不进入账本或报告

#### Scenario: τ callback contract failure carries a safe reason code
- **WHEN** τ simulator/judge bridge 收到不符合冻结 callback 契约的事件、模型身份、文本或工具调用响应
- **THEN** route 只记录对应白名单原因码、允许的 failure stage 和异常类名；原始异常消息、模型正文、工具参数及凭证不得进入 ledger/report，历史终态不得因新增诊断被改写

#### Scenario: Verified model preflight failure records safe diagnostics
- **WHEN** one-shot 模型客户端构造或输入校验在发出任何 HTTP 请求前失败，且本机可确认请求未发送
- **THEN** attempt 保持 `not_sent` 并释放预留，可记录 `model_worker_request_pre_send`、受限失败原因及安全异常类名；`http_status` 保持 null，route 仍可继续，异常消息、请求内容和凭证不进入账本

#### Scenario: Worker reaches the HTTP pre-send hook but receives no response
- **WHEN** 隔离模型 worker 的 HTTPX2 request hook 已触发，但 response hook 未触发且模型 usage 无法核实
- **THEN** attempt 与 route 保持 unknown，并记录 `model_worker_request_pre_send`；该阶段不得被解释为请求已到达供应商或供应商未计费，不得据此释放预留或重放路线

#### Scenario: Worker receives response headers but cannot measure usage
- **WHEN** 隔离模型 worker 已收到 HTTP response headers，之后的 SDK/响应解析失败且没有完整模型 usage
- **THEN** attempt 与 route 保持 unknown，并记录 `model_worker_provider_response` 和有效 HTTP 状态码；响应状态不代表 token 或账单已知，响应正文不得进入诊断，路线不得重放

#### Scenario: Supervisor deadline retains a received response boundary
- **WHEN** 隔离模型 worker 已收到 HTTP response headers，但父级监督期限先于 SDK 自身超时到达并终止 worker，且 usage 未知
- **THEN** attempt 与 route 保持 unknown；监督者仅从进程间有界状态记录 `model_worker_provider_response` 和有效 HTTP 状态码，不记录响应正文，不释放预留，也不重放路线

#### Scenario: Supervisor deadline before transport progress
- **WHEN** 父级监督期限到达时 one-shot worker 尚未更新任何传输阶段，且不能证明请求是否已发出
- **THEN** attempt 与 route 保持 unknown，记录通用 `model_worker_timeout` 且 HTTP 状态为 null；不得声称请求已发出或未发出，不释放预留，也不重放路线

#### Scenario: Tool gateway loses a response
- **WHEN** 已派发的工具调用返回无法确认上游结果的异常
- **THEN** 工具 attempt 与 route 进入 unknown，记录允许的工具阶段及安全异常类名；参数和返回正文不进入诊断快照，后续工具、模型及评分均停止

#### Scenario: Unknown-route cleanup amendment preserves the failure evidence
- **WHEN** unknown route 后续只补录经过核验的清理结果
- **THEN** route 仍为 unknown，原失败类别/阶段/异常类型不变，且该路线不能重放

#### Scenario: Existing ledger is upgraded without inventing diagnostics
- **WHEN** 使用旧版公开账本恢复评测
- **THEN** schema 以 nullable 列追加诊断字段，既有记录保持 null，原 cohort、账单与 unknown 预留保持不变

