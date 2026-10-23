# Tasks

当前状态（2026-10-23）：19/28 项验收完成；未验收项继续保持未完成。
本文件保留全部任务编号、复选状态和验收目标。逐项实现记录、历史测试输出及旧状态原文完整归档于 [tasks-history-20261023.md](tasks-history-20261023.md)。

## 1. Workspace 数据与身份边界

- [x] 1.1 追加 workspace/legacy 归属迁移及必要唯一/关联约束；隔离 PostgreSQL 验证旧员工、任务、审批、记忆、操作和 checkpoint 原身份不变，迁移重复执行安全。
- [x] 1.2 扩展可信员工身份与凭证的 workspace 解析，统一 workspace-aware 数据访问；测试拒绝客户端覆盖空间和跨空间关联，并保留旧员工登录/MCP 协议回归。
- [x] 1.3 隔离目录、工单与分派候选，限制 supervisor 为本空间；两空间集成测试覆盖读取/列表/写入/候选和原子版本冲突。
- [x] 1.4 隔离任务、审批、操作、回执、trace/export 与 Worker/MCP 分派；两空间猜 ID、重放确认、切换员工和恢复测试全部拒绝越权。
- [x] 1.5 将固定 company_scope 迁移为可信 workspace，保持已发布工作流的当前员工稳定记忆槽；测试跨空间同 subject、不合法来源、旧记忆恢复和撤销。
- [x] 1.6 交付 workspace 身份链和迁移/回退说明，包含泳道图；逐条与本组集成测试和实际迁移核对。

## 2. 游客会话与业务体验

- [x] 2.1 实现游客 session 创建/恢复、安全 Cookie/CSRF/Origin，原子初始化独立模拟公司；测试并发创建、无效/过期会话和 loopback/HTTPS Cookie 行为。
- [x] 2.2 实现本空间角色切换及清理浏览器身份缓存，保留原任务/待办执行身份；E2E 验证切换后不能消费旧员工确认，切回后可继续。
- [x] 2.3 实现 7 天无活动失效与清空入口；验证过期即禁访问、后台轮询不续命、删除请求幂等和 CSRF。
- [x] 2.4 实现受限生命周期清理：撤销凭证、停止准入、取消/等待租约、核实 unknown 后清理记录和 checkpoint；测试在途写入与进程重启无重复副作用。
- [ ] 2.5 工作台增加开始体验/恢复/清空/角色提示，仅已注册模拟业务；展示真实 Jev 建议并保持语义选择与写入确认分离，覆盖 Choice/Noul/Score/无匹配/补充/拒绝核心 E2E。
- [x] 2.6 按游客使用子目录交付指南、会话/过期时序图及权限说明；实际浏览器走通文档，不虚构公网地址或生产准确率。

## 3. 资源准入与试用预算

- [x] 3.1 实现独立 trial 日账本和配置，按北京时间全站 $5 美元等值抵扣前 API 费用/500万 Qwen token、每游客30任务/50万 Qwen token；CNY 账单使用冻结的 CFETS USD/CNY 中间价快照并按 10% 安全余量保守换算，报价超 7 天拒绝 CNY 预留，含代金券的抵扣前 API 费用照计；原子并发测试证明不超额，公开评测 Jev 账本不受影响。
- [ ] 3.2 将模型派发接入任务/游客/全站原子预留与实际结算，unknown 保留；测试免费额度、无可靠价格拒发、超时、跨日和重启不能释放未知占用。
- [x] 3.3 实现游客1/全站2的持久执行准入及20等待队列，任务计数在入队成功后占用；测试满载拒绝、取消、租约回收、公平领取和恢复不双执行。
- [x] 3.4 实现创建/提交/回复限流与受信代理解析，UI显示额度/等待/429；测试清Cookie仍受全站限制、不可信转发头无效和队列边界。
- [x] 3.5 交付资源与费用说明及预留/结算状态图；说明每日汇率快照/来源日期/安全余量、抵扣前费用、券后实付和跨币种换算；用测试账本核对零费用、unknown 和拒绝派发显示口径。

## 4. SOP 本机部署

- [x] 4.1 交付 API/业务Worker/业务MCP/PostgreSQL SOP Compose 与配置预检；真实容器检查条件计划开关、MCP身份和仅loopback暴露，保留repair兼容隔离。
- [x] 4.2 实现含 Worker/MCP 的业务健康探针及专用可清理空间、零付费脚本模型冒烟；故障注入证明 API 健康不能掩盖业务故障。
- [x] 4.3 支持安全首次安装与后续升级：首次安装只允许固定 SOP Compose 项目完全无容器/卷/网络时创建；持久记录 image digest 和 Compose 指纹（含 secret 文件摘要），失败后仅允许同一 digest/配置恢复，不删除数据。确定性回归覆盖首次安装状态拒绝、同 digest/config 恢复及密钥轮换指纹变化；真实 Docker 验收保留在 4.4。
- [ ] 4.4 在真实本机 Docker 上演练首次安装、失败后同 digest/Compose/secret 恢复且保留数据库、正常升级、健康失败兼容回滚及 schema 不兼容停服；核验 Windows 登录启动、Docker Desktop、Runner 和服务重启恢复，交付本机部署子目录指南、拓扑/恢复图及实测证据。不得仅以离线测试关闭本项。

## 5. 公共 CI 与私有 CD

- [ ] 5.1 优化公共CI分层、独立job并行与锁定缓存，保留Python3.11/3.13、Ruff、确定性、PostgreSQL、核心E2E、wheel/MCP、Compose；比较各job与总耗时，列出skip原因。
- [x] 5.2 实现私有部署仓库的发布验证器和最小输入工作流；离线测试拒绝错误 repo/SHA/tag/main/CI/run attempt/digest 和任意脚本路径，不在本机执行未验证代码。审查确认发布验证先于 rollout，且只传验证器输出；真实 dispatch 留在 5.3/5.4。
- [ ] 5.3 配置私有 DecAgent-deploy，迁移本机 Runner，公共发布仅用最小凭证触发固定 workflow；验证公共 PR/Actions 无法调度本机，私有凭证最小且不打印密钥。
- [ ] 5.4 完成一次可信版本GHCR发布至本机SOP服务的全链路演练及失败演练；保存commit/run/digest/健康/回滚证据，任务串行无互相覆盖。
- [ ] 5.5 分别更新代码仓库、CI、CD操作指南与导航，提供发布时序/信任边界图；核对真实仓库/命令，不把公网与独立Linux标为完成。

## 6. 最终集成核验

- [ ] 6.1 串联两游客完整业务与记忆、审批、配额、重启恢复；运行对应确定性/集成/E2E和安装产物检查，逐项关联需求与证据，无环境skip不冒充通过。
- [ ] 6.2 严格校验两个相关change及文档链接，交付当前通过/失败/证据不足/待办清单；保留原公开评测和真实员工研究待办，不以游客演示替代。

## 当前实施检查点（2026-10-23）

- **4.3 首装/恢复（离线实现验收）：** 首次安装编排、同 digest/Compose/secret 内容指纹恢复、项目容器/卷/网络状态拒绝、Python 3.11+ 解释器校验已实现。全量 deterministic `2,160 passed, 18 skipped`，包含 secret 轮换指纹回归；Ruff 通过。真实 Docker 首装、失败恢复、升级和回滚演练属于 4.4，尚未执行。
- **5.2 私有发布验证（实现验收）：** 私有仓库有固定输入 workflow 与 verifier；只允许 `contents: read`，GHCR public control image 匿名拉取。工作流顺序测试核验 verifier 在 rollout 前运行，且只传 verifier 输出。远端 dispatch 与 Runner 属于 5.3/5.4，尚未验收。
- **5.3/5.4 私有交付仍待办：** DecAgent-deploy main `362a00d1604bc2daa7dcdf90d9c7b58a89e36a34` 已同步审查后的 11 文件部署包，manifest 中 10 个文件哈希逐一远端核对通过；Actions 仅允许 SHA 固定的 `actions/checkout`，`decagent-local-production` 仅允许 main。尚无 Runner、本机 SOP 配置和 secrets，GitHub App 触发器未配置，未做真实 dispatch 或容器部署。公共发布边界改动已推送到 `feat/conditional-evaluation`，没有创建 release tag，也没有派发 CD。详见[私有部署边界证据](../../../evals/integration/evidence/private-deployment-boundary-20261023.md)。
- **尚未开通：** 本机 sop.env/secret files 和 Runner 不存在；公有 release 到私有 workflow 的 GitHub App 凭证及启用变量尚未配置。私有仓库分支保护受当前 GitHub 计划限制；环境分支规则仍约束部署到 main。
- **验证边界：** v10 首波报告记录指定候选 8 路线终态为 3 failed、5 unknown、0 success；这不是通过结果，unknown 不重放。后续代码/文档修正未重发模型请求。当前 deterministic 为 `2,164 passed, 18 skipped`，Ruff 与两个 change 的 OpenSpec strict 通过；guest budget PostgreSQL 集成模块 `36 passed`。旧 CI run `39935184424` 在 `4e932ce` 上因测试日期错位失败，已修复。修复提交 `14ffd1fe238306cdd858673bb786ac2c3bd581ff` 的同 SHA CI [run 39940453829](https://github.com/shuaibilx/DecAgent/actions/runs/39940453829) 9/9 jobs 全绿。私有 bundle `-B -S` 导入 smoke 已通过。
