# 验证流程注册与实例化

此指南覆盖 `business/workflow_registry.py` 的注册和匹配契约。注册表和迁移 017 默认没有流程、验证报告或已发布记录。一次任务成功不会更改流程发布状态；发布始终由当前有效的 `OwnerIdentity` 显式操作。

## 版本与参数

每份定义包含 `workflow_id`、`version`、`goal_id`、当前 `scope_id/scope_revision`、`plan`、`input_schema`、`applicability` 和 `policy_dependencies`。注册表从当前 PluginSpec 派生 `tool_fingerprint`，从 QuestionRegistry 派生 `question_fingerprint`。规范化定义的 SHA-256 是 `definition_digest`。

`plan` 直接使用现有 `Plan` schema 和 `compile_plan`，并使用该流程的输入 schema 编译。不能绕过节点数、依赖、能力、字段类型或完成证据检查。版本内容不可变；修订发布内容必须使用新版本。

输入 schema 使用关闭的结构化对象，不允许外部 schema 引用、预填历史默认值或权限字段。流程不能携带历史员工、操作、动作、审批、授权或凭据字段，也不能携带具体 UUID。业务文本、资源 ID 和资源版本通过 `task_input`、前序真实 `node_output` 或 `accepted_decision` 绑定；资源 `expected_revision` 必须来自本次读取。固定 `verified_memory.memory_id` 不可放入通用流程。

目录 Choice 必须通过编译器的 `QuestionRegistry` 检查，其候选输入来自本次 `directory` 读取。实例化不复制任何旧目录、节点结果、接受的判断、记忆或操作；解释器负责在当前员工权限下重新执行读取。

## 管理接口

| 调用 | 结果 |
| --- | --- |
| `register(owner, definition)` | 编译并登记 `draft`；同版本同内容幂等，异内容拒绝 |
| `validate(owner, workflow_id, version, report, expected_sha256)` | 校验绑定和覆盖，保存不可变报告，进入 `validated` |
| `publish(owner, workflow_id, version)` | 重验 scope、工具/问题和策略，进入 `published` |
| `revoke(owner, workflow_id, version, reason)` | 永久进入 `revoked`，保留版本、报告、实例与事件 |

每次管理操作都会在数据库重新核对 owner 的当前 bearer 或 session；构造一个身份对象不能获得管理权。数据库触发器保护定义、报告、实例和事件不可被普通更新或删除。

`applicability` 为 `{ "roles": [...], "input_equals": {...} }`，角色只能是 `employee`、`handler`、`supervisor`。条件全部同时成立，且仍须通过当前员工真正拥有的命令权限与资源可见性检查。声明角色不能授予权限。

## 报告口径

报告格式为 `decagent.workflow-regression.v1`，必须包含：

- 唯一 `report_id`、内容引用 `reference` 和与规范化定义完全对应的 `definition_digest`。
- 显式 `synthetic: true/false` 及 `quality_claim: false`。脚本/夹具报告必须写 `synthetic: true`。
- 至少 `success`、`missing_input`、`permission_denied`、`tool_failure` 四类通过记录；每例保存唯一 `case_id`、`category`、`passed`。
- 完整的 `covered_nodes` 和 `covered_edges`，覆盖声明的全部节点、分支和异常边。

报告校验能验证结构、覆盖声明与内容摘要，不能独立证明报告所述实验真的执行。报告生产者和导入 owner 必须保留其引用的回归输出。工作流回归报告不会证明 Jev 正确率，也不会发布阈值策略。

带 `decide` 的流程必须精确列出 `{policy_id, policy_version, definition_digest}` 依赖，且与每个判断节点一致。验证/发布及后续匹配会检查现有 DecisionPolicyStore 中真正可用的已发布策略，包含问题版本、primitive、工具契约、workflow 版本与候选来源。缺少真实已发布的策略时，合成工作流回归报告不能补足 Jev 质量授权。

## 明确匹配与实例化

`match(identity, scope_id, inputs, workflow_id=..., version=..., goal_id=...)` 返回 `WorkflowResolution`：

- 用户明确选择流程，或者明确给出已注册目标，且唯一有效版本符合全部条件时返回 `matched`。
- 未给明确目标返回 `clarification_required`；多个合法流程或版本返回 `ambiguous`，不会默默取最新版本。
- 缺字段返回 `missing_input` 和 `missing_fields`；类型错误为 `invalid_input`；角色/输入前提不符为 `not_applicable`。
- 权限、资源、scope、问题或策略变化及撤销分别返回明确的失效原因。调用方应询问或按已有预算规则初次规划，不把未命中计为成功复用。

`instantiate(identity, task_id, workflow_id, version, inputs)` 只接受原员工的 business-3 任务，重新执行匹配，并要求 `inputs.instruction` 与任务原始指令一致。它产生独立 `instance_id` 和新 `plan_id`，持久保存本次输入摘要、员工/账号/scope 版本和模板/报告摘要。

返回 `WorkflowInstance.plan` 是现有不可变 `Plan`；`.task_input` 返回本次输入副本；`.binding_context` 每次返回新的 `BindingContext`，运行时来源集合为空。相同任务、相同版本和相同输入可幂等读取原实例；不能替换已存在的其他计划或在账号版本变化后重新使用旧绑定。

## 撤销与历史

新命中会立即排除已撤销或依赖漂移的模板。`check_instance(identity, task_id)` 提供下一安全边界的当前适用性信号，执行方据此暂停未执行步骤；它不提交、重试或补偿操作。`instance(identity, task_id)` 允许原员工查看历史实例，即使模板已撤销。

既有 operation/receipt 的独立查询与核实路径保持可用。模板撤销不会删除历史，也不会把未知提交变成可重放操作。解释器必须在执行安全边界调用失效检查；单独创建实例不等同于已经完成这项运行时接线。

## 定向验证

从 DecAgent 根目录，使用专用可派生测试数据库运行：

```powershell
python -X utf8 -m pytest evals/deterministic/test_business_workflow_contracts.py evals/integration/test_business_workflow_registry.py -q --tb=short
python -X utf8 -m ruff check decagent/business/workflow_registry.py evals/deterministic/test_business_workflow_contracts.py evals/integration/test_business_workflow_registry.py
```

集成用例使用新建 `case_*` 数据库；没有配置 `DECAGENT_TEST_DATABASE_URL` 时的 skip 不算数据库验证通过。测试夹具的报告始终标记为 synthetic regression，不能作为真实模型质量或性能证据。
