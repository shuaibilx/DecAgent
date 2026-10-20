# Design

## Context

动机见 proposal。当前 `EmployeeIdentity` 无 workspace，BusinessScope 是全局插件注册而不是租户；人员/工单等表共享，supervisor 可查询全库，记忆强制 local-company。现有员工 Cookie、CSRF、MCP 员工凭证、prepare/确认/commit 和原子任务预算可复用。当前 Compose 默认 repair Worker；release 在公共仓库调度本机 Runner，失败没有自动回滚，不能直接开放游客。

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

复用业务工作台的任务/操作/人工待办接口；游客入口验证会话、当前角色和空间有效期后进入相同治理链。角色切换撤销旧活动员工会话/凭证，清理 UI 详情，不能改变已有任务的执行员工；切回原身份才能继续其待办。无活动 7 天过期，活动只由本人有效请求更新，不被后台轮询任务永久续期。清空/过期立即封禁新准入并撤销会话，标记任务取消；清理器等待租约退出，结果未知的操作先核实，保留最小受限核实材料直到终结，再级联清理。仅聚合匿名资源统计可保留，不保留旧业务原文。

### 3. 无校准策略的明确人工消费

仅开放已注册模拟业务和受验证流程模板。沿用无有效策略转人工的现有机制，不伪造 threshold=0 或 confidence=1 让演示自动通过。Jev 原建议、primitive、选项/评分定义与证据显示为建议；游客选择、补充或拒绝后继续原节点；随后写入另走独立确认。未就绪主模型不以脚本伪装线上规划结果。

### 4. 三层额度与持久准入

独立 trial 账本采用北京时间自然日 bucket；全站 $5/5,000,000 Qwen token，每游客 30 任务/500,000 token。入队时原子占用任务额度，满队列拒绝不占额度；实际失败任务计已准入次数，避免失败刷请求。每次模型调用预留任务+游客+全站的最大输入/输出 token 与费用；有版本价格或明确免费额度的已知用量可结算，没有可靠费用上界拒绝派发。免费额度仍计 token，unknown 的 token/费用预留不释放、不归零；旧日期记录保留。评测 $1 Jev ledger 独立，不从试用取款。

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
