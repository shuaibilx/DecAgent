# Design

## Context

动机见 proposal.md。代码位于同级 DecAgent，当前入口及 task/project 数据约束均面向修复；主模型绑定全局修复工具，决策类型仅 Choice/Noul。PostgreSQL、预算账本、任务租约、事件、审批与 LangGraph checkpoint 已存在。正式规格库为空，本次业务规格不替换历史 change。代码目录不是 Git checkout，不初始化或推送仓库。

## Goals / Non-Goals

**Goals:** 业务与修复独立运行；能力契约可冻结；判断、治理、执行和结果核实可分别检查；默认产品可用于本地工单业务，并以公平对照证明或否定加速。

**Non-Goals:** 生产公司后台接入、陌生插件执行、多用户身份、实际修复企业设备、自动业务长期记忆、独立 Linux 验收。

## Decisions

### 1. 插件采用版本化 JSON 契约

新增 decagent.business 下的 contracts、registry、runner 模块。清单包含 id/version/level/entrypoint/commands/skill；每条命令包含 input_schema、effect、confirmation、result_schema 和可选资源版本及回执能力。JSON Schema 支持对象、数组、枚举、布尔与数值，不沿用修复工具的有限字段表。registry 是模型工具及运行校验的唯一来源。

用户注册已安装可信入口；注册时保存清单摘要，任务冻结摘要。修改或禁用会阻止旧任务继续执行。Skill 作为任务资料读取，无执行授权效力。基本契约可直接承接第三方 CLI，但不假定仅凭命令帮助就能得到完整工作流。

runner 使用 subprocess 参数数组与 shell=False，清理环境，仅传入清单允许的业务配置，阻止模型密钥、GitHub 密钥和 owner token 默认继承；限制时间、输出及进程树。用户批准的是可信插件代码，不能据此声称支持任意不可信代码的隔离。

### 2. 任务基础设施保留，新增业务作用域

通过 005 新迁移增加 app.business_scopes 与插件记录，app.tasks 增加 kind/business_scope_id；旧任务默认 repository_repair。放宽 project_id/base_sha 的统一非空限制后，增加按 kind 检查：修复仍要求仓库与 SHA，业务要求作用域且不伪造仓库。事件增加单 task 外键保障业务事件关联。阶段扩展业务阶段；actions、approvals、usage 与 checkpoints 共用已有 task ID。

修复 Worker 仅领取修复类型。业务 Worker 使用独立 graph version 与工厂，共用租约、heartbeat、预算和 checkpointer；兼容性按任务类型判断，不能因为新增业务版本让旧修复任务整体失效。主模型延迟到实际规划时创建，业务路径不构造 GitHub 或 broker。

新接口：GET /api/v1/plugins，POST /api/v1/business/tasks。创建输入 scope_id、instruction、budget、strategy（hybrid/main/rule，默认 hybrid）；插件版本和 provenance 来自程序。现有 tasks 查询、事件、取消、恢复、approvals 回复及 responses 按任务类型分派，保持旧修复行为。管理 CLI 提供插件 inspect/register/disable 和业务任务提交。

### 3. 独立工单业务作为增强插件

工单数据与业务回执保存在 PostgreSQL 独立 business schema。新增可信 JSON CLI，可独立从终端运行；Agent 只通过该 CLI 调用业务能力。CLI 收发 JSON，回执包含 operation_id、输入摘要、结果、资源 ID 与 revision。幂等键、业务写入、历史及回执在同一事务提交。

初始合成目录包含人员 ID、姓名、团队及服务分类；重名保留。提供 directory、list/get、create、assign、start、comment、resolve、close、reopen、set_priority 和 receipt 命令。状态迁移为 open→assigned→in_progress→resolved→closed；resolved/closed 可重开为 open。resolve 必须有用户提供的解决记录。写入拒绝过期 expected_revision。

明确请求的 create/首次 assign/start/comment 可自动执行；reassign/resolve/close/reopen/已有 priority 变更需要具体确认。查询不需要确认。目录人员是业务实体，不表示登录权限或多用户身份。

### 4. Jev 判断与规划分工

增强流程的合法动作、类别与参数枚举由契约定义。人员与工单候选来自带 ID/revision 的查询，不能让 LLM 凭空生成。Choice 保留 no_match；Noul 判断指定负责人、多人影响等独立命题；Score 以有序情境描述评估阻碍程度：0 未影响工作、1 可绕过、2 工作受阻。加权分数按 [0,0.5)、[0.5,1.5)、[1.5,2] 映射 normal/high/urgent 新工单优先级建议，不授权修改已有工单。

扩展已有 typed contracts、持久化和响应校验支持 Score，并保留旧记录读取。业务问题与候选按请求构造，版本和候选来源固定；独立问题在相同状态下批量调用。需要先获取人员候选的判断才进入第二次请求。文本自由参数从用户原文、确定性提取或主模型取得，Jev 不生成开放式字符串。

业务快速路线只使用与 actual model、问题、契约、数据集和策略匹配的校准。新增 synthetic-reference 数据源与现有人类标注分开；开发集拟合阈值选择零已观察分类错误条件下的最大覆盖，固定验收集不可用于调阈值。未校准或低确定性转主模型；缺失用户事实时澄清；服务故障有限降级。业务策略不改变旧修复默认 review 策略。

主模型获得同一契约和最新事实，可提出注册动作及内部 request_user/finish 信号；计划、参数和完成断言均由程序核实。使用 LangGraph prepare→decide/plan→validate→human→execute→verify 的有界循环，checkpoint thread_id 为任务 ID，人工节点前后的写入采用稳定 logical_key，防止重放副作用。finish 必须引用已核实回执，创建并分派任务无需关闭工单。

完成声明按 `W ⊆ C ⊆ O` 核实：O 为本任务全部已核实操作，W 为其中已执行写入，C 为模型声明的完成 ID。辅助只读查询可以不列入 C，写入和陌生 ID 不允许漏报或混入；最终核实仍使用完整 observations 及持久回执。现有确定性目标和 literal 命令均不能证明注册目标时，返回 `goal_unproven`，保留证据并终止为 failed，不把模型文本当作证明，也不自动启动新的写入。真实固定对照期间发现的运行时缺陷待当前三轮完整结束后修复；修复前实测原样保留，新增离线、PostgreSQL和有限真实探测分别记录，不能把旧完整对照宣称为修复后性能证明。

### 5. 确认和恢复

动作摘要绑定 task/scope/plugin digest/command/params/resource revision/policy version。资源变化后创建新动作及确认，不消费旧批准。写入结果未知先 receipt 查询，再核实状态；无回执能力的外部插件未知写入直接请求人处理。重复回复返回已有结果，取消终止受控进程并保留已发生的业务变更。

默认网页为业务界面，修复页为显式入口；显示插件、自然语言任务、工单、动作、等待、原始判断与最终路线。使用现有 owner auth/CSRF/幂等请求约束，不另设弱认证入口。

### 6. 评测和文档

默认 120 合成案例：40 开发，80 固定验收（48 常见、16 复杂、16 边界），交错三轮，缓存关闭。三策略获得同样规则、Skill、实体数据和执行约束；失败进入分母。真实机耗时包含模型、CLI 和核实，等待用户单独报告；模拟模型只证明编排行为。接受条件见 business-evaluation，不预设提升比例。

业务评分版本 `business-outcome-2` 按目标动作有序匹配所需参数，并容许契约标记 effect=read 的补充核实；目标读取仍须实际发生，写入顺序和数量不能增加。最终资源状态、单一结果资源及禁止写入尝试继续核实。澄清可先查询，但不能尝试写入。此前将全部动作逐项相等的评分缺陷保留原始部分报告并单独记录停止原因；修复后的完整对照另存，不拼接、覆盖或宣称旧报告通过。

真实费用沿用已知累计 Jev 费用 $0.000253176 与 $1 上限，后续从真实 usage 追加；未知费用不当作零。千问允许既有免费额度。首次评测先做有限探测，确认回执、账本与校准格式，再继续完整评测。无足够预算或服务不可用明确保存部分报告。

主模型用量与提案可执行性分别核实。完整响应或正常结束流的提案被拒绝时，专用安全异常仅携带固定拒绝原因、严格合法的用量及原样安全的实际模型名，预算包装器按既有价格结算后重新抛出；不保存原始响应、工具参数或原异常正文，也不接受任何部分提案。未知价格只保留已知 tokens、费用仍未知；传输中断、非法或缺失用量仍未知。可信派发前预检可证明调用未发生时结算零，不把一般 ValueError 当作未派发证据。业务失败记录保留合法用量与实际模型归属，旧修复记录 rejected 并维持等待及重启不重发。主模型诊断保存固定失败原因及可核实用量和费用，可信未派发记录零用量；相同 operation ID 返回已有证据，不重新调用模型。此修复在固定运行结束后实施，不回填历史未知用量，也不改变已冻结实测结论。

开发采集协议 `business-calibration-export-2` 在固定验收前冻结：40 条开发案例每例仅采集一次、关闭客户端缓存，不通过反复重采筛出成功答案。仅费用已知、tokens 完整、HTTP 2xx、实际模型与请求绑定完整的 `invalid_response` 可作为失败记录继续采集；无答案参与阈值拟合，且该请求涉及的问题 family 全部禁用快路线。其他服务故障、未知 usage、预算不足或版本绑定异常仍立即停止。导出分别公开 collection_complete、有效/无效数量和禁用 family；40 条收集完成不等于 40 条有效判断，更不等于提效通过。v1 导出保留原有全有效要求，旧部分证据不可改写成完整采集。固定 720 trial 继续统计失败和降级成本，验收集不用于调整本协议或阈值。

新生成指南、架构说明、图册和验收证据按北京时间 generated/YYMMDD/HHmm_主题 命名；README 与旧指南更新入口和兼容说明。提供 Mermaid 流程/泳道/时序和本地 HTML 图册；图册可离线阅读。

兼容 profile 中当前请求没有任何启用的判断 family 时，运行时记录 `no_enabled_calibrated_questions` 并直接转主模型，不发送注定不可采用的 Jev 请求；存在任何启用 family 时仍使用原有批量判断与实际模型版本校验。

## Risks / Trade-offs

- [合成校准外推不足] → 限定本地合成业务 profile，真实企业接入重新标注与验收。
- [Jev 或 CLI 调用开销抵消收益] → 记录端到端耗时与降级率，未通过就不宣布提效。
- [第三方插件缺少版本或回执] → 基础接入不伪造可靠性，未知写入暂停。
- [共享任务表影响旧恢复] → 新迁移、按 kind 分派、旧 API 与旧 checkpoint 回归。

## Migration Plan

先备份本机数据库与运行数据，停止写入任务，再新增迁移与安装包。注册 builtin 工单插件及合成目录，启动服务和串行 worker，验证业务路径与旧修复查询。失败时关闭业务入口并保留数据，通过已备份实例恢复旧应用；不得对含新业务任务的数据库直接运行旧 schema 应用。独立 Linux 验收保留待办。
