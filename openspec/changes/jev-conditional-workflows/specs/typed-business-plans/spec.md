# Spec Delta

## Purpose

让已注册业务能够用有限、可检查的条件计划表达 Jev 判断、工具顺序、未来参数和完成证据，使执行方无需在每个已知分支重新请求通用模型，并保持业务权限与可信判断定义的独立性。

## ADDED Requirements

### Requirement: Typed bounded business plans

系统 MUST 接受有版本、唯一节点、目标、数据依赖、条件边、异常出口和完成证据定义的业务计划；计划 MUST 只使用已注册节点能力和有限执行界限。计划 MUST NOT 包含任意可执行代码或来自用户文本的权限定义。

#### Scenario: Known branching workflow
- **WHEN** 规划器提交先创建工单、再按后续候选选择处理人的条件计划
- **THEN** 系统可验证判断与工具依赖，无需首次规划猜测新工单 ID 或处理人名单

#### Scenario: Invalid or unbounded structure
- **WHEN** 计划含未知工具、重复节点、无界循环、悬空引用或超过公布规模上限
- **THEN** 系统拒绝进入执行并返回可定位的校验错误，不产生业务副作用

### Requirement: Trusted typed decision definitions

每个 Jev 节点 MUST 引用已发布的 Choice、Noul 或 Score 模板及版本。模板 MUST 定义问题语义、输出、允许候选来源或评分档位、缺证据和无匹配行为。规划器 MUST NOT 在任务执行时更改这些定义；新增定义 MUST 经验证和评测后发布。

#### Scenario: Model attempts to redefine severity
- **WHEN** 计划在引用 Score 模板时追加与已发布档位不同的含义
- **THEN** 系统拒绝该计划，不将追加说明发送为受信 Jev 指令

#### Scenario: Dynamic authorized candidates
- **WHEN** Choice 的候选来自当前有权读取的员工目录
- **THEN** 系统保留候选的实际 ID、来源和版本，仅允许选择当前合法候选或模板定义的无匹配结果

#### Scenario: Empty candidate set
- **WHEN** 权限过滤后没有可用候选
- **THEN** 系统走缺信息或无匹配出口，不生成虚构候选，也不要求模型强制选择

### Requirement: Typed future parameter bindings

参数 MUST 来自显式常量、用户输入、前序真实结果、已接受判断、经验证记忆或已注册纯计算，并保留来源及类型。经验证记忆只能由已发布工作流通过 `memory_kind + subject` 稳定槽引用，不得把员工记忆 ID 写入模板；非已发布工作流中的记忆绑定 MUST 被拒绝。未来结果 MUST 表示为引用，执行前 MUST 解析并按工具 schema 校验，缺失值 MUST 有明确处理路径。

#### Scenario: Referenced created resource
- **WHEN** 前序创建操作已有核实回执，后续节点需要资源 ID
- **THEN** 系统从该回执绑定实际 ID，并记录其证据来源

#### Scenario: Placeholder or missing value
- **WHEN** 所需值仍不存在、类型不匹配或只是模型生成的占位文本
- **THEN** 系统不调用业务写工具，进入既定补充信息或计划失效路径

#### Scenario: Stable memory slot has no current employee value
- **WHEN** 已发布计划绑定了稳定记忆槽，但当前员工无唯一有效记录
- **THEN** 系统在节点分派前暂停，不接受任务输入中的记忆 ID 或模型生成值替代该槽

### Requirement: Branch-safe data dependencies

系统 MUST 验证条件分支和合流处的数据可用性。实际未激活分支 MUST NOT 阻塞合流或提供虚构输出；依赖部分分支结果的节点 MUST 声明合法的合流映射或缺值出口。

#### Scenario: Only one branch produces assignee
- **WHEN** 分派分支产生处理人，而无需分派的分支不产生该值
- **THEN** 后续节点只能在其激活条件与字段来源相容时使用该处理人，不能读取未执行分支的结果

#### Scenario: Complete path without inactive nodes
- **WHEN** 实际分支已经满足目标且其他分支未激活
- **THEN** 合流允许推进，不等待或执行未激活分支

### Requirement: Replaceable planner with observable requests

规划器 MUST 以受限上下文输入和类型化计划输出工作，并暴露实际模型请求、重试、取消与用量。普通工具结果、已知分支和低确定性 MUST NOT 单独触发通用模型再规划。系统 MUST 能在无需自由文本生成时确定性输出结果。

#### Scenario: Successful known branch
- **WHEN** Jev 选择了已包含在有效计划中的分支
- **THEN** 系统直接继续计划，不增加通用模型请求

#### Scenario: Adapter hides internal requests
- **WHEN** 候选规划器无法报告内部重试、摘要或工具循环产生的实际请求
- **THEN** 该适配器不能用于有调用预算约束的任务

### Requirement: Evidence-constrained completion

计划完成条件 MUST 绑定受信业务目标及真实读取结果或已核实写入回执。模型声称完成、Jev 判断或未核实工具响应 MUST NOT 独立证明成功；所有实际写入 MUST 与目标相容且有可核实结果。

#### Scenario: Plausible completion without receipt
- **WHEN** 模型表示已完成分派，但对应提交结果仍未知
- **THEN** 任务保持结果核实状态，不报告成功

#### Scenario: Auxiliary read
- **WHEN** 合法辅助读取帮助完成目标但并非最终用户目标
- **THEN** 完成判断可以忽略该读取作为必需成果，同时保留审计记录

