# Spec Delta

## Purpose

以可复现的完整模拟业务工作流评测，验证条件计划、Jev 运行时语义判断、置信度门控和人工接管是否改善任务质量及执行效率，并明确 benchmark 适用范围、统计不确定性、人工成本与未通过的指标。

## ADDED Requirements

### Requirement: Reproducible full-workflow benchmark environments

EnterpriseOps-Gym CSM/ITSM MUST 作为主要端到端工作流 benchmark；τ-Bench MUST 作为对话、澄清和用户模拟的补充 benchmark。评测 MUST 固定来源、版本/提交、许可、任务 ID、工具/API 版本、容器镜像摘要和初始数据库快照。原始任务目标、工具和成功条件 MUST 保持不变；部分运行 MUST 报告任务清单和覆盖范围，不得声称完成全量 benchmark。孤立意图分类数据 MUST NOT 作为完整 Agent 工作流成功的主要证据。

#### Scenario: EnterpriseOps primary evaluation
- **WHEN** 执行主要端到端评测
- **THEN** 使用固定版本的 EnterpriseOps-Gym CSM/ITSM 原始任务、容器化 MCP 服务及其最终状态 verifier，并从规定的数据库初始状态运行

#### Scenario: Tau dialogue supplement
- **WHEN** 执行 τ-Bench 对话或澄清补充评测
- **THEN** 固定域、任务和版本；使用原始 Tool/ToolKit 工具语义或由所有策略共用的 MCP 适配器，单独记录用户模拟器的模型调用和费用

#### Scenario: Benchmark subset
- **WHEN** 资源限制只允许执行 benchmark 的固定子集
- **THEN** 报告选择规则、全部任务 ID 和未运行部分，不修改样本目标来迎合某条策略

### Requirement: Separate data splits and prevent answer leakage

系统 MUST 保留 benchmark 官方任务划分；无官方划分时按业务流程/任务族隔离开发、验证和测试。期望状态、reference actions、标签、答案及 verifier 私有字段 MUST NOT 出现在待测模型输入中。测试集 MUST 不参与模板、阈值或策略调优，评测记忆不得跨测试样本学习。

#### Scenario: Related tasks cross intended split
- **WHEN** 多个任务属于同一业务流程族、状态模板或近重复改写
- **THEN** 它们被放在同一 split 或明确排除，不能同时用于阈值选择和测试证明

#### Scenario: Verifier data is present
- **WHEN** benchmark 包含 expected actions、目标数据库状态或评分器私有字段
- **THEN** 模型只接收允许的原始输入与合法选项，评分器单独读取隐藏标签

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

#### Scenario: Plan already determines the branch
- **WHEN** 首次规划已能依据现有事实确定唯一分支
- **THEN** 直接执行已验证计划，不增加 Jev 或后续主模型判断

### Requirement: Paired fair baselines and ablations

评测 MUST 在相同冻结任务上比较 ReAct + Skill + MCP、一次规划 + 确定性执行器 + MCP、一次规划 + 确定性执行器 + Jev 语义运行时判断。后两者的 MCP 工具调用 MUST 由执行器按计划和类型化参数绑定直接构造；不要求每个工具调用都请求 LLM。对语义分支 MUST 使用相同状态、可信问题和候选，配对比较 Jev 与主模型二次判断的正确率、延迟和成本；结构化分支单独与代码规则比较，不要求 Jev 胜过代码。新路线 MUST 区分无门控/有门控、冷启动/复用及必要的记忆消融；基线和失败样本不得事后选择性替换。

#### Scenario: Same workflow across strategies
- **WHEN** 多种策略运行同一冻结任务
- **THEN** 使用相同任务目标、数据库快照、工具实现、身份权限和必要确认规则，不能给某策略额外答案或权限

#### Scenario: Reuse cost comparison
- **WHEN** 报告零主模型复用收益
- **THEN** 分列首次编排和模板验证成本、命中率及复用执行成本，不将所有成本隐藏到准备阶段

#### Scenario: Jev compared with a second primary-model decision
- **WHEN** 运行时语义分支分别由 Jev 和主模型作出判断
- **THEN** 使用同一输入与选项测量质量、延迟和真实用量；质量未达到目标或没有可测成本/延迟改善时，不宣称 Jev 优于主模型

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

所有真实评测 MUST 读取既有累计账本，在 Jev 累计 1 美元授权内预留并记账；费用未知 MUST 保持约束。千问免费额度 MUST 仍记录实际请求和 token。未执行、缓存、脚本与真实网络结果 MUST 明确区分。

#### Scenario: Budget insufficient for remaining test
- **WHEN** 剩余额度无法覆盖下一批的有界预留
- **THEN** 停止新付费请求，保存已完成样本与未完成清单，不伪称完成全量测试

#### Scenario: Model failure with missing usage
- **WHEN** 网络异常后不能确认本次费用
- **THEN** 保留未知用量和既有核实约束，不能把异常自动计为免费并继续消耗预算

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

