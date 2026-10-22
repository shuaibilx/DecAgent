# 游客预算费用说明验收记录

- 日期：2026-10-22（北京时间）
- OpenSpec：`guest-sop-local-trial` 任务 3.5
- 模型/API 请求：无

## 完成内容

- 新增面向操作者的[预算与模型费用说明](../../../generated/261022/0951_游客预算与模型费用口径.md)，覆盖每日限额、Qwen/Jev 标准费率来源与版本边界、抵扣前费用、代金券与现金证据、CFETS 汇率快照、10% 余量、¥2 换算示例及预留/结算/unknown 状态图。补充核对了 Qwen 官方 `qwen3.8-flash` 分地域目录价和 OpenRouter Jev Latest 当前目录价；这两者均不能替代用户 API Key 的逐笔账单证据。
- 游客工作台展示费用口径和 Jev 当前会在派发前拒绝的原因。任务详情区分已知费用与尚未解决的 unknown；零费用不会掩盖 unknown。
- OpenRouter 官方接口只读审查发现：通用 Provider Routing 文档描述 `provider.max_price`，但 Decisions API 专属契约没有明确承诺该端点执行此硬上限。游客 Jev 仍维持预留前拒绝，依据见[契约审查](openrouter-decisions-cost-bound-20261022.md)。
- 新增 DecAgent 自有 `decagent-openrouter-decisions` 技能参考。OpenRouterTeam 上游 Decisions skill 的仓库没有声明许可证，因此未复制其文件；本地参考写明来源与集成限制。

## 2026-10-22 官方价格来源复核

- 用户指定的千问 API 价格目录适用于本项目端点；当前公开 Qwen3.8-Flash 条目列出普通输入 ¥0.8/M、缓存命中 ¥0.1/M、输出 ¥2.7/M，Batch 与显式缓存另有价格。评测 profile v4 已绑定标准实时价格；历史 profile/manifest 未改写。
- 同一千问平台的 DeepSeek-V4.1-Flash 页面列出人民币计价，并区分忙时/错峰时段；这些价格仅适用于千问平台线路。项目备用模型走 OpenRouter，不能套用该人民币费率。
- OpenRouter `deepseek/deepseek-v4.1-flash` 页面当前目录基准价为 $0.07/M 输入、$0.50/M 输出、$0.008/M 缓存读取，同时列出不同上游费率；实际用量按该 generation 的响应结算。游客备用路由在上游和最大费用未受约束前不视作有可靠费用上界。
- `~typesafe/jev-latest` 当前解析至 Jev 1.14；精确版本页显示 $0.042/M 输入、$0/M 输出。后续可复现实验须保存实际版本；当前主 profile v4、v8/v9 manifest、历史账本和实验结果均未重写。

对应可复核页面：[千问 API 价格目录](https://platform.qianwenai.com/pricing/api)、[Qwen3.8-Flash](https://www.qianwenai.com/models/qwen3.8-flash)、[千问平台 DeepSeek-V4.1-Flash](https://www.qianwenai.com/models/deepseek-v4.1-flash)、[OpenRouter DeepSeek V4.1 Flash](https://openrouter.ai/deepseek/deepseek-v4.1-flash)、[OpenRouter Jev 1.14](https://openrouter.ai/typesafe/jev-1.14)。本次仅读取公开文档，没有向任何模型或业务 API 发请求。

## 验证

- `node evals/deterministic/guest_workbench.cjs`：通过，覆盖游客费用披露、零费用、unknown 和工作台费用状态。
- `.venv\Scripts\python.exe -m pytest evals/deterministic/test_guest_trial_budget.py evals/integration/test_business_guest_trial_budget.py -q -rs`：`39 passed`。28 个 PostgreSQL 集成用例使用一次性 `postgres:16-alpine` 容器，绑定 `127.0.0.1:55439`；测试后停止并移除。没有改动既有应用容器或数据库。
- `ruff check decagent/business/trial_budget.py decagent/storage/usage.py decagent/runtime/model_calls.py`：通过。
- OpenSpec strict 验证与 `git diff --check`：通过。
- `skill-creator` `quick_validate.py`（UTF-8 模式）：技能格式有效。

未访问供应商 API、未发送模型请求；费用示例仅做十进制换算，不代表发票对账或游客 Jev 已开放。
