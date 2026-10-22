# Jev 适用性：政策与工具契约审阅

审阅日期：2026-10-22（Asia/Shanghai）

## 结论

在当前已冻结的公开评测执行器和可信模板注册表中，唯一已审核、自然适用的 Jev 语义节点是 EnterpriseOps CSM 的 `csm_knowledge_article`：在一次完整、未截断的 `retrieve_knowledge` 只读结果中，按用户问题选择最相关的已发布知识文章，或选择 `no_match`。该判断只选知识候选，不批准链接、通知或其他写操作。只有任务实际执行到该节点，才可配对比较 Jev 与通用模型。

ITSM、τ retail 和 τ telecom 当前没有对应的已审核 Jev 模板。它们的确定性状态与权限条件应由代码或计划条件处理；必须由员工明确表达的候选选择与写入确认应留给员工。当前公开对照中这些域标记为 Jev `N/A`，不能为了增加 Jev 样本而把规则或员工选择改造成模型判断。该结论针对当前模板与执行器契约，并不宣称所有未检查的具体任务都不存在自然语言歧义。

## 按域审阅

| 域 | 静态政策 / 契约证据 | 应采用的判断方式 | Jev 状态 |
| --- | --- | --- | --- |
| EnterpriseOps CSM | [semantic_templates.py](../../../decagent/evaluation/business/semantic_templates.py) 将候选限制为同一次 `retrieve_knowledge` 的完整结果，要求 `published`，保留 `no_match`；[templates.py](../../../decagent/business/plans/templates.py) 只在隔离评测目录满足精确契约时注册该模板；[compiler.py](../../../decagent/business/plans/compiler.py) 校验候选必须来自真实无上限只读调用。 | 按文本相关性选候选属于语义匹配；候选完整性、发布状态和写权限由代码校验。 | 自然适用；仅当运行时实际到达该节点时纳入同状态配对。 |
| EnterpriseOps ITSM | 当前 `EvaluationCatalog` 的语义注册路径只开放满足固定 CSM 知识检索契约的模板；当前 ITSM 没有对应注册模板。 | 已注册状态、动作及权限规则使用代码；planner 可理解用户请求，但不能凭此将未审核判断伪装成 Jev 节点。 | 当前执行器范围 `N/A`。 |
| τ retail | [retail policy](../vendor_cache/tau2-bench-source/data/tau2/domains/retail/policy.md) 将身份认证、订单状态、可执行动作和用户确认写成明确流程；[retail tools](../vendor_cache/tau2-bench-source/src/tau2/domains/retail/tools.py) 提供读写工具及其显式状态校验。 | 订单状态和规则前置条件用确定性规则；支付方式、商品选项、退换项目及是否确认由用户明确提供。 | 当前没有审核通过的 Jev 语义节点，`N/A`。 |
| τ telecom | [main policy](../vendor_cache/tau2-bench-source/data/tau2/domains/telecom/main_policy.md) 与 [technical-support workflow](../vendor_cache/tau2-bench-source/data/tau2/domains/telecom/tech_support_workflow.md) 按工具诊断结果规定后续步骤，并要求用户选择套餐、确认费用；[telecom tools](../vendor_cache/tau2-bench-source/src/tau2/domains/telecom/tools.py) 暴露结构化账户状态和读写操作。 | 明确的状态门槛、诊断结果与政策动作走规则；套餐选择、费用确认和写入授权由用户完成。 | 当前没有审核通过的 Jev 语义节点，`N/A`。 |

## 审阅边界与验收状态

- 本次仅检查静态政策、工具实现和 DecAgent 的模板注册/编译器代码；没有读取任何 EnterpriseOps 或 τ-Bench task JSON，也没有访问官方 test 任务内容。
- 这不是对具体任务分布的扫描。若新任务提供一个非规则、非员工保留的自然语义选择，必须先单独审核其完整工具输入、候选来源、无匹配行为、权限边界和模板版本，再加入对照。
- 9.6.D 继续保持未完成：CSM 模板虽已冻结，但还没有相同状态的 Jev / 通用模型真实请求配对。没有进行任何模型或公开 benchmark 请求。

## TypeSafe primitive fit and confidence semantics (2026-10-22)

The reviewed CSM node uses **Choice** only. Its domain is one member of a closed candidate set: one `article:<knowledge_id>` option for each eligible article, plus `no_match`. It is not a Noul because it is not a yes/no question, and it is not a Score because article candidates have no meaningful ordinal scale. The current TypeSafe definitions describe Choice as selecting from a fixed set, Noul as a yes/no probability, and Score as an ordered set of descriptive levels ([Choice](https://docs.typesafe.ai/primitives/choice), [Noul](https://docs.typesafe.ai/primitives/noul), [Score](https://docs.typesafe.ai/primitives/score)).

The request state is built from the task's issue text, optional product name, and the complete result of the same unbounded, read-only `retrieve_knowledge` call. It carries the published article fields needed for comparison and source digests. The question criteria contain only returned candidate IDs and `no_match`; result truncation is refused. See [`semantic_templates.py`](../../../decagent/evaluation/business/semantic_templates.py) and [`templates.py`](../../../decagent/business/plans/templates.py).

TypeSafe's Choice `confidence` summarizes how concentrated its option probabilities are. It is not measured task accuracy, calibrated success probability for the entire workflow, employee consent, or permission to perform a write ([Confidence](https://docs.typesafe.ai/confidence), [State](https://docs.typesafe.ai/concepts/state)). The paired callback regression verifies identical state and question serialization for Jev and the fixed general-model comparator, using local mock transports only. There is still no real paired trial, so this semantic review does not complete 9.6.D or establish a Jev advantage.
