# Tasks

本 change 承接已批准游客与本机交付计划。公开评测首波仍未形成有效评分闭环，真实路线不重放；workspace 隔离、游客会话创建/恢复、角色切换、7 天无活动失效、空间停用与受限物理清理已完成。游客入口由 `DECAGENT_GUEST_TRIAL_ENABLED` 控制且默认关闭，公开域名/隧道不在本轮开通。清理遇在途 Worker 或含歧义的操作证据时保持 pending，不删除恢复材料。

## 1. Workspace 数据与身份边界

- [x] 1.1 追加 workspace/legacy 归属迁移及必要唯一/关联约束；隔离 PostgreSQL 验证旧员工、任务、审批、记忆、操作和 checkpoint 原身份不变，迁移重复执行安全。证据：`020_business_workspaces.sql` 为既有业务表添加 legacy workspace 和同空间复合外键；集成测试验证旧标识、receipt 与 checkpoint 不变、重复迁移可重跑、MCP 响应不泄漏内部 workspace 字段、跨空间工单分类引用被拒绝；另通过身份升级、备份还原、记忆、计划、服务和 MCP 回归 65 项。
- [x] 1.2 扩展可信员工身份与凭证的 workspace 解析，统一 workspace-aware 数据访问；测试拒绝客户端覆盖空间和跨空间关联，并保留旧员工登录/MCP 协议回归。证据：`EmployeeIdentity`、浏览器会话、MCP 凭证及任务恢复均从数据库绑定 workspace；工作台任务、操作、审批、审计和 MCP 访问使用同一空间；请求体伪造 workspace 返回 422，伪造身份不能使用其他空间的会话/凭证；隔离 PostgreSQL 的员工认证、操作、工单 ACL、MCP 与 Gateway 回归 80 项通过，新增 workspace 端到端切片测试通过。
- [x] 1.3 隔离目录、工单与分派候选，限制 supervisor 为本空间；两空间集成测试覆盖读取/列表/写入/候选和原子版本冲突。证据：目录、详情、历史、列表、分派候选、工单写入、操作/回执 SQL 均注入可信 workspace；双空间集成测试拒绝跨空间工单、回执和人员候选，仅列出本空间目录/工单；相同资源的过期 revision 在提交时失败且不产生新副作用。
- [x] 1.4 隔离任务、审批、操作、回执、trace/export 与 Worker/MCP 分派；两空间猜 ID、重放确认、切换员工和恢复测试全部拒绝越权。证据：新增 migration 021 从 task/action 派生事件、action、approval、attempt、usage、计划/决策/工作流子记录的 workspace，测试 legacy 默认及错误 legacy 值均被修正为父 task 空间；真实 Worker 凭证恢复保留任务员工/workspace，不匹配身份被拒；同空间 supervisor 与 legacy 员工猜 guest task ID、trace/export、事件流和重放 approval 均为 404，approval 保持 pending，游客本人 trace/plan/export 读取通过；现有 operation/receipt ACL 与 unknown 恢复回归保留。核心业务 PostgreSQL/MCP/Gateway 81 项通过；扩展集成首轮 190 passed、2 failed，失败根因是历史 schema fixture 使用新 workspace 字段；修复 fixture 后，计划/记忆/迁移相关 34 项通过。Ruff 通过。
- [x] 1.5 将固定 company_scope 迁移为可信 workspace，保持已发布工作流的当前员工稳定记忆槽；测试跨空间同 subject、不合法来源、旧记忆恢复和撤销。证据：migration 022 从员工及已核验回执恢复 workspace、移除固定 `company_scope`，并把经验规则键改为 `(rule_id, rule_version, workspace_id)`；`MemoryContext` 由认证身份构造，记忆候选/稳定槽/来源/发布/撤销均按 workspace + employee 过滤，workspace ID 不出现在记忆 API；同一 `kind + subject` 在两个空间分别解析到各自内容，跨空间回执不能发布规则，同名规则版本可独立发布；旧 active 偏好迁移后仍能从原员工稳定槽读取、重复提议不延长 TTL、原员工可以撤销。相关定向确定性/PostgreSQL 组合 `163 passed`，Ruff 通过。
- [x] 1.6 交付 workspace 身份链和迁移/回退说明，包含泳道图；逐条与本组集成测试和实际迁移核对。说明：[generated/261021/0237_workspace身份与迁移回退说明.md](../../../generated/261021/0237_workspace身份与迁移回退说明.md)。内容核对 `EmployeeAuth` 的浏览器/MCP workspace 绑定、020/021/022 forward migrations、checksum 规则、跨 workspace 记忆来源中止边界、备份恢复约束及隔离 PostgreSQL 集成证据；包含身份请求与迁移恢复 Mermaid 泳道/时序图。文档未含未经执行的操作命令。

## 2. 游客会话与业务体验

- [x] 2.1 实现游客 session 创建/恢复、安全 Cookie/CSRF/Origin，原子初始化独立模拟公司；测试并发创建、无效/过期会话和 loopback/HTTPS Cookie 行为。证据：迁移 `023_guest_sessions.sql` 将会话 token/CSRF 仅保存为 hash，并以复合外键绑定当前员工与 guest workspace；`GuestAuth` 在单个 PostgreSQL 事务中创建 workspace、6 个人员、5 个类别、6 个无密码模拟角色及会话。`POST/GET /api/v3/guest/session` 使用 HttpOnly/SameSite Strict 会话 Cookie、独立 CSRF Cookie、精确 Origin 校验；HTTP 仅接受 loopback，HTTPS Cookie 带 Secure。`DECAGENT_GUEST_TRIAL_ENABLED` 默认 false，入口尚未开放。6 项 PostgreSQL 集成测试覆盖并发隔离、身份恢复、过期/伪造会话、CSRF/Origin/Host、防止客户端指定 workspace、HTTPS/loopback Cookie 和失败原子回滚；定向 Ruff 与 config 测试通过。
- [x] 2.2 实现本空间角色切换及清理浏览器身份缓存，保留原任务/待办执行身份；E2E 验证切换后不能消费旧员工确认，切回后可继续。证据：固定角色 key allowlist；角色切换在同一事务更新 guest/employee session 并旋转 CSRF。隔离 PostgreSQL 测试验证旧员工确认在切换后返回 404 且仍为 pending，切回后同一任务和待办仍归属原员工并可读取；游客退出同时撤销两张 session 并清除 Cookie。Playwright CLI 本地浏览器验证游客首次进入、刷新恢复、切换 supervisor、角色刷新恢复；页面状态清除由 `guest_workbench.cjs` 回归覆盖，包含任务/操作/追踪/记忆及 EventSource；启用/禁用入口的 UI 行为均通过。测试未调用模型。
- [x] 2.3 实现 7 天无活动失效与清空入口；验证过期即禁访问、后台轮询不续命、删除请求幂等和 CSRF。证据：migration 024 增加 `last_activity_at`；仅成功的非被动游客请求滑动续期，session 恢复/status/EventSource 不续期；失效后 `/guest/session`、员工会话回退、MCP token 和 Worker 凭证解析均拒绝。CSRF+Origin 保护的清空会停用 workspace、撤销浏览器/MCP 凭证并禁用该空间账号；`EmployeeAuth` 对浏览器、MCP 与 Worker 凭证解析均要求 workspace active 和有效 guest session。丢失清空响应后，同一原 token+CSRF+Origin 可安全重试并仍返回已清空，错误 Origin/CSRF 不通过；物理记录/检查点清理留给 2.4。游客 PostgreSQL 集成 13 项通过；身份/Workspace/Gateway 与员工账户/操作集成共 78 项通过；deterministic 1937 项通过、18 项因 τ 运行时或 Windows symlink 环境跳过。浏览器实测覆盖开始、切换、刷新、清空；控制台 0 错误/警告。测试无模型调用。
- [x] 2.4 实现受限生命周期清理：撤销凭证、停止准入、取消/等待租约、核实 unknown 后清理记录和 checkpoint；测试在途写入与进程重启无重复副作用。证据：migration `025_guest_cleanup.sql` 增加清理完成标记和仅保存清空重试哈希的 tombstone，并将 workspace 内循环外键延迟到事务提交检查。`GuestWorkspaceCleaner` 每次限量过期/清理；清空后立即尝试，Worker 周期重试；Worker 只领取 active workspace 任务，并持有跨进程 task advisory lock 覆盖 graph/checkpoint 写入，租约续期也要求 workspace active。清理器获取 workspace/task/operation 锁后复查租约、任务和操作集合；租约/锁仍在途、receipt partial/mismatch 或成功操作无 verified receipt 时保持 pending。安全收尾后 fence/cancel 未终态任务、删除所有 checkpoint 与 workspace 业务行，只留下 workspace 完成标记和 clear tombstone；重复清空可验证 tombstone 并幂等返回，未知操作不会重放。隔离 PostgreSQL 游客生命周期 19 项通过，Worker/操作/workspace migration 联合回归 67 项通过；Ruff 通过。测试覆盖真实 BusinessWorker 在途 checkpoint、独立进程持有锁、租约等待、未知 receipt 歧义/已验证、7 天自动过期及跨 Cleaner 重启幂等；无模型调用。
- [ ] 2.5 工作台增加开始体验/恢复/清空/角色提示，仅已注册模拟业务；展示真实 Jev 建议并保持语义选择与写入确认分离，覆盖 Choice/Noul/Score/无匹配/补充/拒绝核心 E2E。部分离线证据（2026-10-21）：工作台已展示服务端返回的 `suggestion`/`raw_model_answer`，`business_semantic_workbench.cjs` 覆盖 Choice/Noul/Score、无匹配、补充、拒绝、过期选择和不调用写入端点，`guest_workbench.cjs` 覆盖会话开始/恢复/角色切换/清空提示。尚未从真实 Jev 请求验证建议来源，也未在浏览器以 Jev 结果跑完六种交互，因此任务保持未完成，不用模拟结果冒充真实体验。
- [ ] 2.6 按游客使用子目录交付指南、会话/过期时序图及权限说明；实际浏览器走通文档，不虚构公网地址或生产准确率。

## 3. 资源准入与试用预算

- [ ] 3.1 实现独立 trial 日账本和配置，按北京时间全站 $5/500万 token、游客30任务/50万 token；并发集成测试证明不会超额，评测账本不受影响。
- [ ] 3.2 将模型派发接入任务/游客/全站原子预留与实际结算，unknown 保留；测试免费额度、无可靠价格拒发、超时、跨日和重启不能释放未知占用。
- [ ] 3.3 实现游客1/全站2的持久执行准入及20等待队列，任务计数在入队成功后占用；测试满载拒绝、取消、租约回收、公平领取和恢复不双执行。
- [ ] 3.4 实现创建/提交/回复限流与受信代理解析，UI显示额度/等待/429；测试清Cookie仍受全站限制、不可信转发头无效和队列边界。
- [ ] 3.5 交付资源与费用说明及预留/结算状态图；用本组测试账本核对零费用、未知和拒绝显示口径。

## 4. SOP 本机部署

- [ ] 4.1 交付 API/业务Worker/业务MCP/PostgreSQL SOP Compose 与配置预检；真实容器检查条件计划开关、MCP身份和仅loopback暴露，保留repair兼容隔离。
- [ ] 4.2 实现含 Worker/MCP 的业务健康探针及专用可清理空间、零付费脚本模型冒烟；故障注入证明API健康不能掩盖业务故障。
- [ ] 4.3 实现旧digest/config/schema记录、备份和兼容应用回滚；演练升级成功、健康失败回滚、schema不兼容停止且不覆盖数据库。
- [ ] 4.4 核验 Windows 登录启动、Docker Desktop、Runner 和服务重启恢复，交付本机部署子目录指南、拓扑/恢复图及实测证据。

## 5. 公共 CI 与私有 CD

- [ ] 5.1 优化公共CI分层、独立job并行与锁定缓存，保留Python3.11/3.13、Ruff、确定性、PostgreSQL、核心E2E、wheel/MCP、Compose；比较各job与总耗时，列出skip原因。
- [ ] 5.2 实现私有部署仓库的发布验证器和最小输入工作流；离线测试拒绝错误repo/SHA/tag/main/CI/run attempt/digest和任意脚本路径，不在本机执行未验证代码。
- [ ] 5.3 创建/配置私有 DecAgent-deploy，迁移本机Runner，公共发布仅最小凭证触发固定workflow；验证公共PR/Actions无法调度本机，私有凭证最小且不打印密钥。部分进度（2026-10-21）：公共仓库 Runner 数量 0，公共 workflow 无 self-hosted deployment job，本机 `DecAgentLocalDeployRunner` 计划任务 Disabled，Runner 服务/进程为 0；因此当前公共路径无法调度本机。私有仓库、最小触发凭证、Runner 迁移和验证仍未完成，本项保持未勾选。
- [ ] 5.4 完成一次可信版本GHCR发布至本机SOP服务的全链路演练及失败演练；保存commit/run/digest/健康/回滚证据，任务串行无互相覆盖。
- [ ] 5.5 分别更新代码仓库、CI、CD操作指南与导航，提供发布时序/信任边界图；核对真实仓库/命令，不把公网与独立Linux标为完成。

## 6. 最终集成核验

- [ ] 6.1 串联两游客完整业务与记忆、审批、配额、重启恢复；运行对应确定性/集成/E2E和安装产物检查，逐项关联需求与证据，无环境skip不冒充通过。
- [ ] 6.2 严格校验两个相关change及文档链接，交付当前通过/失败/证据不足/待办清单；保留原公开评测和真实员工研究待办，不以游客演示替代。

## Workflow follow-up

- 域名购买、正式公网入口与入口防滥用验证、跨设备账号绑定、独立 Linux 服务保证后续再实施；本轮不自动开通。
- 按用户另行请求再归档和同步主规格，不隐式归档旧 change。
