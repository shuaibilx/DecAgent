# Design

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

持久准入记录约束游客并行1、全站2、排队20；按入队顺序领取，租约/CAS防止多 Worker 重领。取消排队释放排队槽，已发生模型用量不回退；进程重启不重置日额度。初始游客创建每 IP 每小时最多5次、任务提交每分钟10次、人工回复每分钟30次；响应429/Retry-After。代理地址只从显式受信代理解析，本地无代理直接使用连接地址。匿名 IP 不是个人身份；全站预算和有界排队为最后保护，公网开通前另验证入口防滥用。

### 5. SOP Compose 与私有部署控制面

新增 SOP 专用 Compose 配置，API/业务 Worker/业务 MCP 使用受信内部身份与条件计划开关，PostgreSQL 持久卷；API 默认127.0.0.1。旧 repair/executor 隔离保留兼容，不随游客启动开放。配置检查必须验证 Worker profile 和 MCP URL，健康检查不止 API liveness。

公共 DecAgent 的 push/PR 跑 CI，语义版本 tag 跑完整 release gate 并发布 GHCR digest。新私有仓库 `DecAgent-deploy` 仅容纳部署控制代码和验证器；本机 Runner 只注册到此库，关闭公共仓库直接本机部署入口。发布端使用仅目标私有仓库 Actions write 的细粒度凭证触发固定 workflow，凭证不进入PR；部署端用只读凭证独立查询公共仓库 tag/main ancestry、成功 CI/run attempt、release/SHA 与镜像来源/digest。拒绝任意仓库、脚本、镜像引用和本地路径输入；不 checkout 或执行 dispatch 指定的公共代码。私有库固定、经审核部署脚本才可操作Docker。

所有部署共享单一 concurrency lock。保存旧 digest/config/schema 和 pg_dump；兼容性预检、拉取、升级、API/Worker/MCP及隔离模拟业务探针通过后激活。业务探针使用可清理专用空间、脚本模型、零付费，不能污染游客。失败先核验旧镜像支持当前 schema，兼容时回滚应用再验证；不兼容停止，不自动恢复数据库。保留备份路径和人工恢复步骤。用户已接受 Windows/Docker 登录、睡眠和网络引起短时中断，实测登录启动与重启恢复，不承诺开机未登录即在线。

### 6. CI 分层和文档

保持 lint/确定性、Python矩阵、PostgreSQL集成、核心浏览器E2E、wheel/MCP及Compose门禁。缓存按锁文件和镜像/平台区分；独立job并行，数据库测试使用独立库并限制并发，避免资源竞争。release复用同一完整检查入口，不把未经同SHA验证的其他运行当通过；保留每job与总墙钟以比较优化。无付费模型进入普通CI。

按 generated 日期子目录分别维护代码仓库/CI/CD/部署/游客/评测文档和导航；覆盖架构、泳道、时序和失败恢复图；所有命令由对应测试或本机演练核对。域名、公网地址和独立 Linux 保持待办，不输出虚构访问链接。

## Risks / Trade-offs

- 单公司隐含假设多 → 全表/接口归属审计和两空间负向测试，游客入口默认关闭直到通过。
- 游客无可靠个人身份 → 创建/调用限流与全站限额，丢 Cookie 无法恢复；不承诺防止所有分布式滥用。
- 日预算遇 unknown → 保留预留并停止该任务新模型请求，不把异常计零；其他任务受剩余全站额度约束。
- schema 回退限制 → 按兼容矩阵决定应用回滚，数据库恢复仅人工流程。
- 公共基准未完成 → 与游客功能验收分离，性能结论仍由原 change 管理。

## Migration Plan

1. 先完成/核验原执行器与评测门槛；未通过的真实评测保持待办，可继续本 change 的离线实现。
2. 追加 workspace 迁移，在隔离数据库验证旧身份/任务/审批/未知回执恢复，入口关闭。
3. 完成游客生命周期、ACL/MCP/记忆隔离、日额度/队列和浏览器核心链路，再启用本机游客入口。
4. 创建私有部署库与最小凭证配置，迁移 Runner；确认公共任务无法使用本机 Runner 后启用受信发布触发。
5. 发布新版本到本机SOP拓扑，演练兼容升级、失败回滚、不兼容停止和重启恢复；备份保留。
6. 分区文档同步验收。购买域名和公网开放另行实施；本轮不自动开通隧道或公开数据库/MCP。
