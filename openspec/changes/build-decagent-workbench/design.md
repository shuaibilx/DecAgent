# Design

## Context

动机见 [proposal.md](proposal.md)，行为契约见其声明的 8 份 delta specs。本文件记录实施决定，不代表相应功能已经存在。

已检查的 Waku 现状：

- `waku/app.py` 以同步 `respond()` 组织上下文、工具循环、聊天落库和记忆整理，客户端与数据库可注入，适合作为可测试性参考。
- `waku/tools/registry.py` 的执行保护只捕获异常，没有参数强校验、授权、审批或隔离；`workspace.py` 可以直接在宿主运行生成脚本，不能带入新执行路径。
- `runtime/session.py` 只恢复聊天历史；`db.py` 使用 SQLite/FTS5，没有任务、审批、执行凭据或 checkpoint 数据模型。
- `memory/retrieval_gate.py` 共享小模型客户端，使用宽松 JSON 解析及失败放行；`consolidation.py` 混合未整理会话，逐条写入，无项目版本、冲突或原子批次。
- `experimental.py` 通过 pi CLI 委托；其命令转录存在把密钥参数写入日志的风险。新项目不复制该委托链或宿主自动运行机制。
- 现有网页是静态 HTML/CSS/JavaScript，确定性测试位于 `evals/deterministic/`。沿用这种轻量界面和测试分层，不复制全部个人助手代码。
- 当前已初始化 OpenSpec，但没有主规格；当前目录不是 Git checkout，且 `../DecAgent` 尚不存在。

## Goals / Non-Goals

**Goals:**

- 让生成、判断、授权和执行可以独立测试，所有工具路径经过同一治理入口。
- 让检查点、审计、长期记忆和外部副作用凭据各自有清晰职责。
- 在单用户部署中提供真实服务、真实任务恢复和可复现评测，控制默认部署复杂度。

**Non-Goals:**

- 首发不引入多租户调度、任意插件自动安装、通用远程桌面、自动合并或宿主任意命令。
- 普通容器不被描述为可以安全执行恶意代码；支持范围仍是用户认可的可信代码。
- 不依赖商业可观测性服务或向其默认上传源码，不自动训练或微调 Jev。

## Decisions

### 1. Planning home and independent code target

本 change 的权威规划目录为当前项目的 `openspec/changes/build-decagent-workbench/`；代码目标为当前项目父目录下的 `DecAgent`，包名和命令均为 `decagent`。apply 阶段必须明确将构建、测试和文件修改指向这个目标，不能用当前 Waku 根目录作为默认实施目录。若工具返回的编辑范围只覆盖 Waku，先选择或附加 DecAgent 为实施目标，不通过偷偷扩大范围执行。

本阶段不创建目标目录、不修改工作流配置、不初始化 Git。实施时选择性移植需要的模式，记录来源并保留 LICENSE/NOTICE；不复制 `.env`、`.waku/`、`.venv/`、生成工作区或外部服务数据。若目标已存在，先检查内容，不覆盖。目标没有用户远程仓库时只进行本地工作，不向原上游推送。

默认结构：`decagent/runtime/`、`models/`、`decisions/`、`policy/`、`tools/`、`executor/`、`memory/`、`integrations/`、`gateway/`、`ops/`。每个目录只承担相应职责，数据访问集中在存储模块，入口只负责装配。测试位于 `evals/deterministic/`、`evals/integration/`、`evals/live/`；对照实验与人工标签位于 `evals/benchmarks/`。避免生成大量只转发调用的抽象层。

规划使用 OpenSpec 的固定文件名和路径，不再复制到 `generated/` 形成第二份权威规格。普通启动指南、任务报告和评测导出在 DecAgent 的 `generated/YYMMDD/HHmm_主题.md` 归档；发布快照可携带本 change 的规格，但不擅自迁移当前活动 change。

替代方案：原地改造 Waku 会违反保留参考项目的选择；完整复制会继承个人业务、凭据风险和无关依赖。因此采用独立目标与选择性移植。

### 2. Explicit graph with a small tool surface

采用 LangGraph `StateGraph` 和 LangChain 模型/工具类型。首发不用 Deep Agents 内置 harness，也不用 pi 子进程：精确控制每个执行和审批边界比现成自动化更符合本项目目标。

主模型由 `langchain-openai` 的 Chat Completions 兼容适配器提供；不同供应商必须通过工具调用、工具结果关联、流式结束及 usage 契约测试。基础流程支持非流式执行，流式只改善显示。不是所有声称兼容的地址都会自动被标为已验证。

基础图：准备任务和上下文 -> 推断/规划 -> 提出一个动作 -> 治理 -> 可选人工等待 -> 执行 -> 观察；按阶段进入复现、修复、验证、交付和候选记忆处理。首发同一任务串行执行工具，不让并行动作改变正在审批的快照。核心工具只包括读取、搜索、受限写入/编辑、检查差异、执行命令、请求用户信息，以及受控 GitHub 交付。

pi 的借鉴点是小型工具集、调用前拦截、追加事件和上下文压缩，不复制它的安全假设。参考 [pi 安全说明](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/security.md)；其权限设计不能代替本项目的执行边界。

### 3. Durable state, exclusive execution and receipts

PostgreSQL 既保存应用数据，也保存 LangGraph checkpoint；不引入 Redis。应用数据与 checkpoint 使用不同 schema，部署迁移单独执行。任务领取使用数据库行锁和带过期时间的 lease，执行令牌带递增 generation；执行 broker 检查该 generation，过期 worker 即使继续运行也不能启动新动作。

每个任务一个稳定 `thread_id = task_id`。审批节点只读取/创建幂等审批记录后调用 `interrupt()`，不在它之前做不可重复副作用；恢复使用同一 thread 和 `Command(resume=...)`，节点重入后再次校验动作。不能把恢复等同于重发原用户消息。依据 [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) 和 [persistence](https://docs.langchain.com/oss/python/langgraph/persistence)；checkpoint 本身不提供外部调用的 exactly-once 保证。

任务状态为 `queued / running / awaiting_input / awaiting_approval / paused_budget / recovering / succeeded / failed / cancelled`。阶段另存 `prepare / reproduce / repair / verify / deliver / finalize`，不把每个细小动作编码成任务状态。成功要求独立验证和回归均通过；允许发布的任务还要求草稿 PR 已确认，未授权发布的任务交付本地证据包。

动作先持久化为 `planned`，领取后为 `running`，最终为 `succeeded / failed / unknown / denied`。动作键由 task、逻辑步骤和动作摘要组成，执行凭据绑定输入及结果哈希。可重做的读取在核实后重试；运行中的命令先检查 task 容器和执行编号。进程已消失而结果未知时不假定完成；对可能产生外部副作用的命令转人工，对明确可重做的检查创建新的尝试记录。

GitHub 发布有独立 reconciliation：查询固定任务分支、提交标识及 PR，确认是否已发生。无法确定时保持 `unknown`，不能靠无限重试逼出结果。应用状态与图检查点间的崩溃窗口通过凭据和 reconciliation 修复，不宣称跨 GitHub 与数据库的原子事务。

### 4. Data contracts and migrations

应用 schema 使用受版本控制的 SQL 迁移及 `schema_migrations`，首次部署与升级由独立 `decagent migrate` 执行；启动仅校验版本，不每次启动修改数据库。

| 数据 | 主要关系与约束 |
| --- | --- |
| projects | 稳定仓库标识、基础分支、允许的代码信任范围、配置版本；凭据只保存引用 |
| tasks | project、基础 SHA、授权快照、状态/阶段、预算、lease generation、graph version |
| task_events | task 内单调 seq、类型、脱敏 payload、时间；原事件追加保存 |
| actions / action_attempts | 逻辑步骤唯一键、动作摘要、快照、状态、执行编号及凭据 |
| approvals | task/action、绑定摘要、审批类别、待审/回复状态、回复版本；回复只处理一次 |
| verification_runs | 基础/修复 SHA、固定检查材料哈希、命令、用例清单、环境、退出状态和日志引用 |
| decision_records / usage_records | 问题与策略版本、输入哈希、请求/实际模型、合法结果或错误、最终路由、每次调用 usage |
| memory_candidates / memory_versions | project、source task/PR、类别、来源、适用性、确认引用、候选/活跃/撤销状态及取代关系 |
| deliveries | task 唯一交付、分支、提交、PR 标识、发布及 reconciliation 状态 |
| owner_sessions | 拥有者认证会话、到期时间与撤销状态，不建立多用户角色体系 |

大日志、快照与验证材料放在专用证据卷，数据库保存相对标识、长度和 SHA256；原始敏感输出只在内存中脱敏后落盘。工作区可改，基线和证据由控制服务持有。外键、唯一约束及事务防止跨项目读取和重复激活。备份必须覆盖数据库与同一时点的证据卷。

### 5. Policy, trust and approval binding

执行顺序固定为预算检查 -> 严格参数校验 -> 确定性策略 -> 需要时 Jev -> 必要时人工 -> 执行前重查。所有工具调用使用这个入口，包括来自模型、恢复流程和记忆处理的调用。

确定性禁止包括工作区外访问、控制凭据泄漏、Docker 管理操作、跨任务读取、未授权仓库发布、强制覆盖/主分支推送/自动合并。明确低风险读取可省略 Jev；已有授权内的局部代码修改和可信容器命令按照版本化风险策略处理，不把命令名称白名单误当成完整语义安全分析。

项目可信范围可明确认可本人维护分支；外部贡献者代码只按被确认的提交认可。静态分析阶段不导入 Python 模块、不运行包构建、安装钩子、Git 钩子或仓库配置脚本。代码更新越出认可范围后重新确认。

审批类别包括 action、code_trust、acceptance、memory_conflict 和 clarification。具体动作审批哈希覆盖 task、仓库、规范化参数、base SHA、工作区树、授权/策略版本；有效回复绑定该哈希。用户修改动作后生成新的摘要并重新治理，不能把编辑当作批准旧动作。系统硬拒绝不会因为一个批准回复而被绕过。

补充信息与验收确认经专用接口归档，模型、日志和记忆不能构造这些用户确认。审批等待期间不继续执行其他工具，也不保留正在运行的仓库进程。

### 6. Executor and public network

Compose 控制平面由 `api`、`worker`、`postgres` 和 `executor-broker` 组成；增加不持有凭据的 `egress-proxy`。网页由 API 服务提供，避免额外 Node 构建服务。执行 broker 是受信任基础设施，只暴露任务创建、限定工作区操作、命令执行、状态查询和停止；不接受原始 Docker 参数或任意宿主路径。

只有 broker 可以访问 Docker 管理 socket，API、模型工具和任务容器均不可访问；broker 接口用服务凭据认证且不对公网映射。worker 持有 GitHub/模型凭据，broker 只有自身管理凭据，不把它们传入任务。控制网络与任务网络分离，数据库无宿主公开端口。

每个任务独立容器、任务卷和网络；非 root、全部 capability drop、no-new-privileges、只读镜像根目录、受限 tmpfs、默认 seccomp。首发默认 2 CPU、4 GiB 内存、128 pids；普通命令 120 秒、准备命令 600 秒，用户可在实例资源上限内配置。任务活动时间默认 30 分钟、主循环 30 轮；人工等待不消耗活动时间，但不存在后台继续运行命令。

任务容器只获得自己的代码和工作目录，不获得原仓库 `.env`、SSH 材料、宿主目录、控制数据库或 Docker socket。Git 元数据只用于本地差异，没有带凭据的远程地址；archive 解包和编辑拒绝路径穿越、特殊文件及越界链接。不会沿用 Waku 的宿主自动运行路径。

公网默认通过 HTTP/HTTPS forward proxy 与 CONNECT 通路访问，其他协议明确报告不支持。任务网络禁止直接外连；代理每次连接校验解析后的全部目标地址并固定连接地址，拒绝 loopback、private、link-local、multicast、云元数据和控制/宿主网段，同时处理 IPv6、映射地址、重定向和 DNS 重绑定。代理不持有服务凭据。网络自检覆盖直接连接、代理、`host.docker.internal` 和元数据目标；失败时 broker 禁止启动命令。

普通 Docker 隔离只用于已认可代码，不声称防御恶意容器逃逸。依赖安装同样会执行代码，用户对仓库的认可与受限执行环境都必须在安装前生效。允许公网意味着容器内项目内容有外发可能，凭据隔离不能消除这种风险。

### 7. Reproduction and independent acceptance

准备优先识别 `pyproject.toml`、`requirements*.txt` 和已有 lock；支持 pip/pytest/Ruff。带 `uv.lock` 的项目由固定 runner 镜像中的受版本控制 uv 工具处理，不要求宿主或 Conda 安装 uv；无法遵守锁定环境时报告阻塞，不假装已复现。GPU、生产凭据、交互式服务和其他语言 toolchain 不作为首发承诺。

改代码前生成 reproduction bundle：问题摘要、base SHA、运行镜像 digest、依赖快照、命令、测试收集清单、相关失败及固定复现材料。提议的复现必须实际在 base 失败且与问题有关；环境错误不能代替缺陷失败。

材料由控制平面保存，Agent 不能改写。修改后在修复代码上重新执行同一 bundle；pytest 验证使用受控配置，固定材料与项目可修改测试隔离，检查用例收集和 skip/xfail 状态。基线测试与补充复现是独立证据来源，但自动生成检查本身不等于正确业务规格：关联关系无法证明时请求用户澄清。

Agent 可自动新增或修改仓库测试；差异分析标注删除、skip、xfail、断言放宽和范围变化。旧检查已过时的任务通过 acceptance 审批确认新条件，再冻结新 bundle，并保留原记录。回归命令在首次成功推断后固定；模型只能提出变更，缩减验收范围需要确认。发布门槛为相关基线失败 + 固定独立复现通过 + 回归有效通过 + 当前内容仍匹配验证快照。

### 8. Jev protocol, thresholds and degradation

Jev 通过独立 `httpx` 适配器调用官方 `POST https://api.typesafe.ai/v1/systemone`，Bearer 密钥只在 worker。使用 `state`、`questions`、`model` 请求与 `answers`、返回 `model`、`usage` 响应，不套入 ChatOpenAI。详见 [TypeSafe API](https://docs.typesafe.ai/api)。

| 问题 | 输入 | 首发结果空间 |
| --- | --- | --- |
| action_risk | 授权摘要、动作、风险特征、相关代码/输出片段 | choice: allow / deny / review |
| intervention | 当前目标、阻塞、可选下一步、已有证据 | choice: continue / clarify / review |
| memory_write | 候选、证据、确认引用及冲突摘要 | choice: retain / discard / review |
| memory_read_needed / memory_relevant | 当前任务、当前版本及候选摘要 | noul: 需要检索或候选相关的概率 |

这四类问题各有问题版本，只有共享同一证据且相互独立的问题可以批量请求。主模型负责提取候选和搜索词，Jev 不编造事实、代码或自由文本解释。UI 展示证据、概率和程序路由原因；若生成解释，明确标注其来源。

严格校验题目集合、类型、选项全集、数值有限性与范围、分布和实际模型。Choice 分布和为 1 的容差固定在 adapter 契约测试；Noul 不添加供应商没有返回的 confidence。[TypeSafe confidence](https://docs.typesafe.ai/confidence) 中的统计不是实测准确率。

每次请求超时默认 10 秒，最多 2 次重试，仅对可重试网络错误、429、529 和服务端暂时错误采用有抖动退避；401/422 与无效结构不重试。预算包括全部尝试。缓存键包含问题、策略、输入哈希、代码/证据版本及实际模型；别名下跨任务不复用，模型版本变化进入待校准状态。

启动默认 conservative 策略：硬规则确认的安全读取继续；其他依赖语义判断的执行请求人工；记忆保留待审候选但不自动激活。只有带数据集和模型版本的校准配置才能启用对应自动路径。Noul 不确定时不检索未经批准候选；可使用当前任务证据和用户明确指定的活跃记忆，并显示降级。没有所谓通用安全阈值。

### 9. Memory lifecycle and context

记忆候选从当前任务验证和用户确认中提取，先做结构、敏感性、证据、去重和冲突检查，再经 Jev 价值判断。唯一来源指纹避免重复整理，版本写入与状态更新在应用数据库事务内完成。

状态为 `task_local / pending_review / pending_merge / active / rejected / revoked / superseded`；任务内候选供来源任务参考，不进入其他任务。PR 合并状态由 worker 默认每 5 分钟轮询已知 PR，无需 webhook 公网入口；关闭未合并不会激活。支持手动触发 reconcile。

合并后不能仅比 SHA：GitHub 可 squash/rebase，必须检查来源文件 blob、确认引用和验证依据是否仍适用。不同则重验或审核；自动激活只适用于有证据、无冲突且校准决策允许的事实。设计选择还必须存在有效用户确认。未发布的任务产生的代码事实保持 task_local；用户直接确认、与分支无关的项目约束按当前项目证据审核记录。

首发使用 PostgreSQL 全文检索与精确标签过滤，不引入向量数据库或 embeddings。先限定 project/status/version，再由记忆读取门控和候选相关性筛选进入上下文，记录选入/排除及依据。中文与代码标识初期以 simple 分词加规范化关键字和标签支持，检索不足在评测中呈现。

上下文预算由模型窗口与配置计算，预留输出空间。原始事件追加保存；摘要带来源 event range、版本和被引用证据。保留授权、当前任务、审批、关键失败与工具调用结果配对，较大日志使用必要片段和证据链接。摘要只组织上下文，不替代 checkpoint 或被当作新的用户指令。

### 10. GitHub integration, APIs and owner access

GitHub 使用服务端细粒度 PAT，登记仓库时检查 contents 和 pull requests 权限及固定仓库标识。下载指定提交的 archive，在受控解包后形成任务快照；通过 Git Database API 创建 blob/tree/commit/ref，通过 PR API 建立草稿，不把 Token 交给 git 命令或任务容器。提交作者使用实例配置，不冒充未配置身份。

分支固定为 `decagent/<task-id>`，发布记录包含 base/head、内容哈希和稳定任务标识；冲突不 force push。主分支推进时重新检查可交付性，出现冲突或相关检查依据变化则重新验证。恢复按同一分支与标识核实已存在的提交和 PR。对 closed-unmerged PR 不自动重开或新建替代 PR。合并始终由用户在 GitHub 完成。

HTTP API 使用 `/api/v1`，错误统一为 `code / message / task_id / details`，details 先脱敏。创建及状态变更接受 `Idempotency-Key`；重复且内容相同返回既有结果，键相同内容不同返回 409。核心接口：

| 接口 | 契约 |
| --- | --- |
| POST /auth/session, DELETE /auth/session | 拥有者登录/注销；不提供用户注册 |
| GET/POST /projects | 仓库登记及可信范围；返回权限诊断与配置版本 |
| POST /tasks | project_id、base_ref、issue、trust_scope、预算及 publish_draft_pr |
| GET /tasks/{id} | 持久化状态、阶段、预算、审批和交付引用 |
| GET /tasks/{id}/events | SSE 按 task seq；支持 Last-Event-ID 恢复 |
| GET /tasks/{id}/diff, /verification | 差异与验证记录，不执行代码 |
| POST /tasks/{id}/cancel, /resume | 取消、预算暂停恢复；终态恢复返回冲突 |
| POST /tasks/{id}/responses | 带待补充问题标识的澄清回复 |
| POST /approvals/{id}/reply | expected_digest、approve/reject/edit/confirm、修改参数或确认内容 |
| GET /projects/{id}/memories | 候选和活跃记忆、来源和版本 |
| POST /memories/{id}/review, /revoke | 审核冲突/适用性或撤销；不绕过合并限制 |
| GET /config, POST /diagnostics | 无密钥配置摘要；明确区分本地检查与用户发起的真实诊断 |

拥有者凭据由 `decagent init` 生成，凭据文件采用本地权限保护；服务端只保存高熵登录令牌摘要。网页使用 HttpOnly、SameSite=Strict session cookie，状态变更校验 CSRF token 和 Origin；远程部署要求 HTTPS，只有 loopback 开发访问允许 HTTP。CLI 使用 bearer 认证，凭据不作为命令行参数写入审计。

模型/GitHub/Jev 密钥通过环境或 Compose secrets 配置，UI 只显示已配置状态，不能读取原值。旋转凭据通过配置与重启控制服务完成；非敏感模型配置可更新并版本化，新任务使用新配置，运行中任务不静默切换。SSE 只传脱敏持久化事件；高频 token 可暂存显示，不把连接重连当作任务重启。

### 11. Dependencies, budgets and evaluation

DecAgent 支持 Python >=3.11，发行 runner 默认 Python 3.11；CI 检查 3.11 与 3.13。当前名为 Agent311 的 Conda 环境不据名称判断版本。项目继续支持 `python -m pip install -r requirements.txt`：运行依赖来自 pyproject，锁定约束与测试过的版本由实施阶段解析生成，不依赖宿主 uv。

新依赖集中为 LangChain/core、LangGraph、langchain-openai、langgraph-checkpoint-postgres、FastAPI/Uvicorn、psycopg 及连接池、httpx、dotenv；Pydantic 使用框架已有依赖，CLI 延续 Rich。可观测性默认数据库审计和本地导出，LangSmith/OTel 不作为强制服务。pytest/Ruff 为开发依赖；不引入通用插件集合、商业 SDK 或向量库。

所有主模型、Jev、候选提取和压缩调用记录使用量；模型请求前按已配置价格版本及最大 tokens 预留预算，失败且费用未知时保留预留，不按零费用继续。费用上限和最大调用数由用户给定；未配置价格时只能在明确调用/token 预算和未知费用提示下运行任务，付费基准必须先配置价格与费用上限。费用是基于价格表的控制估算，不宣称等于最终账单。

三种可替换策略为 rule、main-llm、jev。它们共用相同授权、工具、硬拒绝、候选库和验收条件，只替换语义判断。初期建议人工标注 120 个判断样本，四类问题各 30，分层切分校准与保留集；真实修复初期 12 个可重现缺陷，覆盖至少 3 个许可允许的 Python 仓库。样本数是实验默认提案，实际运行 manifest 在花费前由用户确认，可减小但必须报告规模。

样本保存许可、来源、base SHA、问题、固定验收与人工标签；不使用被评测模型自动产生真值。对照保留错误和预算中断，报告误放行、误中断、候选写入/读取质量、完整修复成功率、P50/P95 延迟和调用成本。少量一次运行只支持初步观察，不声称统计显著改善。测试层级为离线单元、PostgreSQL/容器集成、显式 live 和完整交付验收；未运行的层级独立显示。

### 12. GHCR release and Windows local deployment

版本标签采用 `vMAJOR.MINOR.PATCH`。独立 release workflow 在 GitHub 托管 runner 上复用完整 CI；只有测试全通过且标签提交可从 `main` 到达时，才构建并发布 control、executor、runner 三个镜像。每个镜像发布版本标签和完整 commit SHA 标签，部署使用 registry 返回的 digest 而不是可变标签。GitHub token 按 job 最小授权：发布 job 仅有 `packages: write`，部署 job 仅有 `packages: read`。

本机 Windows Docker Desktop 通过仓库级自托管 Runner 执行最后一步。Runner 只匹配 `v*` 发布 workflow，并置于限制为版本标签的 `local-deploy` environment；Pull Request 和普通 CI 永远使用 GitHub 托管 runner。Runner 以当前用户登录任务启动，等待 Docker Desktop Linux engine 可用，不注册 Windows 服务，因此用户登出或 Docker Desktop 未启动时部署会排队或失败。该 Runner 可访问本机 Docker daemon 和 Compose 配置；由于仓库公开，仍需把受信任发布 workflow 的修改视为高权限变更。

部署脚本使用现有 `.env`、secret 文件、Compose project name 和 `compose.windows.yaml`，不改写凭据文件，不执行 `down` 或删除卷。运行迁移前从现有 PostgreSQL 容器导出本地备份；镜像更新后等待 Compose health 状态并探测本机 API。失败时 workflow 返回失败并保留备份；不会盲目自动恢复数据库，因为迁移可能不向后兼容。上线和恢复仍由备份与数据库兼容性策略控制。

## Risks / Trade-offs

- [模型判断受到恶意输入影响] -> 输入最小化、来源标识、硬拒绝先行、人工保留集与错误追踪；Jev 不拥有执行权限。
- [shell 命令语义无法被规则完整证明] -> 仅执行已认可代码、限制环境和凭据、语义风险审核；不宣称支持恶意代码安全运行。
- [公网允许项目内容外发] -> 仅挂载任务必要数据、阻断控制网络和秘密；用户认可代码仍需承担其公网行为边界。
- [普通 Docker 与 broker socket 的宿主权限] -> broker 独立且接口最小、无公网入口，不允许任务触及管理接口；边界自检失败则停止执行。
- [公开仓库上的自托管 Runner 持久化风险] -> PR 与普通 CI 仅使用 GitHub 托管 runner；本机 runner 仅处理 `main` 可达的版本标签、最小包读取权限和本地部署步骤。Docker daemon 仍具有高权限，必须保护 release workflow 与本机账户。
- [自动生成的验证可能误解业务] -> 保留相关基线、独立复现及测试变化记录；验收无法证明时请求用户确认。
- [模型或策略升级导致旧阈值失效] -> 实际模型、问题和数据集版本绑定校准；升级先回到保守策略。
- [外部请求已完成但本地未知] -> 执行凭据和 reconciliation；无法核实时人工处理，不承诺 exactly-once。
- [长期记忆随代码变化失效] -> 合并后激活、来源 blob 检查、冲突审核、撤销和检索适用性过滤。
- [部署依赖较原 Waku 增多] -> 单 PostgreSQL、静态网页、无 Redis/Node 服务；提供完整诊断和备份演练。
- [脱敏无法证明识别一切秘密] -> 已知密钥精确过滤、敏感路径排除、模式检测、受限日志；对不确定敏感内容停止外发并请求处理。

## Migration Plan

1. apply 时建立独立目标，记录继承来源与许可，生成配置模板和可用的 pip 安装路径；原 Waku 只读参考。
2. 先用注入模型和隔离小型 Python fixture 验证任务与执行，再接真实 GitHub 和 Jev；fixture 验证不能替代真实接入验收。
3. 逐阶段完成 tasks 中的持久化、治理、复现、记忆和交付，所有阶段通过对应回归后推进。
4. 首次部署执行迁移、拥有者初始化和执行边界诊断；真实模型与 GitHub 发布使用用户指定凭据和授权测试仓库。
5. 升级前停止接新任务、等待命令结束或取消，备份数据库与证据卷；记录 graph/schema 版本，不能使用新图强行恢复不兼容旧 checkpoint。
6. 回滚优先使用兼容旧 schema 的旧服务镜像；不兼容时在新数据卷恢复已核实备份，保留故障卷。所有可能清空或替换已有运行数据的操作单独确认，不提供默认重置命令。

## Open Questions

以下是实施或实测时才需提供的实例参数，不改变规格和任务划分：用户最终的 GitHub 远程、授权测试仓库、主模型与 TypeSafe 凭据、首次实验费用上限及公开部署域名。未提供凭据仍可完成离线和本地集成开发；真实接入与外部交付必须保持未验证状态，不能用占位实现宣称完成。
