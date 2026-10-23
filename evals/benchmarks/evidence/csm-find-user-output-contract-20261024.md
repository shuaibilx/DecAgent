# CSM `find_user` 输出契约诊断与兼容修正

执行日期：2026-10-24（Asia/Shanghai）

## 诊断范围

在固定 CSM 镜像与冻结数据库快照上，用该公开任务要求的只读 `find_user` 查询复核输出契约。此次没有读取或传入私有 verifier、目标状态、参考动作或标签；没有调用 Qwen、Jev、τ 模拟器或官方评分器，没有运行写工具，也没有修改历史 v10 账本。

原始 MCP 响应正文没有写入此报告或提交。只保留契约结构、字段类型和校验路径摘要。工具 output schema 摘要 SHA-256 为 `c524ed05509b770cf659472ab67d63ecf0094c985803ec38b98d601e5e0d49a6`。

## 结果

- MCP 传输返回 `isError=false`，响应通过单个 text block 携带 JSON。
- 冻结的 `find_user` output schema 将 `active` 声明为 boolean；真实响应将该字段编码为 JSON integer，因此原始严格校验在 JSON Pointer `/active` 失败。真实隔离集成确认该整数符合 `0/1` 数据库 bit 编码；具体记录值未进入报告。
- 调用前后数据库状态指纹相同；退出隔离环境后清理验证通过。
- 原有 v10 attempt 缺少调用参数、原始 MCP 响应与 traceback，所以本次复现不能证明历史 `ValueError` 就是同一个原因；历史 `unknown` 保持不变且没有重放。

## 修正

评测适配器仅在内联 `properties`、schema-valued `additionalProperties` 和对象 `items` 中声明为 boolean 的节点，将精确整数 `0`/`1` 规范化为 `false`/`true`；其他整数仍按原规则校验，schema 同时允许 integer/number 时不转换。适配器不展开 `$ref` 或组合关键字，未匹配的整数继续严格校验并失败关闭。原始 MCP envelope 仍保存在局部评测 journal，规范化路径以 JSON Pointer 记录在独立字段。ReAct 的工具消息与 Plan 的节点输出使用同一规范化业务值，避免两条路线因适配差异看到不同类型。输出封装版本升至 `enterprise-mcp-observation-v2`。

## 验证与边界

- 新回归先在旧行为下失败，再验证 `0`/`1` 映射、`2` 拒绝、integer/boolean 联合 schema 不转换、ReAct 轨迹使用规范化结果。
- 覆盖评测目录、session、路线执行、语义注册、评分与最终回复的确定性回归共 `157 passed`；全仓 Ruff 通过。
- 固定 CSM 镜像的只读 MCP 集成 `1 passed, 4 deselected`，确认 `/active` 被规范化、数据库未变且环境清理成功。
- `openspec validate jev-conditional-workflows --strict` 与 `openspec validate guest-sop-local-trial --strict` 均通过；`git diff --check` 无空白错误。
- 此修正只完成一个工具结果契约兼容边界。它不是正式 benchmark 路线，不产生任务得分，也不证明真实模型、三路线调度、Jev 收益或四域端到端闭环。
- v10 的 4 任务/8 路线历史状态、费用预留、unknown、评分和清理证据均未被改写；启动新 cohort 仍需要新候选、同 SHA CI、冻结预算与单独授权。
