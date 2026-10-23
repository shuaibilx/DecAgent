# Design

### 2026-10-22 DashScope provider alias 预留修复

Qwen token 额度按模型服务语义计数。运行时和 `UsageLedger` 已把 `dashscope` provider 标识作为 Qwen 服务别名，但 trial ledger 的 reservation 校验未保持一致。现让准入校验接受 `qwen` 与 `dashscope`，并以参数化 PostgreSQL 集成测试覆盖两个 provider 标识的原子预留及本币结算。该兼容修复不改变费率配置和实际账单边界；游客 Jev 费用上界未证明，任务 3.2 继续保持未完成。

## Context

动机见 proposal。当前 `EmployeeIdentity` 无 workspace，BusinessScope 是全局插件注册而不是租户；人员/工单等表共享，supervisor 可查询全库，记忆强制 local-company。现有员工 Cookie、CSRF、MCP 员工凭证、prepare/确认/commit 和原子任务预算可复用。当前 Compose 默认 repair Worker；失败没有自动回滚，不能直接开放游客。公共 Runner dispatch 已暂停：远端仓库没有 self-hosted Runner 或部署 job，本机 Runner 计划任务禁用；公共 release 只发布 GHCR。后续 CD 必须先迁入私有部署仓库。

## Goals / Non-Goals

Goals：提供同浏览器恢复的游客模拟公司；身份贯穿执行、记忆及观测；限额免费体验；SOP 本机可信发布与恢复。

Non-Goals：真实公司 API、付费订阅、注册邮箱/GitHub 绑定、跨设备恢复、任意 Shell、游客 owner 权限、域名购买与实际公网开通、独立 Linux SLA。游客体验不作为公开 benchmark 或真实员工研究证据。

## Decisions

### 1. 可信 workspace 身份与向后兼容迁移

共享 PostgreSQL/Worker/MCP，不为每个游客创建容器或数据库。追加迁移引入 workspace，旧记录统一归属固定 legacy workspace。为员工、目录/工单、任务、操作/审批/回执、记忆等追加可信归属和必要复合关联约束，所有资源入口、列表、候选、trace/export、Worker claim 和 MCP 凭证重新验证归属；不能只在 UI 或任务列表过滤。BusinessScope 继续表示插件注册，不能冒充租户。旧任务/节点/operation/request_key 不变，迁移前后旧恢复回归必须通过。

数据库迁移 020 已建立 `business.workspaces`，legacy workspace 固定为 `00000000-0000-0000-0000-000000000001`；历史业务数据及现有 repository tasks 一次性归属到该空间。对员工、目录/工单、任务派生数据、操作/审批/回执、记忆、工作流与计划记录增加 workspace ID 及延迟复合外键。BusinessScope 插件目录保持全局共享，workspace 不能由插件 scope 推导。迁移在隔离 PostgreSQL 验证可从旧 schema 升级、重复 migrate 安全、既有员工/任务/审批/记忆/operation/receipt 与 LangGraph checkpoint 原标识和状态不变，并拒绝跨空间工单分类引用。备份还原识别并替换空库初始化时自动生成的 legacy workspace 行。内部 `workspace_id` 不进入业务 MCP 对外返回对象。兼容默认值只服务旧调用路径；游客入口仍关闭，后续身份和数据访问实现必须显式使用认证解析出的 workspace。

使用 workspace-aware 数据访问方法统一注入归属条件，复合数据库约束阻止跨空间关联；全局清理/调度仅供内部服务使用。员工 login/person ID 由服务端空间命名避免全局 seed 碰撞。迁移 022 从员工、证据来源与回执恢复可信归属后删除固定 `company_scope`；`MemoryContext` 从认证身份取得 workspace，写入、证据验证、候选与撤销均同时校验 workspace 和员工。经验规则的键改为 `(rule_id, rule_version, workspace_id)`，相同发布版本可在不同公司独立校验；跨空间来源不能发布。旧记忆保持内容、期限与状态，运行时以新 workspace context 恢复原员工稳定槽，重复提议按结构化来源/版本兼容去重。稳定记忆槽仍按当前员工和已发布流程解析，API 返回不暴露 workspace ID。共享发布模板只能含无个人数据的参数槽和版本，不共享任务实例或员工记忆。

任务子记录的写入边界再由 PostgreSQL 保护：事件、操作、审批、执行尝试、用量、计划、决策干预和工作流记录在插入时，从可信父任务或父操作派生 workspace；调用方遗漏旧默认值或传入不一致值，都不能改变归属。业务 API、Worker 恢复和 trace/export 仍显式按员工、任务及 workspace 授权/过滤；数据库触发器不替代入口授权。

### 2. 游客会话、角色与空间生命周期

新增 `/api/v3/guest/session` POST/GET 创建/恢复游客身份，`POST /api/v3/guest/role` 切换本空间预置员工，`DELETE /api/v3/guest/workspace` 清空并撤销空间。服务端生成高熵会话、只保存 hash，Cookie HttpOnly/SameSite、公开 HTTPS Secure，变更请求 CSRF/Origin；本地 HTTP 仅 loopback。不得从客户端 body 或不可信 header 读取执行 workspace。保留原 owner/员工入口，不把游客映射 owner。

当前 session 创建会在同一 PostgreSQL 事务内写入 guest workspace、6 个人员、5 个类别、6 个随机无密码账号、guest session 和配套的 employee session。配套记录让既有工作台 API/operation/MCP 权限继续使用统一的员工会话校验；guest Cookie 独立于员工登录 Cookie，只从 guest session 解析当前身份。migration 023 仅保存 session/CSRF hash，并通过复合外键绑定 workspace 与当前模拟员工。公开只读 `/api/v3/guest/status` 让登录页获知入口开关，不创建会话或 workspace；关闭时隐藏游客入口。session 为 HttpOnly Cookie，CSRF 使用独立同源 Cookie 并由 UI 放入 mutation header；两者为 SameSite Strict。角色切换只接受固定 `role_key`，在同一事务更新两张 session 记录的员工/版本/CSRF hash 并轮换 CSRF；API 返回当前角色 key，UI 丢弃原角色任务、工单、操作、审批、计划、trace、记忆详情和事件流，再按新身份刷新。游客退出会撤销两张 session 并清除 Cookie。既有任务和待办保持原员工归属，切回原角色后仍由原员工继续访问；隔离 PostgreSQL 测试验证旧员工无法提交待办确认且审批保持 pending。`DECAGENT_GUEST_TRIAL_ENABLED` 默认 false，直到配额、生命周期清理、UI 和本机交付验收完成才可显式开启。Migration 024 增加 `last_activity_at`，游客会话采用 7 天无活动窗口；仅成功的、带有效身份的非被动请求续期。session restore、status、HEAD/OPTIONS 和 EventSource 不续期，因此后台轮询不能延长生命周期。过期后游客入口、员工会话回退、MCP token 认证、MCP 凭证签发和 Worker 凭证解析均拒绝该身份。带 CSRF 与精确 Origin 的 workspace DELETE 立刻停用 workspace、撤销游客/员工会话及 MCP 凭证、禁用模拟账号；`EmployeeAuth` 的浏览器、MCP token 与 Worker 凭证解析均要求 workspace active；MCP/Worker 凭证还要求 guest inactivity session 仍有效。若清空响应丢失，同一原 token/CSRF/Origin 对已停用空间的 DELETE 可幂等重试；不会恢复访问或读取数据。Migration 025 增加 `cleanup_completed_at` 与清空重试 tombstone；过期/清空先撤销访问和新准入，再由 Gateway 立即尝试、Worker 有界轮询继续清理。Worker claim 只选择 active workspace，并在完整 graph/checkpoint 执行期间持有 task advisory lock；心跳续租同时验证 workspace 仍 active。Cleaner 持 workspace 锁、逐任务/操作取得共享 advisory lock 后复核租约与记录集合；在途执行返回 pending，避免晚到 checkpoint 被误删后又由 Worker 复活。清理器核对 operation receipt：partial/mismatch 和没有 verified receipt 的成功操作保留数据并等待恢复处理；安全条件满足后 fence/cancel 未终态任务，删除 task checkpoint 和 workspace 所属业务行，事务延迟检查空间内循环外键，只留下 inactive workspace 清理标记与最小 token/CSRF 哈希 tombstone。重复清理/clear 可幂等完成，不执行未知操作。隔离 PostgreSQL 覆盖 Worker 晚到写入、跨进程锁、租约、receipt 歧义/核验、过期和清理重启；无模型调用。

复用业务工作台的任务/操作/人工待办接口；游客入口验证会话、当前角色和空间有效期后进入相同治理链。角色切换撤销旧活动员工会话/凭证，清理 UI 详情，不能改变已有任务的执行员工；切回原身份才能继续其待办。无活动 7 天过期，活动只由本人有效请求更新，不被后台轮询任务永久续期。清空已立即停用 workspace、撤销 session/MCP 凭证并禁用模拟账号；当前过期身份拒绝访问。生命周期清理使用 workspace/task/operation 锁、active-workspace claim 和 lease/receipt 复核；在无法证明安全时保留业务记录与恢复材料、返回 pending，不通过重放操作验证结果。清理完成后删除空间业务数据及检查点，仅保留最小 clear tombstone 支持丢失响应重试，不恢复或重放任何操作。

### 3. 无校准策略的明确人工消费

仅开放已注册模拟业务和受验证流程模板。沿用无有效策略转人工的现有机制，不伪造 threshold=0 或 confidence=1 让演示自动通过。Jev 原建议、primitive、选项/评分定义与证据显示为建议；游客选择、补充或拒绝后继续原节点；随后写入另走独立确认。未就绪主模型不以脚本伪装线上规划结果。

### 4. 三层额度与持久准入

独立 trial 账本采用北京时间自然日 bucket；全站每日费用上限为 $5 美元等值抵扣前 API 费用，另限 5,000,000 Qwen token；每游客每日 30 个任务/500,000 Qwen token。所有供应商费用按抵扣前 API 费用计入预算，代金券只改变券后实付，不降低模型资源消耗或预算记账值。对人民币费用，受信部署配置提供 CFETS 官方 USD/CNY 汇率中间价、来源日期及 URL；运行时不抓取网页，且每个北京时间预算日第一次初始化时将快照持久化，此后同一天不可更改。配置报价必须来自该日开始前最新已公布工作日报价，最多允许旧 7 个日历日，超期时停止 CNY 派发并要求更新。首版保守换算使用原汇率的 90%，向下截断至 0.01 CNY/USD，以避免把人民币费用低估为美元费用。可核验示例：[CFETS 官方页面](https://www.chinamoney.com.cn/english/)于 2026-10-20 显示 6.7200，应用 10% 安全余量后冻结为 6.04 CNY/USD，人民币账单以 `CNY / 6.04` 折为美元等值；$5 对应的 Qwen 单币种预算上界为 ¥30.20。额度预留用十进制运算并向上取整，不用浮点数；缺少可核验汇率/费率或不能构造可靠费用上界时拒绝派发。Qwen 5,000,000 token 上限独立于费用上限；即使免费或使用代金券，也照计 token 与抵扣前 API 费用。评测 `$1` Jev ledger 保持独立，不从游客试用预算扣除。

持久准入记录约束游客并行1、全站2、排队20。Migration 028 将 `queue_state`、入队时间和最近领取时间放进每日 task admission ledger；任务状态触发器同步 `queued/running/awaiting/terminal` 与 `waiting/running/paused/terminal`，暂停后重新入队时排到队尾，取消立即释放等待名额。任务创建、全局队列容量检查、每日任务计数和入队记录共享同一 API 数据库事务；满队列返回429且回滚任务、凭证和每日计数。Worker 以 PostgreSQL advisory transaction lock 串行计算 guest active lease 和领取任务，`FOR UPDATE SKIP LOCKED` 只领取最早的可运行项；同一 workspace 有有效运行租约时跳过其后任务，其他 workspace 可以继续。租约过期后只对同一 task 做 generation fencing，不重置队列或账本；状态/等待位置/等待原因由任务 detail/list 返回。全站排队20指等待项，不含当前最多2个执行项。隔离 PostgreSQL 覆盖满队列、取消、FIFO可运行公平性、1/2并发约束、租约回收与旧代次拒绝；用户模型调用入口保持关闭。

Migration 029/030 与 `GuestAdmissionRateLimiter` 使用 PostgreSQL 滑动窗口，按可信客户端地址限制游客新建 5 次/小时、任务提交 10 次/分钟、人工回复 30 次/分钟。成功认证的现有游客 session 恢复或 CSRF 修复不占用新建额度。部署级随机 32 字节 HMAC key 从 secret/env 注入，所有 API 实例必须使用同一稳定值；数据库只保存 HMAC 地址摘要和独立用途的 HMAC key 指纹，不存原始 IP 或 HMAC key。游客开关开启时缺 key 会被配置校验拒绝。Key 需跨重启保持；变更会重置当前 IP 窗口，因此应先关闭游客准入并等待最近一条限流事件超过 2 小时。数据库指纹门禁在已有旧窗口时拒绝新 key，返回 503 和剩余等待时间，且不写入新限流事件；窗口过期后才登记新指纹。每次校验在事务内取得全局指纹锁和 scope+地址 advisory lock，因此多 worker、进程重启及清 Cookie 都共享同一限额。常规用户限额超限返回 429、`Retry-After` 和可读错误；轮换等待返回 503；队列满及日额度耗尽也向客户端提供有界重试提示。

`DECAGENT_GUEST_TRUSTED_PROXIES` 只接受显式 IP/CIDR。ASGI 代理中间件默认将 TCP 对端作为主体；仅当对端落入配置 CIDR 时，才从右向左解析 `X-Forwarded-For`，并接受单值 `X-Forwarded-Proto`。无效链退回代理对端地址；不可信代理头忽略。CLI 启动 Uvicorn 时关闭其默认代理头改写，避免 loopback 被隐式信任。反向代理必须覆盖或追加实际上游连接地址。客户端 IP 仅作为可伪名化的限流信号，不代表个人身份或账号。

游客工作台的额度卡片从只读 `/api/v3/guest/usage` 读取北京时间预算日、全站抵扣前费用与 Qwen token 占用、当前空间任务与 token 占用，并显示固定上限；游客任务详情展示执行状态、队列位置、并发/等待限制。`429` 的 `Retry-After` 以秒数展示给用户。数据包含未知调用预留，不将券后支付误作抵扣前费用。

#### 4.1 模型请求预留、结算与 unknown

业务模型共用既有 `UsageLedger` 与 provider wrapper。每次 Qwen/备用模型/Jev attempt 先在一个 PostgreSQL 事务里写任务级 `app.usage_records` 并写 trial attempt，二者通过 `usage_record_id` 关联；预算检查、费用预留、token 预留和记录写入同事务提交，拒绝时全部回滚。每个 provider attempt 有独立编号与预留；guest attempt 一旦 unknown，后续 retry/fallback 会被 unknown 门禁拒绝。

`TokenPrice` 明确价格币种及版本：Qwen/DashScope 默认 CNY，OpenRouter 备用模型默认 USD。Qwen 派发上界按实际请求输入上限与最大输出 token 计算；响应带齐输入/输出 token 后，使用冻结费率计算抵扣前本币费用，并以 `frozen_rate` 记录依据。OpenRouter Jev 响应若包含确定的 `usage.cost`，按 USD provider-billed 保存；只得到 token 或状态、不足以确认总费用时，保留整个预留。任务账本保存美元等值以应用既有单任务预算，trial 账本额外保存 native currency、费率版本、实际 token、成本依据、请求 ID、延迟与错误类别。Voucher/cash 不由差额推算；缺少供应商响应或 invoice 证明时保持空值。

`BusinessServices`、条件工作流和 memory-write evaluator 通过 task ledger factory 自动绑定 guest workspace 的 TrialBudgetPolicy；员工 workspace 继续使用原任务账本。guest flag 关闭、workspace 已停用、无版本费率或 CNY 汇率不可用时，在模型请求发送前失败关闭。任何已派发模型请求进入 unknown 后，保留原预算日费用和 token 预留并阻止游客任务后续模型请求，包含 wrapper 自动 retry/fallback；重启、跨日或换 workspace state 路径不释放该占用。可信本地预检可证明请求未发送时，task/trial 两个预留在同事务标为 not-sent 并释放。

`UsageLedger` 保留上游 provider 的原始错误类别和 HTTP 状态以供审计；调用 guest trial ledger 时，把 provider taxonomy 映射到受限的持久化枚举：connection→`network_error`，HTTP 408→`timeout`，429→`rate_limited`，其他已分类 HTTP 错误→`provider_error`，未分类传输异常→`unknown_error`。这样已派发但 usage 未知的请求仍可在同一事务中将任务级和 trial 级记录置为 unknown 并保留预留，不因错误类别不兼容而回滚结算。

Jev 当前的 `$0.01` 仅是固定记账预留，不是可证明的单请求费用上界；`jev-latest` 是可变模型别名，128 KB 状态字节限制也没有限制 provider 输出 token。OpenRouter 通用 Provider Selection 文档描述 `provider.max_price`，而 Decisions API 专属请求契约仅声明 `provider` 为通用 object/null，未明确列出 `max_price` 的字段结构或对 Decisions 端点的硬上限保证；该通用路由说明不足以证明游客请求有可执行总费用上界。因此 `UsageLedger.reserve()` 对 guest `model_family=jev` 在创建任何 task/trial 预留之前返回 `guest_jev_fee_bound_required`，`LedgerAttemptBudget` 将其作为预算拒绝，DecisionAdapter 不发送 HTTP 请求。员工 Jev 路径不受此 guest-only 门禁影响。只有冻结可核验的 provider 费率并强制输入/输出 token 上限、确保计算所得单次费用上界不超过预留后，才可开放游客 Jev。审查证据：[OpenRouter Decisions API 费用上界契约审查](../../../evals/integration/evidence/openrouter-decisions-cost-bound-20261022.md)。

### 5. SOP Compose 与私有部署控制面

新增 SOP 专用 Compose 配置，API/业务 Worker/业务 MCP 使用受信内部身份与条件计划开关，PostgreSQL 持久卷；API 默认127.0.0.1。旧 repair/executor 隔离保留兼容，不随游客启动开放。配置检查必须验证 Worker profile 和 MCP URL，健康检查不止 API liveness。

2026-10-22 实施记录：新增 `compose.sop.yaml`，将 PostgreSQL、迁移、业务 bootstrap 和业务 MCP 限制在 internal network；API/Worker 另接 model egress network，API 只把宿主机端口发布到 loopback。配置预检拒绝 repair executor 服务、Docker socket、公开 API bind、内部服务 host port、错误 Worker kind、条件计划关闭或不匹配的 MCP resource identity。一次性容器冒烟真实启动六个服务并验证 API/MCP 健康、业务 Worker 类型和游客关闭；使用空模型密钥，零模型请求，结束后清理容器/卷/镜像。原 repair `compose.yaml` 解析仍通过。27 项需专用 PostgreSQL 测试库的 MCP 集成测试因当前未配置 `DECAGENT_TEST_DATABASE_URL` 而跳过，不计通过。详见[本机容器冒烟证据](../../../evals/integration/evidence/sop-compose-smoke-20261022.md)。该实现完成 OpenSpec 4.1，不代表 4.2 业务探针或 4.3 回滚验收通过。

2026-10-22 实施记录（4.2）：API `/health` 只表示进程 liveness，新增 `/readyz` 检查数据库迁移版本、启用且摘要匹配的业务插件、内部 MCP 健康及 conditional workflow 开关；Worker 专用容器探针还校验其 PID 1 参数为 `worker --kind business`。宿主机 readiness 汇总六个 Compose 服务、迁移/bootstrap 结果、API、MCP 与 Worker 健康；Compose API 健康检查改用 `/readyz`。故障注入停止 Worker 时，API liveness 仍为 healthy 而业务 readiness 降级，恢复 Worker 后重新 ready。一次性 Compose 数据卷/网络/镜像和真实 MCP + ScriptedModel 集成测试均隔离清理，不调用供应商。修复客户端/服务器连接策略冲突：业务 MCP 客户端继续接受本机 loopback，并且只额外接受固定 `http://business-mcp:8001/mcp`；任意其他 DNS、外部 IP、端口、路径与认证信息均拒绝。详见[业务 readiness 与故障恢复证据](../../../evals/integration/evidence/sop-business-readiness-20261022.md)。OpenSpec 4.2 完成；备份/兼容回滚 4.3、Windows/Runner 恢复 4.4 与私有 CD 仍待完成。

2026-10-22 实施记录（4.3，部分实现）：新增 `ops/deploy/sop_rollout.py`，只接受 `ghcr.io/shuaibilx/decagent-control@sha256:...` 固定 digest；部署前核验当前 SOP readiness 并记录运行中镜像、Compose 配置指纹、数据库迁移版本和活跃 graph 版本。先停止 API、Worker 与业务 MCP，再在无业务写入的窗口中保存 `pg_dump`、`/data`、脱敏后的渲染 Compose JSON 与逐文件 SHA-256；备份失败时记录故障并尝试恢复旧版本。目录访问限于当前 Windows 用户，Linux 文件权限为 0700/0600。独占锁拒绝并发部署。候选镜像未 ready 或操作者按 Ctrl+C 中断时，会尝试恢复旧镜像；旧镜像因迁移/checksum/graph 不兼容时保持 API/Worker/MCP 停止、保留数据库及备份，绝不自动 `pg_restore` 覆盖当前数据库。定向 coordinator/preflight/readiness 测试 `33 passed`，完整 deterministic `2,075 passed, 18 skipped`；Ruff 与格式检查通过，SOP Compose 只读 preflight 通过。详见[回滚协调器实现记录](../../../evals/integration/evidence/sop-rollout-coordinator-20261022.md)。**4.3 尚未完成：** 公开最新 tag `v0.1.4` 不含当前 SOP Compose，尚无可用的发布镜像在独立 Docker 数据卷上演练可信 digest 升级、健康失败回滚和 schema 不兼容停服；因此不勾选 4.3，也不宣称生产回滚已经验收。

公共 DecAgent 的 push/PR 跑 CI，语义版本 tag 跑完整 release gate 并发布 GHCR digest。新私有仓库 `DecAgent-deploy` 仅容纳部署控制代码和验证器；本机 Runner 只注册到此库，关闭公共仓库直接本机部署入口。公开的 GHCR 镜像匿名拉取；公开发布 provenance 从公开 DecAgent API 匿名读取，不需要长期只读 PAT。自动发布触发器尚未接入：后续使用仅对 `DecAgent-deploy` 授予 Actions write 的 GitHub App 安装令牌调用固定 workflow_dispatch；令牌只能在 tag release 发布成功后的专用 job 中使用，不能进入 PR workflow。拒绝任意仓库、脚本、镜像引用和本地路径输入；不 checkout 或执行 dispatch 指定的公共代码。只有私有 workflow 先运行固定发布验证器再把其输出传给 rollout coordinator 的路径，构成发布来源验证边界。

私有部署的 workflow_dispatch 仅允许 `source_repository`、`tag`、`sha`、`run_id`、`run_attempt` 与 `image` 六个字符串输入；验证器固定来源仓库、语义版本格式、commit SHA、release workflow/run attempt、CI 与发布 job、main ancestry、GHCR SHA tag 对应 digest 及 OCI source/revision/version 标签。验证器只向后续固定 rollout coordinator 输出经过核验的不可变 digest，不接受脚本、命令、工作目录或仓库覆盖。公共镜像先从固定 `ghcr.io/shuaibilx/decagent-control:sha-<sha>` 拉取，仅读取 Docker inspect 元数据，不执行镜像代码。

2026-10-22 5.2 实施记录（部分实现）：新增标准库实现 `ops/deploy/release_verifier.py` 和私有 workflow 模板 `ops/deploy/private-deploy-workflow.yml`。离线测试验证注解 tag 解析、main 祖先比较、release run 的 tag/SHA/run attempt 绑定、CI/publish 成功状态、digest/OCI 标签、固定 Docker tag、最小 dispatch 输入、拒绝任意命令/脚本字段与私有 main 环境边界。workflow 只 checkout 固定 `main` 私有部署代码，运行受限 Windows Runner，并绑定 `decagent-local-production` environment。

2026-10-23 接线复核补充：`decagent-control` 是公开 GHCR package，Docker 支持匿名拉取；私有 workflow 仅授予 `contents: read`，不需要 `packages: read` 或 Manage Actions package grant。公开 provenance 查询使用无凭据的公开 API，避免为读取公开元数据保存长期 PAT。安装器和 workflow 统一使用本机固定项目根 `DECAGENT_LOCAL_PROJECT_DIR`、`ops/deploy/sop.env` 与 `.decagent/sop-secrets/{postgres_password,main_api_key,jev_api_key}`；不再误要求 repair broker/GitHub token。私有发布包仅含 reviewed Compose、标准库 verifier、rollout、preflight、business probe 和合同常量；预检可在不安装或检出 `decagent` 的私有目录中导入。部署协调器通过独立的 compose-file、env-file、backup/state path 参数，把私有控制代码与本机配置/备份根分开。私有 workflow 仍需最小发布触发身份和真实派发/部署验收；这不是已完成的 CD。

所有部署共享单一 concurrency lock。保存旧 digest/config/schema 和 pg_dump；兼容性预检、拉取、升级、API/Worker/MCP及隔离模拟业务探针通过后激活。业务探针使用可清理专用空间、脚本模型、零付费，不能污染游客。失败先核验旧镜像支持当前 schema，兼容时回滚应用再验证；不兼容停止，不自动恢复数据库。保留备份路径和人工恢复步骤。用户已接受 Windows/Docker 登录、睡眠和网络引起短时中断，实测登录启动与重启恢复，不承诺开机未登录即在线。

### 6. CI 分层和文档

保持 lint/确定性、Python矩阵、PostgreSQL集成、核心浏览器E2E、wheel/MCP及Compose门禁。缓存按锁文件和镜像/平台区分；独立job并行，数据库测试使用独立库并限制并发，避免资源竞争。release复用同一完整检查入口，不把未经同SHA验证的其他运行当通过；保留每job与总墙钟以比较优化。无付费模型进入普通CI。

按 generated 日期子目录分别维护代码仓库/CI/CD/部署/游客/评测文档和导航；覆盖架构、泳道、时序和失败恢复图；所有命令由对应测试或本机演练核对。域名、公网地址和独立 Linux 保持待办，不输出虚构访问链接。

## Risks / Trade-offs

- 单公司隐含假设多 → 全表/接口归属审计和两空间负向测试，游客入口默认关闭直到通过。
- 游客无可靠个人身份 → 创建/调用限流与全站限额，丢 Cookie 无法恢复；不承诺防止所有分布式滥用。
- 日预算遇 unknown → 保留预留并停止该任务新模型请求，不把异常计零；其他任务受剩余全站额度约束。
- schema 回退限制 → 按兼容矩阵决定应用回滚，数据库恢复仅人工流程。
- 部署备份包含数据库与 `/data` 业务内容；当前仅使用 Windows 用户 ACL/Linux 文件权限保护，未做应用层加密。保存真实敏感业务数据前，主机磁盘加密、备份保留期限和恢复权限需确认并记录。
- 公共基准未完成 → 与游客功能验收分离，性能结论仍由原 change 管理。

### 2026-10-23 Public-to-private Runner fail-closed hardening

The public repository still reports zero registered self-hosted runners; the legacy local scheduled task is disabled and no Runner process/service is active. GitHub returns 404 for the planned private `shuaibilx/DecAgent-deploy`, while two old local Runner directories remain configured for the public repository. Their credentials were neither read nor removed. The installer allows only the private deployment repository, checks that GitHub marks it private before creating local Runner state, and verifies any existing local Runner marker/ID against that repository's registered Runner list. It refuses old/partial directories and duplicate registrations rather than copying legacy state or using `--replace`. The startup script checks the fixed private repository URL and Runner name again before executing `run.cmd`.

Current 2026-10-23 review additionally found and fixed a private-copy import defect: `sop_preflight.py` depended on `decagent.business.mcp_contract`, which is unavailable when the private repo correctly does not checkout public app source. The reviewed endpoint constant now lives in the deployment contract, with a parity regression against the app contract. The workflow passes the persistent local project root separately from its private checkout, and installer preflight checks SOP-specific config/secret files. Selected deployment and boundary regressions pass (`78 passed`); no live workflow or Docker rollout ran. Current GitHub API checks report public Runner count `0`, private deployment repo `404`, and no local deployment scheduled task. Pushing candidate `fc96c7a` failed twice with a connection reset; GitHub does not yet contain that commit, so same-SHA remote CI is unverified. These source safeguards do not create the private repository, migrate the Runner, or complete task 5.3.

### 2026-10-23 Private repository and initial-install findings (historical snapshot)

The private repository `shuaibilx/DecAgent-deploy` now exists and is private, but currently contains only its initialization README. GitHub reports Actions enabled with the default `all` action policy, and both public and private Runner APIs report zero registered runners. No private workflow, deployment environment, branch policy, package Read grant, or local SOP configuration has been installed. The public release workflow remains GHCR-only and tag-triggered; it has no path to schedule the local machine.

The local deployment candidate is still not remotely verified: `fc96c7a` is two commits ahead of the remote feature branch, both Git pushes ended in connection reset, and GitHub has no run for that SHA. Do not treat prior CI for a different SHA as candidate verification.

Reviewing first deployment exposed an implementation gap: `sop_rollout.rollout()` requires an already-ready stack, so it can update an installed service but cannot provision a pristine host. The approved local-delivery scope implies a first install. The design requires a separate fail-closed bootstrap that checks for zero project containers/volumes/networks, persists the exact image digest and Compose fingerprint, permits only same-image/config recovery after interruption, and never deletes partially initialized database state. Once marked ready, the service can only use the backup and compatibility-aware upgrade path.

### 2026-10-23 Current implementation and deployment boundary

The first-install path is implemented by `first_install()` and `deploy_candidate()`. It records an immutable image digest and Compose fingerprint before provisioning, checks for existing containers and project-labelled volumes/networks, permits only same-digest/same-Compose/same-secret-content recovery, pulls images before starting the stack, and retains partial database state. File-backed secret content contributes only SHA-256 digests to the fingerprint; plaintext is not persisted. The Runner installer and deployment workflow require the project `.venv` interpreter to be Python 3.11 or newer; they do not trust global `python` on PATH. Offline tests do not replace real Docker first-install, recovery, upgrade, rollback, or private-dispatch validation (task 4.4/5.3/5.4).

The private repository `shuaibilx/DecAgent-deploy` is private. Its current main commit and bundle manifest are recorded in the linked boundary evidence. GitHub Actions is enabled, only pinned `actions/checkout` is allowed, SHA pinning is required, and the `decagent-local-production` Environment allows only `main`. `decagent-control` is public, so the private workflow uses anonymous GHCR pulls and does not request `packages: read` or a package access grant.

GitHub rejected repository branch protection with HTTP 403 because the current plan does not provide that feature for this private repository. The environment branch policy still restricts deployments to `main`, but repository writers can still change `main`; restrict write access to trusted maintainers. No Runner is registered, local SOP config/secrets are absent, and the App credentials plus public release enablement variable are not configured. The corresponding public release trigger remains in the local worktree and has not received same-SHA remote CI. See [private deployment boundary evidence](../../../evals/integration/evidence/private-deployment-boundary-20261023.md).

## Migration Plan

1. 先完成/核验原执行器与评测门槛；未通过的真实评测保持待办，可继续本 change 的离线实现。
2. 追加 workspace 迁移，在隔离数据库验证旧身份/任务/审批/未知回执恢复，入口关闭。
3. 完成游客生命周期、ACL/MCP/记忆隔离、日额度/队列/请求限流和浏览器核心链路，再启用本机游客入口。
4. 创建私有部署库与最小凭证配置，迁移 Runner；确认公共任务无法使用本机 Runner 后启用受信发布触发。
5. 发布新版本到本机SOP拓扑，演练兼容升级、失败回滚、不兼容停止和重启恢复；备份保留。
6. 分区文档同步验收。购买域名和公网开放另行实施；本轮不自动开通隧道或公开数据库/MCP。
