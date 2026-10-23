# Tasks

当前状态（2026-10-24）：22/28 项验收完成；剩余 6 项保持未验收：2.5、4.4、5.3、5.4、6.1、6.2。
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
- [ ] 2.5 工作台增加开始体验/恢复/清空/角色提示，仅已注册模拟业务；展示真实 Jev 建议并保持语义选择与写入确认分离，覆盖 Choice/Noul/Score/无匹配/补充/拒绝核心 E2E。**2026-10-24 进展：** 新增并接入真实 Chromium 游客生命周期 E2E，验证开始体验、切换角色仍处于同一 workspace、刷新后恢复、清空后 workspace 失效且无活动会话；0 模型请求、0 页面错误。现有 Node UI 回归覆盖 Jev 建议卡及 Choice/Noul/Score/无匹配/补充/拒绝的决策接口与写入隔离；尚无真实浏览器内完整 Jev 建议到语义回复链，也未调用真实 Jev，因此本项继续未验收。见[游客工作台浏览器 E2E](../../../evals/integration/evidence/guest-workbench-e2e-20261024.md)。
- [x] 2.6 按游客使用子目录交付指南、会话/过期时序图及权限说明；实际浏览器走通文档，不虚构公网地址或生产准确率。

## 3. 资源准入与试用预算

- [x] 3.1 实现独立 trial 日账本和配置，按北京时间全站 $5 美元等值抵扣前 API 费用/500万 Qwen token、每游客30任务/50万 Qwen token；CNY 账单使用冻结的 CFETS USD/CNY 中间价快照并按 10% 安全余量保守换算，报价超 7 天拒绝 CNY 预留，含代金券的抵扣前 API 费用照计；原子并发测试证明不超额，公开评测 Jev 账本不受影响。
- [x] 3.2 将模型派发接入任务/游客/全站原子预留与实际结算，unknown 保留；固定 OpenRouter Jev 1.14 费率 profile，按完整 262,144 token 上下文计算费用上界 $0.011010048、每 attempt 预留 $0.02，别名/错误 provider/超限请求拒发。PostgreSQL 集成验证原子预留、费用结算、免费额度、超时及 unknown 保留；真实 Jev UI/E2E 仍属 2.5。证据：[游客模型预算验收](../../../evals/integration/evidence/guest-trial-model-budget-20261023.md)、[Jev 固定费率边界](../../../evals/integration/evidence/guest-jev-priced-bound-20261023.md)。
- [x] 3.3 实现游客1/全站2的持久执行准入及20等待队列，任务计数在入队成功后占用；测试满载拒绝、取消、租约回收、公平领取和恢复不双执行。
- [x] 3.4 实现创建/提交/回复限流与受信代理解析，UI显示额度/等待/429；测试清Cookie仍受全站限制、不可信转发头无效和队列边界。
- [x] 3.5 交付资源与费用说明及预留/结算状态图；说明每日汇率快照/来源日期/安全余量、抵扣前费用、券后实付和跨币种换算；用测试账本核对零费用、unknown 和拒绝派发显示口径。

## 4. SOP 本机部署

- [x] 4.1 交付 API/业务Worker/业务MCP/PostgreSQL SOP Compose 与配置预检；真实容器检查条件计划开关、MCP身份和仅loopback暴露，保留repair兼容隔离。
- [x] 4.2 实现含 Worker/MCP 的业务健康探针及专用可清理空间、零付费脚本模型冒烟；故障注入证明 API 健康不能掩盖业务故障。
- [x] 4.3 支持安全首次安装与后续升级：首次安装只允许固定 SOP Compose 项目完全无容器/卷/网络时创建；持久记录 image digest 和 Compose 指纹（含 secret 文件摘要），失败后仅允许同一 digest/配置恢复，不删除数据。确定性回归覆盖首次安装状态拒绝、同 digest/config 恢复及密钥轮换指纹变化；真实 Docker 验收保留在 4.4。
- [ ] 4.4 在真实本机 Docker 上演练首次安装、失败后同 digest/Compose/secret 恢复且保留数据库、正常升级、健康失败兼容回滚及 schema 不兼容停服；核验 Windows 登录启动、Docker Desktop、Runner 和服务重启恢复，交付本机部署子目录指南、拓扑/恢复图及实测证据。不得仅以离线测试关闭本项。2026-10-24 已将 SOP 默认宿主机端口调整为 8001，与仍运行的 `decagent-api` 8000 分开；当前端口查询及静态预检通过，但真实首装前仍须重查端口占用。此前候选 SHA `d7e8b0e79df5c443a756beb76f1094c799c28f1a` 的一次性本地 smoke 验证六服务 readiness、Worker 故障检测/恢复和资源清理；端口修正后的候选尚未进行持久首装、升级或回滚验收。证据：[启动竞态修复与隔离 smoke](../../../evals/integration/evidence/local-sop-smoke-startup-race-20261024.md)。

## 5. 公共 CI 与私有 CD

- [x] 5.1 优化公共CI分层、独立job并行与锁定缓存，保留Python3.11/3.13、Ruff、确定性、PostgreSQL、核心E2E、wheel/MCP、Compose；比较各job与总耗时，列出skip原因。基线 `9743e1a6d303e41cd88175a97d3b386e7edfefd6` 的 [run 39951724425](https://github.com/shuaibilx/DecAgent/actions/runs/39951724425) 和评测候选 `50e38b71ce030d7a17593611e4efd564f0a183bf` 的同 SHA [run 39981292425](https://github.com/shuaibilx/DecAgent/actions/runs/39981292425) 均为 12/12 jobs 通过；候选 run 墙钟 8:23、累计 Runner 53:36。候选 Python 3.11/3.13 deterministic 各为 2,169 passed、18 skipped，六个 PostgreSQL 集成 shards 均成功。完整耗时、计数边界及 skip 分类见[CI验收报告](../../../evals/integration/evidence/ci-browser-sharding-20261023.md)。 最新代码候选 `7b56d588682a36d8ea2171890676af3e6f45ac3c` 的同 SHA [run 40014153430](https://github.com/shuaibilx/DecAgent/actions/runs/40014153430) 也为 12/12 jobs 成功（8:35 墙钟、53:11 累计 Runner）；两个 Python deterministic 各 `2,176 passed, 18 skipped`，六个 PostgreSQL shards 合计 `1,364 passed, 66 skipped`。
- [x] 5.2 实现私有部署仓库的发布验证器和最小输入工作流；离线测试拒绝错误 repo/SHA/tag/main/CI/run attempt/digest 和任意脚本路径，不在本机执行未验证代码。审查确认发布验证先于 rollout，且只传验证器输出；真实 dispatch 留在 5.3/5.4。
- [ ] 5.3 配置私有 DecAgent-deploy，迁移本机 Runner，公共发布仅用最小凭证触发固定 workflow；验证公共 PR/Actions 无法调度本机，私有凭证最小且不打印密钥。**2026-10-24 部分完成：** GitHub UI 已确认 App 只安装到 `shuaibilx/DecAgent-deploy`；权限为 `metadata: read` 与 `actions: write`。当前 API 核验仍为私有仓库 Runner 1 个在线、公开仓库 0 个；本机 `DecAgentPrivateDeployRunner` 计划任务运行，旧公共 Runner 计划任务禁用。公开发布 workflow 为 tag-only，dispatch job 仍使用 GitHub-hosted Runner，且仅在 `DECAGENT_PRIVATE_CD_ENABLED == 'true'` 时运行；该公开变量当前不存在，因此门禁关闭。普通 `gh` 用户凭证调用安装 API 返回 401（需要 App JWT），不能作为安装范围证据。尚未完成 App-authenticated 范围复核、受控 dispatch 与公共 PR/Actions 不可调度验证，因此本项保持未验收；没有触发部署。见[安装范围恢复证据](../../../evals/integration/evidence/github-app-install-scope-recovery-20261024.md)。
- [ ] 5.4 完成一次可信版本GHCR发布至本机SOP服务的全链路演练及失败演练；保存commit/run/digest/健康/回滚证据，任务串行无互相覆盖。
- [x] 5.5 分别更新代码仓库、CI、CD 操作指南与导航，提供发布时序/信任边界图；核对真实仓库/命令，不把公网与独立 Linux 标为完成。证据：[文档总索引](../../../docs/README.md)、[代码仓库](../../../docs/repository/README.md)、[CI](../../../docs/ci/README.md)、[CD](../../../docs/cd/README.md)、[本机部署](../../../docs/deployment/README.md)。文档含仓库/版本流、CI 分层、发布时序、信任边界、loopback Compose 拓扑与回滚说明；区分已执行命令和未执行模板。本机 SOP env/secrets 已准备且静态预检通过；截至 2026-10-24，GitHub App 已由用户确认只安装到私有 `shuaibilx/DecAgent-deploy`，权限为 `metadata: read` 与 `actions: write`；该仓库有 1 个在线 Runner，公开 `DecAgent` 为 0。公开仓库已配置 App 凭证但部署开关未设置；未派发 workflow、未创建 release tag、未部署持久服务。私有 CD、真实 Docker 首装/升级/回滚和独立 Linux 仍未验收。最新范围证据见[安装范围核验与恢复记录](../../../evals/integration/evidence/github-app-install-scope-recovery-20261024.md)。

## 6. 最终集成核验

- [ ] 6.1 串联两游客完整业务与记忆、审批、配额、重启恢复；运行对应确定性/集成/E2E和安装产物检查，逐项关联需求与证据，无环境skip不冒充通过。
- [ ] 6.2 严格校验两个相关change及文档链接，交付当前通过/失败/证据不足/待办清单；保留原公开评测和真实员工研究待办，不以游客演示替代。

## 当前实施检查点（2026-10-23）

- **3.2 游客模型预算（实现与 PostgreSQL 验收完成）：** 2026-10-23 在独立临时 Postgres 17 上运行预算集成模块，38 项通过、0 skip；另有 4 个 Jev 费率 profile 确定性测试通过。覆盖 task/trial 原子预留、provider usage 结算、免费 Qwen token、超时和 unknown 保留。新增 Jev 固定费率 profile 只允许 OpenRouter typesafe/jev-1.14；以 262,144 token context × $0.042/M 计算最大输入费 $0.011010048，按 $0.02 预留。别名、错误 provider、低预留及超上下文请求派发前拒绝。全程仅使用 mock HTTP，无真实 Jev 请求；task 2.5 的真实建议 UI/E2E 仍待完成。

- **4.3 首装/恢复（离线实现验收）：** 首次安装编排、同 digest/Compose/secret 内容指纹恢复、项目容器/卷/网络状态拒绝、Python 3.11+ 解释器校验已实现。全量 deterministic `2,160 passed, 18 skipped`，包含 secret 轮换指纹回归；Ruff 通过。2026-10-23 本机 Docker Linux engine/Compose 可用，SOP env/secrets 已安全准备，`sop_preflight` 通过且未创建容器；真实 Docker 首装、失败恢复、升级和回滚演练仍属于 4.4。见[本机 SOP 预检证据](../../../evals/integration/evidence/local-sop-preflight-20261023.md)。
- **5.2 私有发布验证（实现验收）：** 私有仓库有固定输入 workflow 与 verifier；只允许 `contents: read`，GHCR public control image 匿名拉取。工作流顺序测试核验 verifier 在 rollout 前运行，且只传 verifier 输出。远端 dispatch 与 Runner 属于 5.3/5.4，尚未验收。
- **5.1 公共 CI（验收完成，2026-10-23）：** 最终 SHA `9743e1a6d303e41cd88175a97d3b386e7edfefd6` 的 GitHub Actions [run 39951724425](https://github.com/shuaibilx/DecAgent/actions/runs/39951724425) 12/12 jobs 通过。相较旧基线 run `39940453829`，墙钟由 12:27 降至 8:19；累计 Runner 时间由 51:39 变为 52:05（增加 26 秒），主要新增了浏览器 E2E 覆盖，故不宣称资源费用下降。E2E 使用 digest 固定的 Playwright Python 镜像；Python 3.11/3.13 主矩阵保持不变。完整 job 时间和 skip 理由见[CI 验收报告](../../../evals/integration/evidence/ci-browser-sharding-20261023.md)。
- **5.3/5.4 私有交付检查点（2026-10-23 历史快照）：** DecAgent-deploy main `362a00d1604bc2daa7dcdf90d9c7b58a89e36a34` 已同步审查后的 11 文件部署包，manifest 中 10 个文件哈希逐一远端核对通过；Actions 仅允许 SHA 固定的 `actions/checkout`，`decagent-local-production` 仅允许 main。本机 SOP env/secrets 已准备，Docker Compose 静态预检通过。该日记录的 App 安装页选择状态后来被证明错误：安装实际选择了全部仓库，之后已撤销。部署开关仍关闭，没有 Runner、真实 dispatch 或容器部署。公共发布边界改动已推送到 `feat/conditional-evaluation`，没有创建 release tag，也没有派发 CD。详见[私有部署边界证据](../../../evals/integration/evidence/private-deployment-boundary-20261023.md)和[本机预检证据](../../../evals/integration/evidence/local-sop-preflight-20261023.md)。最新状态见 5.3 与[安装范围恢复证据](../../../evals/integration/evidence/github-app-install-scope-recovery-20261024.md)。
- **5.5 仓库/CI/CD/部署文档（2026-10-23）：** 新增 `docs/` 导航以及 repository、ci、cd、deployment 四个分区指南。核对公共 `DecAgent`、私有 `DecAgent-deploy`、CI 12-job 证据、tag-only GHCR 发布、受限 GitHub App dispatch、private main-only workflow 与 SOP Compose loopback/secret/备份回滚边界。Mermaid 图展示 release 顺序、信任边界和本机服务拓扑。每个示例命令明确标出是否实际运行；文档初次验收时 App/Runner 尚未配置，未创建 release tag 或公网入口。根 README 增加文档索引；内部链接、OpenSpec strict 和 `git diff --check` 通过。后续 App/Runner 配置状态见 5.3/5.4 检查点。
- **当前交付边界（2026-10-24）：** GitHub App 已在 UI 中安装到唯一选择的私有 `DecAgent-deploy` 仓库；私有 Runner 在线，公开 Runner 为 0，旧公共 Runner 计划任务禁用。App secret/Client ID 已配置，部署开关仍关闭；尚未做 App-authenticated 独立范围核验、dispatch、服务部署或回滚演练。SOP env/secrets 已准备且静态预检通过，但持久服务容器和受信候选镜像尚未部署。私有仓库分支保护受当前 GitHub 计划限制；环境分支规则仍约束部署到 main。详细边界见[安装范围恢复证据](../../../evals/integration/evidence/github-app-install-scope-recovery-20261024.md)。
- **验证边界：** v10 首波报告记录指定候选 8 路线终态为 3 failed、5 unknown、0 success；这不是通过结果，unknown 不重放。后续代码/文档修正未重发模型请求。最终 CI 中每个 Python 版本 deterministic 为 `2,164 passed, 18 skipped`，每个版本三个 PostgreSQL 分片合计 `678 passed, 33 skipped`；所有 skip 仍表示相应运行时、平台或 opt-in fixture 未在这些 job 中验证。旧 CI run `39935184424` 在 `4e932ce` 上因测试日期错位失败，已修复。修复提交 `14ffd1fe238306cdd858673bb786ac2c3bd581ff` 的同 SHA CI [run 39940453829](https://github.com/shuaibilx/DecAgent/actions/runs/39940453829) 9/9 jobs 全绿；task 5.1 的最新扩展覆盖见 [run 39951724425](https://github.com/shuaibilx/DecAgent/actions/runs/39951724425)。私有 bundle `-B -S` 导入 smoke 已通过。
- **本机部署与触发边界复核（2026-10-23，早期历史快照）：** 此次检查发生在 GitHub App 创建和凭证配置之前，不能代表当前凭证状态；最新状态见上方“5.3/5.4 私有交付仍待办”。GitHub 公共 `main` 为 `0c747a3`，release workflow 保留 tag-only GHCR 发布，没有本机 Runner 调度；公开和私有 Runner API 均返回 0。`DECAGENT_PRIVATE_CD_ENABLED` 返回 404，公开仓库目前没有 App client-id variable 或 App private-key secret 名称。本机 `DecAgentPrivateDeployRunner` 计划任务、Runner 进程与目录均不存在。SOP 静态预检再次通过；一次性 Compose smoke 启动六项服务、通过业务 readiness 与 Worker 故障注入/恢复，model requests=0、guest trial=false，退出码 0 后已核验清理项目容器/卷/网络/镜像。旧 `decagent-api` 仍占用 `127.0.0.1:8000` 并保持运行；本地 smoke 使用随机端口，未改动它。已有 `v0.1.4` GHCR control image 对应代码不含本 change 的 SOP Compose，不能作为当前 SOP 发布候选。4.4、5.3、5.4 仍未完成；证据：[隔离 Docker smoke](../../../evals/integration/evidence/local-sop-disposable-smoke-20261023.md)、[私有部署边界](../../../evals/integration/evidence/private-deployment-boundary-20261023.md)。
- **Jev 费用 profile 回归（2026-10-23）：** OpenRouter adapter 将员工 `jev-latest` 规范化为 `~typesafe/jev-latest`；预算绑定现按该规范化映射记录实际请求名，同时访客精确 profile pin 不接受 alias 漂移。相关 Jev/业务服务/条件判断/访客预算回归 193 passed，详见[固定 Jev 费用边界证据](../../../evals/integration/evidence/guest-jev-priced-bound-20261023.md)。
