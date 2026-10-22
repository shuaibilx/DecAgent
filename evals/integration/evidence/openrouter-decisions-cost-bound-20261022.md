# OpenRouter Decisions API 费用上界契约审查

- 日期：2026-10-22（北京时间）
- 目的：判断 `provider.max_price` 能否作为游客 Jev 的强制单次费用上界。
- 模型/API 请求：无；未发送 OpenRouter 或 Qwen 请求。

## 核对来源

- [OpenRouter Decisions API 请求契约](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request)：`POST /api/alpha/decisions` 的 body 列出 `model`、`questions`、`state`、`provider` 等字段；`provider` 类型为通用 object/null，示例为 `allow_fallbacks`。响应示例返回 `usage.cost`、`input_tokens` 与 `output_tokens`。
- [OpenRouter Provider Selection / Max Price](https://openrouter.ai/docs/guides/routing/provider-selection)：说明通用 provider routing 的 `provider.max_price` 可按输入/输出每百万 Token 价格筛选 provider，并称无满足价格的 provider 时会拒绝执行。
- [OpenRouterTeam 官方 Skills 仓库](https://github.com/OpenRouterTeam/skills)：列出 `openrouter-decisions`，用于 OpenRouter Decisions API 的三类 typed decisions；上游 HEAD 在审查时为签名验证通过的 `33597a44e38b0cdc38ea1256ef8bcf50c2704e3d`。

## 结论

Decisions 专属 API 契约没有明确列出 `provider.max_price` 的字段结构、单位或此端点上的硬拒绝保证。通用 Provider Routing 文档说明该设置用于请求路由；仅因 Decisions 请求也存在 `provider` 对象，不能证明它一定是该端点可验证的总费用上限，也不能推导出绝对费用上界。

因此，游客 Jev 继续由 `UsageLedger.reserve()` 在 task/trial 预留之前拒发。固定 `$0.01` 仍是记账预留，不是费用上界。后续若 OpenRouter 在 Decisions API 专属契约中明确支持并保证费用上限，仍需按冻结模型、输入/输出限制与请求体做本地 mock 传输测试；mock 只能证明 DecAgent 发出配置，不能替代供应商的契约保证。

## 上游技能许可边界

GitHub API 在该仓库返回 `license: null`，也没有找到根目录许可证文件。本项目没有复制该技能源码。另新增项目自有的 [DecAgent OpenRouter Decisions 参考 skill](../../../.agents/skills/decagent-openrouter-decisions/SKILL.md)，只记录本项目实施边界并链接官方来源。


## Current official-docs recheck (2026-10-22)

The current [Decisions API request reference](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request) still describes `provider` as an optional object or null and gives `allow_fallbacks` as its example; it does not declare `max_price` as a child field or promise a hard fee ceiling. The [Provider Selection guide](https://openrouter.ai/docs/guides/routing/provider-selection) documents `max_price`, but scopes the `provider` object in its overview to Chat Completions requests. These pages therefore still do not prove that `/api/alpha/decisions` accepts and enforces that ceiling.

No OpenRouter test request was sent. Keep guest Jev blocked before reservation until a Decisions-specific contract or written provider guarantee documents the supported field, units, and enforced rejection behavior. The existence of `provider.max_price` in generic routing documentation is not evidence that it applies to the Decisions API.
