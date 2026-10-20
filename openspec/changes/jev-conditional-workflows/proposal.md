# Proposal

## 历史执行快照（2026-10-20，首波 live 执行前）

当前主 change 有 78 项任务，已完成 66 项，剩余 12 项：9.6.D–G、10.3–10.6、10.8.D、10.10.B、10.10.D–E。`guest-sop-local-trial` 为 0/28。公开基准仍是 0/286；固定首波 4 个任务、8 个路线槽位已有受门禁保护的 live CLI，但本次没有派发 live 模型请求或 ledger-backed live route。另有 4 条 EnterpriseOps 脚本路线用于工程验收，均只执行一次只读查询并取得 0.0 官方任务分数；它们不是首波真实模型试次。当前代码工作树有未提交修改，精确候选 SHA 门禁因此阻止执行。9.6.B 的 EnterpriseOps 原始 verifier 工程验收与 9.6.C 的 τ bridge 工程 smoke 均已完成；这些结果都不代表 Agent 正确完成公开任务或首波真实模型验收。下方更早的状态作为历史证据保留。

首波已有规范账本、模型/工具账本适配器、manifest 身份绑定、单路线入口、受控 provider callbacks、四任务环境预检和 first-wave phase scheduler。τ bridge 现拒绝不接受 `timeout_seconds` 的同步模型 callback，防止无界 callback 进入派发路径；用户模拟器/裁判时限现为请求级 cap，不再修改共享 channel 默认值。但无法强杀自行忽略 timeout 的任意 Python 同步函数。CLI 要求同一干净 HEAD 存在成功 GitHub CI，校验模型配置与 readiness、本地摘要固定的 EnterpriseOps 镜像、τ Python 3.12 runtime 和新报告路径，然后才可进入 supervisor 与规范账本。远端候选 `2b65a2a1afc21074385caa3874c139833e363bfb` 已修复离线测试依赖后，候选 `60ea6f6dce5855cb078c4796abeaa3708d988247` 的 CI 又发现 3 个平台/配置边界测试问题（2 个 τ 清单原始换行校验、1 个 dry-run 读取 live `.env`）。三处修复现位于本地未提交工作树，完整回归和远端复验待完成。本轮尚未进行模型 API 调用。四域真实受控响应闭环、强制取消、评分/清理故障注入和完整首波尚未验收，不能称为真实评测闭环。最新完整确定性回归为 `1906 passed, 18 skipped`（包含第三轮 CI 修复后的本机结果），这不改变 66/78 与 0/286 状态。

## 最新真实评测状态（2026-10-20，首波 live 执行后）

固定四域首波已在候选 `9bc3ee6654e46630c9ae8238a28eb6e34a088698` 上实际派发，精确候选 CI run `39442012550` 全部通过。4 个任务共执行 ReAct/Plan 8 条路线；结果为 4 unknown、4 failed、0 completed，8/8 cleanup verified，8/8 官方评分未启动。首波工程闭环因此未通过，公开基准仍没有任何已评分任务；历史状态中的“尚未派发”只代表执行前快照。

账本记载 4 次 Qwen 请求：1 次有 6,952 tokens 的已知用量，3 次 usage unknown；账单均不完整，代金券不能把未知费用折算成 `$0`。Jev 与 judge 请求数为 0。unknown 工具 attempt `find_user` 的 effect 被目录标成 `write`，结果未知；不能声称没有业务副作用。所有路线均不重放，首波剩余 smoke 也停止派发。机器报告和可读摘要分别见 `evals/benchmarks/evidence/public-first-wave-live-20261020.json` 与 `public-first-wave-live-summary-20261020.md`。

本次执行也发现 CLI 在报告写盘成功后，会因将内存中的 Decimal 直接传给 `json.dumps` 而以退出码 1 结束。已通过 Decimal 回归测试修复 stdout 序列化，没有覆盖或重写已落盘的 live 报告，也没有再次派发模型/工具请求。现有账本只保留粗粒度失败类别，没有足够诊断信息离线识别派发失败阶段；下一次独立批次之前需要先做离线问题定位并补充不包含请求正文或密钥的诊断证据。

离线诊断切片已完成并新增任务 10.10.F；OpenSpec 进度为 `67/79`，仍有 12 项待办：9.6.D–G、10.3–10.6、10.8.D、10.10.B、10.10.D–E。新字段只保存白名单失败阶段、异常类名及有限 HTTP 状态；旧账本的 unknown 不重放，补录清理证据保留原诊断，live report v2 导出安全的 attempt 摘要。定向回归 `167 passed`，旧 v1 报告与 canonical ledger 未重写。本次真实试次没有证明任务质量、路线间性能、Jev 收益或成本优势，`--execute` 仍未放行。

### 2026-10-20 EnterpriseOps 原始 verifier 工程验收

完成 9.6.B 的离线任务评分边界：固定 CSM/ITSM smoke 任务分别创建两个全新容器与数据库，采集并比对私有状态指纹，先验证原 MCP 读取，再把冻结任务自身的原始 verifier 交给 pinned `VerifierEngine`。在不执行任务动作的情况下，两域各两次均得到完整的官方 `task_failure`（CSM 0/4、ITSM 0/2），评分状态为 `scored`，cleanup 经核验；中间副作用明确为 `not_measured`。任务 verifier/SQL/期望标签只留在 evaluator 侧，不进入 `model_input`。证据：`evals/benchmarks/evidence/enterpriseops-official-verifier-smoke-20261020-csm.json` 与 `enterpriseops-official-verifier-smoke-20261020-itsm.json`。Docker 集成 `2 passed`；评分与路由失败分类测试 `92 passed`。这是可复现的原始评分器工程验收，非模型路线或任务成功率成绩；0 模型请求、0 业务写入，公开基准仍 0/286。


### 2026-10-20 CSM 知识文章语义模板审阅

首波 CSM 任务中，用户要求针对产品和故障描述查找并引用合适的知识文章。这一判断依赖自由文本语义与真实检索候选，适合受限 `Choice`，并含显式 `no_match`；用户工单数量比较和 ITSM 已给出的用户/服务/状态字段继续由代码处理。`csm_knowledge_article@1.0.0-csm-knowledge-1` 已接入评测专用隔离 `EvaluationCatalog` registry、Plan compiler、`ControlledRoutes` 和生产 `PlanRuntime`。模板消费同一个 `retrieve_knowledge` read node 的原始查询参数与实际返回的已发布文章，并保留 `no_match`；不截断候选、不接受隐藏标签、不批准知识关联或通知写入。编译器拒绝有限 `limit` 与非字面量 `state=published`；运行时只接受来自确切返回数组的 integer article ID。超过 24 个候选或超过 16,000 字符的上下文时该判断不适用。该模板不会通过生产 PluginSpec、HTTP 或用户 manifest 注册。

证据 `evals/benchmarks/evidence/jev-semantic-template-audit-20261020.json` 与 `jev-semantic-template-registry-20261020.json` 来自 pinned CSM MCP 容器工具目录审阅及真实 registry 核验；cleanup verified，0 业务工具调用、0 模型请求。上游 `retrieve_knowledge` 没有 `readOnlyHint`，本地隔离目录仅对匹配已审 schema 的工具应用只读覆盖；生产 capability registry 保持不变。registry/compile/scripted route 回归 8 passed，模板构造器 6 passed，相关公开 Session 套件 36 passed。工具响应没有总数或截断元数据，因此只声称比较实际返回数组。当前 first-wave dry-run 状态已更新为 `reviewed_csm_natural_case_pairing_pending`；该标签表示自然场景已审核但 Jev/Qwen 配对尚未排期，冻结首波仍只有 ReAct/Plan 两路线、8 个路线槽位。真实同状态配对和官方任务成绩仍未运行，故 9.6.D 保持未完成、公开基准仍为 0/286。

### 2026-10-20 EnterpriseOps 脚本路线工程闭环

固定首波 CSM/ITSM 任务各运行 ReAct 与 Plan 一次，共 4 条隔离脚本路线；使用摘要固定的真实 Docker 镜像、真实 MCP read call、生产 `ControlledRoutes`/`PlanRuntime` 和原始 EnterpriseOps verifier，并逐条核验 session/environment cleanup。CSM `find_product` 与 ITSM `list_users` 的只读例外仅绑定各自精确的 pinned image 与工具 schema digest；任何 schema/image 漂移都回到默认写操作策略，显式 `readOnlyHint=false` 仍优先。四条路线均成功到达原始评分器，均为预期的 `task_failure`/0.0，因为脚本只执行一次只读查询；provider 请求和业务写入均为 0。证据：`evals/benchmarks/evidence/scripted-enterprise-first-wave-routes-v2-20261020.json`。该结果验证隔离工具、执行器、评分与清理链，不是模型质量或性能结果，也不计入首波 8 条真实路线或公开基准分母。

接线时发现 MCP 返回 `structuredContent: null`、有效 JSON 位于 text content 的兼容情况会被错误地当成最终结构化值，导致输出 schema 校验失败。评测目录现仅在 `structuredContent` 非 null 时采用它，否则解析文本 content；精确只读 digest、显式写提示优先级和该回退由 `test_business_evaluation_catalog.py` 覆盖，定向套件 `14 passed`。τ ReAct 用户模拟与官方评分在锁定的 Python 3.12 worker 上复核 `2 passed`；随后把实际 Plan read-tool 路线扩展到 τ retail 与 telecom，分别调用 `get_order_details` 和 `get_customer_by_phone`，官方 evaluator、transcript 持久化和 cleanup 均通过，参数化测试 `2 passed`，证据：`evals/benchmarks/evidence/tau-plan-cross-domain-smoke-20261020.json`。所有回调仍为脚本响应，不代表 τ 官方 test split 或首波真实模型试次。OpenSpec 任务计数仍为 `66/78`，待办 12 项不变；公开基准仍为 `0/286`，dirty working tree 仍阻止 live candidate gate，本轮未发送 Qwen/Jev 请求。

本轮最终本机回归：`uv run --extra dev pytest evals/deterministic -q` 在新增 telecom Plan 参数化之前为 `1897 passed, 16 skipped (281.19s)`；新参数化测试本身在锁定 τ worker 上 `2 passed`。使用独立临时 PostgreSQL 17 容器运行核心数据库/MCP/预算/业务路由集成的 7 个测试文件，`80 passed, 0 skipped (241.03s)`；证据 `evals/benchmarks/evidence/postgres-core-integration-20261020.json`，容器与临时数据库均已清理。Python 3.11/3.13 上三个关键评测/门禁文件各 `37 passed`；证据 `evals/benchmarks/evidence/python-matrix-smoke-20261020.json`。Python 3.11 editable 安装在当前中文 Windows 路径遇 CP936 `.pth` 解码问题；使用非 editable wheel 在临时 ASCII 路径通过，本项目 `.venv` 已恢复到 Python 3.13 且 `uv run` 冒烟通过。`uv run --extra dev ruff check decagent evals` 通过；`openspec validate --changes --strict` 为 `6 passed, 0 failed`；`git diff --check` 通过，只有现有 Windows LF/CRLF 转换提示。`uv build` 成功生成 wheel 与 sdist，`decagent --help` 和 `decagent business-server --help` 可运行。16 项跳过不视作通过；全量 integration 与核心浏览器 E2E 仍未验收，以上均不替代远端 CI、真实模型首波或公开基准。

## Why

当前 DecAgent 在普通工具队列耗尽后继续调用主模型，尚不能一次表达后续 Jev 判断、条件分支和未来参数依赖，限制了 Jev 低成本、低延迟的价值。需要把业务执行改为可验证、可恢复的条件计划，并在完整的模拟业务工作流中公平比较多种执行路线，检验 Jev 的实际收益和适用边界；不预设 Jev 必然优于代码规则或主模型。

### 当前证据状态（2026-10-19）

10.9 routed readiness 的技术检查已通过：Qwen 只读计划经生产 compiler 接受，Jev readiness 使用独立累计账本。该 readiness 的旧 profile 把 Qwen 费用记为 `$0`，不能作为计费或成本证据；旧请求、unknown 和原始记录均保留。公开基准尚未启动，206+80 正式任务仍为 0/286；固定 smoke 只有离线 preflight 证据。该状态补充 supersedes 本 change 中“主模型未就绪”的较早技术状态，但不改写计费结论。

### 计费口径更正（2026-10-20）

Qwen API 调用仍按服务计费，账户代金券只抵扣账单，不改变 API 费用。正式成本比较使用抵扣前 API 费用；代金券抵扣和账户实际支付分别记录。`evaluation_model_profiles_v2.json` 中 Qwen 的零费率来自此前对“免费额度”的错误理解，不是供应商费率或账单证明，不能用于成本结论。保留旧 profile 与原始证据，新增计费 profile 时绑定可核验的服务费率或账户账单来源；来源不足时成本记为 unknown，而不是 `$0`。用户已明确接受首波 Qwen 批次按冻结的请求、Token、墙钟停止线运行，即使实际单价暂未核验；这些 Qwen 请求不设金额上限，金额保持 unknown 时仍可用于任务质量评分，但不能用于成本优劣结论。Jev 仍必须受原已知 `$1` 费用账本约束，未知费用不派发。

首波资源停止线同时固定为最多 500 次 Qwen 请求、5,000,000 输入与输出 Token、4 小时总墙钟；Agent、官方用户模拟器、裁判分别最多 300/160/40 次请求及 3,000,000/1,600,000/400,000 Token。首波 8 条基础路线合计最多 1,600 次工具调用。它们是批次硬上限，不是预期消耗；24 例 smoke、全量及额外消融须按首波实耗另行估算。

### 2026-10-20 apply 实施状态

批次账本 schema 已到 v6；准入与 route/model/tool/machine-time 预留在同一 SQLite 写事务执行。新增仅限首波的规范账本打开入口，要求项目 OS 生命周期锁已持有，并只读打开既有累计 Jev 账本。生产 PlanRuntime 的公开工具调用已连接 ledger adapter；工具结果 unknown 时路线不再继续，也不会调用官方评分器。route ledger 将业务、评分与清理分开保存，unknown 后允许补记清理证据但禁止改写业务终态。模型侧已有单次 `LedgeredModelCallback` 与 `LedgeredModelAdapterFactory`，可把规划、ReAct、语义判断、总结、τ 用户模拟器和官方 judge 的 callback 接到公开账本；用户授权的 Qwen 角色可在批次用量硬上限内以 null 费用 reservation 执行，Jev 不享有此例外。managed model callback 客户端在评分和评分用量读取后统一幂等关闭。离线 phase 预检可区分 first-wave、remaining-smoke 与 full 的冻结 trial 子集；first-wave 已有规范账本绑定的 claim/resume scheduler、provider/session factories、四任务环境预检与受门禁 CLI。四域受控模型响应/官方评分/清理的故障注入仍未验收。

上述多数是离线组件证据，不等于真实评测运行。受门禁保护的 first-wave CLI 与 provider/session/environment factories 已接通；四域受控响应、官方评分/清理故障注入、任意阻塞 callback 的强制取消和 Jev unknown 的 live runner 注入仍未完成。公开 benchmark 继续为 0/286；本轮未发送模型请求。本机公共 release Runner 已注销并停用自启；远端 `main` 没有部署 job，公共 CI 与 GHCR 发布保留。

部署边界复核（2026-10-20）：通过 GitHub Actions API 复核，`shuaibilx/DecAgent` 当前注册 Runner 数量为 0；远端 `release.yml` 的 job 只有 reusable CI gate 与 `ubuntu-latest` GHCR publish，没有本机 self-hosted deployment job。Windows 上精确检查的 `DecAgentLocalDeployRunner` 计划任务状态为 `Disabled`，Runner.Listener/Worker 进程及 Runner Windows 服务均为 0。公共 CI、GHCR 发布和现有 DecAgent 容器保持运行。当前工作树将 release workflow 显示名改为 `Release image publishing (GHCR only)`，并新增 deterministic boundary test；该名称修正尚未合并到远端，远端显示名仍是历史的 “Release and local deployment”。本项确认实际 dispatch 边界已冻结，也增加仓库回归门禁；不代表本机自动 CD 已完成。

模型身份复核（2026-10-20）：冻结 profile 记录请求模型；响应可取得的上游实际模型名另行记录。provider 未返回实际模型名时保留 null，不以请求名填充；如果实际返回模型与冻结 profile 不一致，则本次派发记录保留，并在规范账本中持久写入 `model_profile_drift` 停止原因，阻止后续路线派发，不能拼接为同一模型实验组。

试次身份复核（2026-10-20）：公开调度器与单路线入口现要求 `PublicComparisonTrial` 绑定清单 ID，并在 route reservation 前核对 benchmark、版本、case、exposure、资源、回复策略及角色 profile digest。账本 ID 不进入配对指纹。Resolved provider 同样只保存上游实际返回值，缺失时为 null；请求 provider 由冻结 profile 单独表示。此切片的定向身份与 dispatch 回归共 32 passed，不构成 live route 执行，`--execute` 仍关闭。

Jev unknown 跨路线账本回归（2026-10-20）：在第一条路线的 Jev 请求进入 unknown 后，第二条运行中路线的公开账本预留 API 拒绝新增 Jev 请求，不产生新的本地/global reservation，既有 unknown reservation 与额度占用保留。该失败关闭属性已通过公开 API 的确定性回归验证；它没有真实 provider/session factory，不能替代统一 runner 下的 Jev dispatch 故障注入或官方评测。

## What Changes

- 2026-10-19 首轮 Qwen 就绪诊断保留原 3 次尝试及 unknown 证据。后续独立诊断批次优先使用 `qwen3.8-flash`，可在暂时性网络/限流/服务端故障后重试一次，再调用 OpenRouter `deepseek/deepseek-v4.1-flash` 一次；旧账本不重置，未知预留保留，新尝试须在每次派发前按同一任务总上限预留。新增诊断批次上限 8 次、累计 300,000 输入与输出 Token、DeepSeek 费用最多 1 美元；此授权不覆盖正式基准或 Jev 额度。通用模型角色支持固定的主/备模型配置，正式对照固定模型配置并关闭跨模型降级。唯一的本地恢复例外是已持久证明“从未派发、实际 token/费用为零”的 preflight 失败：归档原失败证据后可在同一授权额度下继续；任何已派发或 unknown 请求均不可重绑定、清零或重放。
- 游客多空间、本机 SOP 拓扑和私有仓库 CD 由独立 `guest-sop-local-trial` change 承接，本 change 的五项公开评测待办保持原验收标准。

- 将已注册 MCP/Skill 业务编排为有类型、版本和界限的条件计划；LLM 引用可信判断模板，代码绑定真实参数并执行，已知分支不追加主模型。
- 采用可替换规划器；普通任务以 1–3 次实际通用模型请求为目标，异常局部重规划另设上限。区分主模型、Jev 与执行预算，所有重试和辅助请求均记账。
- 增加 Choice/Noul/Score 分类型确定性策略和持久人工待办；低分直接由人选择、纠正或补充，达到阈值仍须满足权限及独立的写入确认。
- 让任务工作记忆、Jev 决策和真实回执进入受限的后续上下文；跨任务记忆只通过已发布工作流显式声明的稳定槽读取，以记忆类型与 subject/key 绑定，每次按当前员工身份解析有效记录，不自动注入通用规划上下文。
- 发布可验证的复用计划，适用任务可零通用 LLM 执行，仍重新读取业务事实与权限。
- 在工作台展示计划路径、阈值、实际模型请求、等待原因和 trace；同步指南、架构、泳道、流程、时序与恢复图。
- 以 EnterpriseOps-Gym 的 CSM/ITSM 任务作为主要端到端工作流评测，以 τ-Bench 作为对话、澄清和用户模拟的补充评测。意图分类数据不得替代完整业务流程的成功率证据。
- DecAgent 自有合成集只作辅助回归与契约诊断，不进入主要业务成绩、阈值选择或策略发布。保持冻结集合和 holdout 边界；缺少独立复核验证队列时保留人工默认与样本不足结论。
- 在隔离评测会话中注册受信的原始公开 MCP 工具目录，复用生产编译器、参数绑定和 PlanRuntime；评测目录不能通过生产插件注册或 HTTP 任务参数启用。业务成功仍由官方 verifier 判定，评测完成信号不等于生产业务完成证明。
- 对照 ReAct + Skill + MCP、一次规划 + 确定性执行器 + MCP、一次规划 + 确定性执行器 + Jev 运行时语义判断。结构化条件由代码处理；Jev 只评估运行时语义分支，并与同一状态和候选上的主模型二次判断比较。只有在质量可接受且成本或延迟确有改善时才报告 Jev 收益。
- 公开路线对照采用相同的评测专用资源上限，并将保留生产预算的结果单独作为副评；EnterpriseOps 全部 206 条任务按是否暴露于冒烟/开发分组报告，τ-Bench 官方 test 80 条保持冻结。
- τ-Bench 的官方用户模拟器及确有必要的模型评分使用配置的千问服务，和 Agent 请求分开记录；不隐式启用上游 GPT/Claude 或自动重试。
- 公开计划路线默认从本试次实际工具证据确定性渲染最终回复；无法满足预注册输出契约时，允许一次受控 Qwen 总结，计入 Agent 主模型请求、Token、费用和执行延迟。所有路线绑定相同的输出策略，不读取 verifier 私有内容决定是否总结，不用计划目标文字冒充结果。
- 通用模型的有限重试/降级必须逐实际派发计入请求、Token、费用和延迟。Qwen 暂时故障最多尝试两次，随后可对同一纯模型步骤使用 DeepSeek 一次；未知费用的预留不得释放，只有备用请求有足够剩余预算时才允许继续。业务工具写入不得因模型降级重放。
- Qwen 在某一公开路线上的用量 unknown 时，终结该路线并停止其后续工具、模型和裁判调用；调度器可在批次上限内继续其他尚未开始的路线。Jev 用量或费用 unknown 时，暂停所有新增 Jev 请求，保留全局累计账本预留；不影响不需要 Jev 的路线。
- 首批真实冒烟先取 CSM、ITSM、τ retail、τ telecom 各自冻结清单首例，分别跑 ReAct 与 Plan，共 8 个基础路线试次；四域逐域完成工具、最终回复、官方评分和清理闭环后，再补齐原定 24 个 smoke 案例。Jev 只加入自然适用且已审核的语义节点。
- 自动处理部分实测正确率目标至少 95%，报告统计置信区间并最大化满足目标的覆盖率；默认阈值在验证集选择，测试结果不足或不达标则如实标记。
- **BREAKING（新业务任务内部运行协议）**：新任务使用新的图与计划版本，替代当前普通队列循环。旧 business-2 任务保留隔离恢复路径，不原地改写 checkpoint、不重放已提交操作。旧低分自动 primary fallback 不用于新条件计划；硬权限、审批和未知提交核实保持有效。

本轮不接入 Pi 实现、不支持任意未注册工具、不连接御龙真实服务、不变更 MCP 协议、不实施部署或删除历史仓库修复功能。实施后可删除新业务默认路径中的重复队列逻辑；兼容恢复和基线所需实现仍隔离保留。

## Capabilities

### New Capabilities

- `typed-business-plans`：可信判断模板、类型化条件计划、参数引用、静态校验与规划器边界。
- `conditional-plan-execution`：持久节点执行、批量判断、局部重规划、分层预算、旧任务兼容和副作用恢复。
- `confidence-gated-intervention`：分类型阈值、有效策略、持久人工接管与原节点恢复。
- `business-context-memory`：业务工作记忆、证据上下文、受限跨任务记忆及隔离、纠错和撤销。
- `verified-workflow-reuse`：复用计划发布、适用性校验、版本失效和零主模型执行。
- `conditional-workflow-workbench`：计划与决策展示、人工操作、可观测性和使用文档。
- `selective-business-evaluation`：EnterpriseOps-Gym 主评、τ-Bench 补充评测、完整工作流筛选、Jev 与主模型配对比较、阈值选择及质量/成本/延迟验收。

### Modified Capabilities

无已落盘的主规格可直接修改：`openspec list --specs --json` 返回空列表，因此以上均为 ADDED 能力。相关历史契约仍位于 business-workflow-agent、mcp-business-runtime 和 build-decagent-workbench 的 change specs；本 change 明确新运行版本的差异并保留治理约束，不在本轮隐式归档或重写旧 change。

## Impact

- 规划与实现目标均为 DecAgent 项目根目录；change 规格、设计和任务保存在 openspec/。Apply 已持续实施，逐项状态以 tasks.md 与验收证据为准。
- 主要影响 decagent/business/{services,graph,worker,planner,questions,calibration}.py、新计划/上下文/记忆模块、models/primary.py、storage/usage.py 及追加迁移、gateway/business.py 与业务工作台、evaluation/business 和 evals。
- 复用现有 MCP 客户端/服务端、employee actor、prepare/确认/commit、幂等回执、租约与 PostgreSQL/LangGraph 持久化；不引入必需 Pi、向量数据库或外部 trace 服务。
- 评测环境只在许可、版本和运行要求核实后固定 benchmark/task/容器镜像/数据库快照指纹；不得以缩小或改写任务来适配 Agent，也不得将意图分类成绩称为流程成绩。此前累计 Jev 1 美元预算继续生效。Qwen API 仍计费，代金券抵扣不等于零费率；抵扣前 API 费用、代金券抵扣与实际支付分别记录。部署和独立 Linux 验收保持后续待办。

### 2026-10-19 实施状态更新

10.9 routed model readiness 已通过；公开基准仍为 0/286，真实四域首波尚未派发。当前新增 v2 model profile、v5 trial manifest，保持 24 个 smoke/286 个 full case 分母，并固定四域首波 4 个任务与 8 个 ReAct/Plan 路线槽位。公开 runner 继续 fail closed；当前变更只补齐离线配置与恢复安全，不把 dry-run 或脚本适配证据称为模型表现。

Jev 使用项目既有累计账本。路径校验拒绝替代路径和符号链接，公开 Jev attempt 可关联同一 cumulative reservation；尚未接通 live runner 对规范账本的加载，也尚未冻结 Agent/模拟器/裁判的批次总请求、Token、费用和墙钟预算。公开执行租约已加入 heartbeat、过期接管与 generation fencing，相关离线并发安全检查通过；公开任务调用仍待模型/工具 callback、官方评分、资源门禁与清理闭环。

### 2026-10-20 评分持久化与运行时复核

公开账本现可在 `score_status=scored` 时持久保存来源受限的最小官方评分摘要，并验证其摘要与冻结 benchmark 相符；旧 v6 route 表通过 nullable 加列迁移，不填补历史评分。unknown route 仍只能补 cleanup，不能改写为 scored。相关 ledger 回归 `42 passed`；集成回归 `4 passed, 2 skipped`，其中两项 PostgreSQL 路由测试因未配置专用 `DECAGENT_TEST_DATABASE_URL` 而跳过。全确定性套件 `1806 passed, 13 skipped`，Ruff 与 OpenSpec 严格校验通过。首波 dry-run 固定选择 CSM、ITSM、τ retail、τ telecom 各一条任务，共 8 条 ReAct/Plan 路线；dry-run 创建 0 个账本、发出 0 次模型/MCP 请求。EnterpriseOps Docker readiness 2 passed；τ 固定 Python 3.12 官方任务初始化在 PowerShell 设置 UTF-8 mode 后为 80/80 ready，τ bridge 定向回归 24 passed。以上均为环境/契约验证，不是首波任务执行。正式公开任务仍 0/286，未新增 Qwen、DeepSeek 或 Jev 请求，`run_public_trials.py --execute` 继续 fail closed。provider client、统一 runner、实际 deadline 取消、τ 官方步数、四域真实评分与 cleanup、费用报告整合仍未接通；主 change 仍有 14 个 OpenSpec 任务未完成。

### 2026-10-20 公共部署 Runner 边界

用户批准的计划要求公共源码仓库不再直接调度本机 self-hosted Runner。PR [#1](https://github.com/shuaibilx/DecAgent/pull/1) 已通过 Build/container、Python 3.11、Python 3.13 三项检查并合并至 `main`，merge commit 为 `0c747a396a360cc168d62c261f5d3dcd329cfb82`。变更只删除 release workflow 的 `deploy-local` job；CI 与 GitHub-hosted GHCR publish 保留。GitHub Actions API 显示仓库 Runner 数量为 0，本机 Windows 服务与 Runner.Listener/Worker 进程也为 0。现有 v0.1.4 容器未更改。此项只完成公共 Runner 隔离边界；可信本机 CD 将迁入私有部署仓库并在后续单独实施。

τ 运行时补充：修正用户模拟器一个回复包含多个工具调用时的对话语义，整批工具先在同一个权威环境中执行，作为单个 MultiToolMessage 一次交回模拟器，并保留批次轨迹。公开会话与隔离 τ worker 回归 `39 passed`（固定 Python 3.12、脚本化模型回调）；这修复评测适配保真度，不是四域 benchmark 结果，也未发送外部模型请求。

### 2026-10-20 首波实施续进

τ `parent_call` 的剩余时间现会传入 timeout-aware callback，并继续贯穿用量记录与受账本保护的 provider callback；对 profile、route、batch 与 τ bridge deadline 取最小值。定向回归 39 passed；修改后完整确定性测试 `1817 passed, 14 skipped`，全仓 Ruff 与 OpenSpec strict（6 passed）通过。Integration 为 9 passed、619 skipped，因未设置专用测试数据库而未执行数据库类用例。本次 first-wave dry-run 镜像预检通过，固定 4 个任务/8 条路线，无 ledger、模型请求或 MCP 请求。真实任务仍 0/286，统一 route supervisor、四域官方评分/清理闭环与 `--execute` 放行条件仍未完成。

### 2026-10-20 Agent 计时起点修正

machine-time ledger 增加 nullable `agent_started_at_utc`，兼容迁移不改变既有 schema/cohort 身份。隔离环境与 τ 初始模拟完成后，`ControlledRoutes` 通过 `on_agent_start` hook 在进入 Agent 回路前触发持久化起点；只允许 dispatched reservation 首次锚定 deadline，环境准备仍计批次墙钟、不计入每路线 Agent 机器时间。资源无关的测试环境明确声明 cleanup verified 后，完整确定性套件为 `1822 passed, 14 skipped (257.77s)`，全仓 Ruff 通过。本切片只证明 ledger deadline 与 Agent 起点的接口契约；统一 supervisor 调度、任意同步阻塞回调/工具的强制取消、官方评分与 cleanup 的真实四域闭环仍未完成，live dispatch 保持关闭，公开基准仍为 0/286。

### 2026-10-20 单路线监督执行原语

新增 `decagent/evaluation/business/public_execution.py`，提供只允许首波 ReAct/Plan 的 per-route 受控入口。它在 claim 前强制 canonical supervisor OS lock、canonical smoke ledger 和同一账本绑定的模型/工具 adapters；对照结果使用官方分数与 cleanup evidence 的摘要持久化。机时超限会阻止 route completed，同时仍保存可核验的评分和清理状态。8 项路线入口确定性测试及 2 项机时 ledger 测试通过，全仓 Ruff 通过。此代码没有 provider client factory、四域 session factory、批次 scheduler、恢复 CLI 或强取消；本阶段不因此开启 `--execute`，正式公开任务仍 0/286。

该入口随后修正了 route claim 与 machine-time reservation 的事务边界：`start_route_with_machine_time()` 在同一个 SQLite 写事务中检查批次/路线剩余额度、领取 pending route 并预留机时；任何准入失败都会整体回滚，route 仍保持 pending，不能出现已领取但没有机时 reservation 的半状态。两项原子性用例和入口回归共 10 项通过。修正后完整确定性测试为 `1833 passed, 14 skipped (269.32s)`，`uv run --extra dev ruff check decagent evals` 通过，`openspec validate --changes --strict` 为 6 passed、0 failed，`git diff --check` 通过（仅有 Windows 换行转换提示）。这仍只是单路线入口和离线证据；批次调度、真实 provider/session factories、任意同步阻塞调用的强制取消、四域官方评分与清理闭环未完成，live dispatch 保持关闭，正式公开任务仍为 0/286。

### 2026-10-20 首波 phase 调度进度

新增 `execute_public_phase()`，只支持冻结的 `first-wave` smoke phase；它在规范 supervisor/ledger 下按 CSM、ITSM、τ retail、τ telecom 以及 ReAct、Plan 的固定顺序遍历 8 个槽，只派发 pending route，跳过已终态路线，并在持久 batch stop 时不加载环境或构造 route callback。未知路线不会重放，当前路线 unknown 后仍可继续其他 pending 路线。调度器定向回归 `12 passed`，其中真实 SQLite ledger 关闭并恢复后 8 条已完成路线均未重放；实现后的全量确定性套件 `1837 passed, 14 skipped (268.23s)`，全仓 Ruff 通过、OpenSpec strict 为 `6 passed, 0 failed`、`git diff --check` 通过。live provider/session factories、phase CLI/完整恢复体验、remaining-smoke/full live 调度、强取消和四域真实闭环仍未完成；没有新增 Qwen、DeepSeek 或 Jev 请求，正式公开 benchmark 仍为 `0/286`，`--execute` 继续关闭。

### 2026-10-20 Route 与工具 deadline 接线

持久 route/batch 剩余时间现传入 EnterpriseOps HTTPX 请求、τ MCP 读取和 τ worker 子进程 bridge；超时或派发后其他不确定异常都按 unknown 处理，禁止该路线重放、继续工具调用或评分。定向回归为 `191 passed, 11 skipped`。此项没有实现通用的任意 Python 同步 callback 强制中断，也没有接通 live provider/session factories 与完整四域官方评分/清理；真实公开 benchmark 仍为 `0/286`，`--execute` 继续 fail closed。

计时复核发现 dispatch 至 Agent-start 之间的环境准备可能提前消耗 per-route machine-time。现已改为在 Agent-start 持久化之前返回完整 route 预留额度，同时仍受 batch deadline 限制；真实 Agent 回路开始后才按 deadline 扣减该额度。ledger/route/scheduler 定向回归 `129 passed`；完整确定性套件正在依据这项修正重跑。该边界修复不构成 live route 或模型评测成绩。

该计时修正后的完整 `evals/deterministic` 结果为 `1850 passed, 14 skipped (286.53s)`；全仓 Ruff、OpenSpec strict（6 changes）和 `git diff --check` 通过。14 个 skip 是现有可选/环境依赖项，不转述为通过的 PostgreSQL 集成。尚未运行真实首波任务或新增模型请求，公开 benchmark 仍 `0/286`，`--execute` 仍关闭。

补跑的一次性 PostgreSQL 17 集成 `evals/integration/test_business_route_execution.py` 为 `2 passed (21.20s)`，测试后已清理本次专用容器；它覆盖应用级 PlanRuntime 与 loopback MCP，不是公开基准 provider/scorer 闭环。其他现有容器保持运行。

### 2026-10-20 τ step 计数边界接通（仍为离线工程验证）

按冻结 tau2 `96a3ef9` 的半双工 role handoff 语义，新增持久 `tau_step_attempts` 与独立 200 步上限；计数在相应 Agent/User/Environment 调用前预留，用户 multi-tool 批次只算一个 Environment step。Plan 的外部节点（含语义 `decide`）作为 Agent action 单独计步；模型请求、MCP 调用和 Plan 控制步保持独立。账本恢复不重置，unknown route 不重放，回执读取已持久步数；上限阻止后续 route simulation dispatch，官方 judge 仍依独立额度处理允许的已结束轨迹。

该计数子项当时没有覆盖路线闭环；随后新增的 τ train工程路线验收已完成 9.6.C，详见下节。无 Qwen、DeepSeek、Jev 或公开 benchmark route 请求；公开任务仍 `0/286`，真实首波仍未执行，`--execute` 保持 fail closed。最新主 change 为 `65/78`，9.6.F/G 与 10.10.D/E 仍未完成。

### 2026-10-20 τ retail train 集成闭环证据

固定 τ2/Python 3.12 worker 的 train 工程 smoke 最初通过三条路线：retail ReAct、telecom ReAct、retail Plan；后续复核补上 telecom Plan，详见当前快照和 `tau-plan-cross-domain-smoke-20261020.json`。各路线均使用隔离 τ 环境、原始 `evaluate_simulation` 和清理验证，所有模型 callback 为脚本注入，没有 Qwen、DeepSeek 或 Jev 请求。测试同时发现 telecom 原始 case ID 长达 213 字符而评测作用域上限为 160，已将 case ID 的独立硬上限调整为 256，并加边界回归。τ 进程桥与评测目录首次完整定向套件 `45 passed (100.52s)`；该工程证据不代表 official test score、真实模型质量或首波成绩。四域故障注入仍由后续任务覆盖。证据见 `evals/benchmarks/evidence/tau-step-accounting-20261020.json`；主 change 当时为 `65/78`，公开任务仍 `0/286`，`--execute` 仍 fail closed。

### 2026-10-20 CSM 语义模板接入进度

评测专用 `csm_knowledge_article@1.0.0-csm-knowledge-1` 现已从离线构造器接入隔离评测 registry、Plan 编译器、`ControlledRoutes` 与生产 `PlanRuntime`；只接受同一 `retrieve_knowledge` 节点实际返回的已发布文章。受限只读 schema override 仅作用于匹配 pinned EnterpriseOps 工具契约的隔离目录，不扩展生产注册路径。真实容器工具目录验证无业务/模型调用，scripted route 和 registry/编译器单测通过；真实 Jev/Qwen 同状态配对、公开任务分数与模型收益尚未测量，9.6.D 仍待完成，公开基准仍 `0/286`。

### 2026-10-21 当前 apply 状态更正

此前首波 live 入口在候选 `9bc3ee6654e46630c9ae8238a28eb6e34a088698` 的 CI `39442012550` 通过后确实运行了 4 个任务、8 条路线；结果为 4 failed、4 unknown、0 个已评分成功路线，全部 cleanup verified，官方评分未调用。v7 ledger 没有 candidate SHA 元数据，不能安全把该批结果归到后续候选。本地已加不可变候选绑定，并拒绝为含历史 attempt 的旧 ledger 补写 SHA；因此不能用它重跑或追加任务。公开 benchmark 的可报告成绩仍为 `0/286`。任何新真实批次必须先有新 cohort、明确预算和同 SHA CI 证明；历史 unknown 不会重放。

