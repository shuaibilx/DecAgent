---
title: 业务 Agent 架构详解
type: explanation
audience: [A2, A3, A4, A5]
runs: no
verified_on: 2026-10-13
applicability: 当前 business-1 图、受信任插件和 service-tickets 1.0.0；保留 repository_repair
verification_scope: 源码与 CLI 契约核对；本机部署见0900验收记录；图表达实现关系，真实模型提效与独立Linux待办
sources:
  - decagent/business/contracts.py
  - decagent/business/registry.py
  - decagent/business/questions.py
  - decagent/business/calibration.py
  - decagent/business/planner.py
  - decagent/business/actions.py
  - decagent/business/execution.py
  - decagent/business/services.py
  - decagent/business/graph.py
  - decagent/business/worker.py
  - decagent/business/runner.py
  - decagent/business/tickets/store.py
  - decagent/gateway/business.py
  - decagent/runtime/worker.py
  - decagent/storage/005_business_tasks.sql
  - decagent/storage/006_service_tickets.sql
  - decagent/evaluation/business/harness.py
  - compose.windows.yaml
  - ops/local/start.py
---

> 历史版本说明：本页保留 CLI / owner 业务路线的当时内容。当前业务已迁移为员工身份下的 MCP + Skill；请使用[当前指南](../261014/1646_MCP业务Agent使用指南.md)和[当前架构](../261014/1646_MCP业务Agent架构详解.md)。

# 业务 Agent 架构详解

DecAgent 让主模型和 Jev 使用同一份业务契约，但把“判断什么”“能否执行”和“是否完成”交给不同环节。
当前默认业务是本地合成服务工单：查目录、创建、分派、评论和状态变更。
主模型规划复杂目标，Jev 判断程序提供的有限选项，程序校验每次提案并核实实际回执。

当前源码包含非空 `W⊆C⊆O` 完成证据及 `goal_unproven`，以及严格拒绝响应用量、安全模型归属、可信未派发零结算与持久化不重发。修复后综合回归和 9 次真实功能探测见[现版本验证](../261013/0158_现版本修复与功能验证.md)。[完整 720 次对照](2341_业务固定集完整实测.md)属于修复前版本，严格 not_passed；当前六类 Jev 快路线禁用，不能由该对照或小样本宣称 Jev 提效。

图册提供相同主题的[离线交互视图](0902_业务Agent架构图册.html)。
部署和操作见[使用指南](0852_业务Agent使用指南.md)；字段查阅见[API 与规划协议](../261011/2349_业务API与规划协议.md)。
本页描述源码，不把模拟模型、历史修复验收或本机容器视为真实业务提效证据。

## 能力如何成为契约

管理员登记的是已经安装且可信的 JSON CLI。清单冻结插件 ID、version、entrypoint、
环境白名单、每条命令的输入/结果 Schema、read/write 效果和确认要求。
registry 校验并保存 manifest_digest，业务任务创建时把同一摘要放入 authorization_snapshot。
启用状态或摘要变化后，旧任务不能继续使用原能力。

basic 清单提供可调用能力；enhanced 额外提供版本化工作流、合法状态、问题、评分尺、
参数来源和完成约束。主模型工具与运行校验由同一清单生成，避免“模型看到的参数”与
“程序实际允许的参数”分裂。未知字段、开放输入对象、无效本地引用、外部 Schema 引用均受注册校验。

插件 Skill 只提供资料，不授权任意命令、shell 或 SQL。
runner 的 entrypoint 来自可信登记；用户自然语言不能指定新脚本或新增环境变量。
源码：[contracts.py](../../decagent/business/contracts.py:399)、[registry.py](../../decagent/business/registry.py:10)、
[planner.py](../../decagent/business/planner.py:42)。

## 总体组件与运行边界

```mermaid
flowchart LR
  U["拥有者"] --> UI["默认业务网页 / CLI"]
  UI --> API["Gateway：owner / Origin / CSRF / 幂等"]
  API --> DB[("PostgreSQL：app + checkpoint + business")]
  API --> CTRL["BusinessControl：登记资料和当前只读核对"]
  DB --> BW["Windows BusinessWorker"]
  BW --> G["business-1 LangGraph"]
  G --> S["BusinessServices"]
  S --> REG["冻结插件契约"]
  S --> RULE["确定性规则"]
  S --> JEV["Jev：Choice / Noul / Score"]
  S --> LLM["主模型：合法工具提案"]
  S --> GOV["程序治理 / 动作日志 / 回执核实"]
  GOV --> CLI["Windows CliRunner：可信 JSON CLI"]
  CLI --> TS["独立 TicketStore"]
  TS --> DB
  DB --> RW["Docker repository_repair Worker"]
  RW --> BROKER["修复 broker / 隔离 runner"]
  RW --> DELIVERY["控制端 DeliveryService"]
  DELIVERY --> GH["授权 GitHub 草稿交付"]
```

业务 worker 不建立 GitHub、修复 broker 或仓库工具。
主模型直到进入 plan 才初始化；查询和精确规则任务可以不调用模型。
同一 API 保留修复回调，修复 HTTP 与 broker 客户端延迟到使用时建立。
GitHub 发布由控制端 DeliveryService 执行；broker 和隔离 runner 不持有 GitHub 凭据。

Windows 当前通过 Job Object 控制 CLI 子进程树和数量；非 Windows runner 返回 unsupported_platform。
Linux 显式 `worker --kind business` 在领取任务前拒绝启动。
Docker 中的 PostgreSQL 和修复 worker 可运行，但不能据此宣称业务 CLI 在 Linux 可用。
独立 Linux 适配与主机部署验收仍是待办。

## 两个任务类型共享什么

005 迁移增加 task.kind 和 business_scope_id；旧任务默认 repository_repair。
修复任务仍要求 project_id、base_sha 且没有业务作用域；业务任务必须有作用域，
project_id/base_sha 为 NULL。业务事件以 task 外键关联，没有伪造仓库。

共享设施包括任务状态、预算、租约、事件、action、approval、usage 和 PostgreSQL checkpoint。
业务图版本是 business-1，修复图使用其原有版本；兼容性按 kind 检查。
checkpoint.thread_id 为 task ID，恢复同时检查任务身份和图版本。

Windows 未指定 worker kind 时串行交替两类队列；Linux 默认只运行修复 worker。
本机部署把 Docker worker 明确限定为 repository_repair，Windows worker 明确限定为 business。
这避免两个进程争抢同类队列，也保留旧任务和修复执行边界。
源码：[005 迁移](../../decagent/storage/005_business_tasks.sql)、[worker 分派](../../decagent/runtime/worker.py)。

## LangGraph 的业务闭环

下面是逻辑流，不表示源码添加了这些静态边。
源码只有 START→prepare 静态边，后续由每个节点的 Command 指定唯一目的地，避免两条路线同时运行。

```mermaid
flowchart TD
  START(["开始 / 原 checkpoint 恢复"]) --> PREP["prepare：查目录与当前工单"]
  PREP --> DEC["decide：按策略和既有结果选择路线"]
  DEC -->|"精确规则 / 校准后的判断"| VALID["validate：Schema、ID、状态、revision、请求意图"]
  DEC -->|"复杂 / basic / 不确定"| PLAN["plan：主模型提出注册动作"]
  PLAN --> VALID
  VALID -->|"拒绝 / 目标不成立"| FIN["finalize：核对目标与持久回执"]
  VALID -->|"缺事实 / 需确认"| HUM["human：持久问题"]
  HUM --> WAIT["human_wait：checkpoint + interrupt"]
  WAIT -->|"批准 / 改提案 / 澄清"| VALID
  WAIT -->|"澄清后重读事实"| PREP
  WAIT -->|"拒绝"| FIN
  VALID -->|"有效当前授权"| EXEC["execute：租约围栏 + 稳定 operation_id"]
  EXEC --> VER["verify：receipt + 输入摘要 + 工单历史"]
  VER -->|"核实成功"| DEC
  VER -->|"失败"| FIN
  VER -->|"结果未知"| REC["reconciliation_wait：只读核实等待"]
  REC -->|"拥有者恢复"| VER
  PLAN -.->|"BudgetExceeded"| BUD["budget_wait：明确新预算"]
  EXEC -.->|"BudgetExceeded"| BUD
  BUD -->|"重查预算后回原节点"| EXEC
  FIN --> END(["任务 succeeded / failed"])
```

图中的 budget_wait→execute 只展示一种恢复目标；实际 wait_target 保存原阶段，
也可以是 plan、validate、human_wait 或 verify。
拒绝、非法规划、未知回执和阶段上限均不能用完成文本变成 succeeded。

图累计最多 128 个自主阶段，恢复不会重置计数。
上限处若有未知写入，保留 reconciliation 等待；明确恢复后只能 verify 原回执。
核实后按上限结束，不重新规划或新增写入。
源码：[graph.py](../../decagent/business/graph.py:48)、[services.py](../../decagent/business/services.py:90)。

## 泳道：创建并分派

```mermaid
flowchart LR
  subgraph OWNER["拥有者"]
    U1["创建并分派目标"] --> U2["必要时补部门 / 人员 ID"]
  end
  subgraph CONTROL["API 与持久控制"]
    A1["冻结 scope / digest / strategy / budget"] --> A2["排队与租约领取"]
    A3["认证回复 / 事件 / checkpoint"]
    A4["展示动作和完成证据"]
  end
  subgraph AGENT["判断与规划"]
    P1["读取真实目录"] --> P2["复杂目标进入主模型"]
    P2 --> P3["create 提案"]
    P4["保留创建结果与 action ID"] --> P5["assign 提案"]
    P6["逐步完成校验"]
  end
  subgraph EXECUTION["治理与独立 CLI"]
    X1["校验创建"] --> X2["写入 + 历史 + receipt 同事务"]
    X3["核实 create"]
    X4["核对人员和工单 revision"] --> X5["分派 / 必要确认"]
    X6["核实 assign"]
  end
  U1 --> A1
  A2 --> P1
  P3 --> X1
  X2 --> X3 --> P4
  P5 --> X4
  X4 -.->|"同名 / 缺事实"| A3
  A3 --> U2
  U2 --> A3 --> P5
  X5 --> X6 --> P6 --> A4
```

已创建工单会保留在后续规划上下文中。澄清人员后不重复创建。
完成要求创建和分派回执引用同一工单；工单仍可处于 assigned。
业务 Agent 没有实际设备维修证据，因此不能擅自写解决记录或关闭工单。

## Jev 的三个类型怎样使用

| 类型 | 当前问题 | 选项/尺子从哪里来 | 结果用途 |
| --- | --- | --- | --- |
| Choice | operation、person、category | 清单命令经状态过滤；directory 实际 ID、姓名、团队；含 no_match | 选择单一合法操作或实体 |
| Noul | explicit_assignee、multi_person | enhanced 契约内独立命题 | 判断用户是否指定处理人、是否多人受影响 |
| Score | work_blockage | 契约有序情境：未影响、可绕过、工作受阻 | 新工单优先级建议 |

Noul 是命题成立概率，max(p,1-p) 用于确定性判断；p 接近 0.5 不自动采用。
Score 保留有序分布及其加权位置，工单尺度为 0 到 2：
[0,0.5)→normal、[0.5,1.5)→high、[1.5,2]→urgent。
分数不授权修改已有工单，不表示业务已解决。

实际 API 会独立舍入概率、score 和 confidence。校验保留原值，并要求存在同一概率分布同时满足
各字段的精度区间、总和及最高等级约束，不能分别放宽到互相矛盾的数字。
细节见[三类决策格式与评分尺子](../261011/1900_三类决策格式与评分尺子.md)。

程序把同一状态下独立问题批量发送，问题不会把其他答案当输入。
人名重复不会合并：两位张伟保留不同 ID 和部门；缺少唯一支持时选择 no_match。
非法状态、复合请求或没有明确操作也不能强选某个命令。
自由文本标题、评论和解决记录来自用户或主模型保留原意的提案，Jev 不生成任意字符串参数。

```mermaid
flowchart TD
  C["冻结 enhanced workflow"] --> Q["合法命令 / 独立命题 / 有序评分尺"]
  D["实际 directory + ticket + 来源摘要"] --> Q
  Q --> B["同一状态批量 Choice / Noul / Score"]
  B --> V["严格回答校验：类型、候选、分布、实际模型"]
  V --> CAL["兼容 profile + 每题 certainty 阈值"]
  CAL -->|"当前分支需要的答案被接受"| MAP["映射合法参数提案"]
  CAL -->|"缺失 / 不确定 / 版本变更 / 服务异常"| MAIN["主模型规划"]
  MAP --> GOV["同一程序治理"]
  MAIN --> GOV
  GOV --> CLI["经授权执行，独立核实"]
```

profile 绑定 actual model、plugin_digest、workflow/question/policy/dataset version。
请求别名变化不替代实际模型核对；目录证据摘要绑定每次请求。
其他增强插件仍可构造类型化问题，但服务工单的专用参数映射不会套在不同题型或评分尺度上。
它们进入主模型规划。
源码：[questions.py](../../decagent/business/questions.py:65)、[calibration.py](../../decagent/business/calibration.py:36)、
[services._typed_route](../../decagent/business/services.py:192)。

## 校准说明的有效边界

业务 profile 与历史仓库修复的人类标注 profile 分开。
当前参考数据是 synthetic-reference，开发集 40、固定验收集 80；
“开发集接受答案零已观察错误”只说明拟合条件，没有独立人工标注或泛化保证。

开发拟合逐题选择在零已观察错误下尽可能保留覆盖的阈值，不能可靠采用的题禁用。
每条记录以自己的合法候选和状态校验，不能拿第一条请求的候选验证全部样本。
验收集禁止用于调阈值；部分、漂移或未知必要 usage 的开发采集不能产出有效 profile。
缺失/不可读/不兼容校准时记录原因并降级到有预算的主模型。

## 主模型能提议什么

PlannerProtocol 用契约输入 Schema 生成 plugin 工具，外加
decagent_ask_user 和 decagent_finish 两个控制。
主模型可以组织复杂步骤、保留用户自由文本、询问缺失事实和提出完成摘要。
模型没有任意 shell、SQL、插件登记或新增权限工具。

未知工具、非法参数、无效 finish 被拒绝。
无效规划最多两次，再询问拥有者；不是无限重试。
finish 的声明集合 C 必须非空，满足 W ⊆ C ⊆ O：O 是本任务全部已核实操作，W 是按注册命令 effect 判定的全部写入。辅助 directory/get/receipt 读取可以不列入 C；写入不能遗漏，陌生 ID 不能混入。最终核实仍使用完整 O 及 app.actions 的成功回执、目标、精确参数和单一结果资源，不把证据缩减为 C。
程序不能从现有目标识别或注册 literal 命令建立目标时，完成声明返回 goal_unproven 和程序给定摘要，保留所有动作且不追加副作用。当前目标识别是有限规则，不等于任意自然语言目标都可自动证明；未知目标应写成明确注册操作，或使用精确 JSON。已有拒绝、取消及执行失败仍保留对应程序原因。
精确 JSON 请求还核对原命令与参数；模型不能偷偷换标题、对象或内容后保留自动授权。

## 响应被拒绝时怎样记账

提案合法性与用量可靠性分别判断。完整响应或正常结束流的所有提案被拒绝时，`PrimaryResponseRejected` 只携带固定 reason、严格用量和安全原始模型名；不保存工具参数、原始响应或原异常正文。三个 token 字段必须是非负整数且总数相符，布尔、字符串、浮点与修补总数均不接受。流式用量和模型名在 LangChain 合并前独立检查，避免强制转换及字符串拼接改变证据。

```mermaid
flowchart TD
  R["持久化调用与费用预留"] --> P{"可信本地预检阻止派发？"}
  P -->|"是"| Z["结算 0 token / 0 费用；保存 not_dispatched"]
  P -->|"否"| CALL["调用供应商"]
  CALL --> END{"完整响应或正常结束流？"}
  END -->|"传输中断 / 未结束"| U["用量与费用 unknown；保留预留"]
  END -->|"完整"| V{"严格用量合法？"}
  V -->|"缺失 / 非法"| U
  V -->|"合法"| PRICE{"既有价格已知？"}
  PRICE -->|"是"| KN["按实际 token 和版本化价格结算"]
  PRICE -->|"否"| KH["记录已知 token；费用 unknown，保留预留"]
  KN --> G["提案另行校验；无效则拒绝全部"]
  KH --> G
  Z --> E["保存安全失败；不自动重发旧修复或诊断"]
  G -->|"被拒绝"| E
```

可信本地预检的零结算不适用于供应商抛出的异常；客户端伪造相同异常类型也不能证明请求未发送或已完整结束。业务规划继续执行原有两次失败后询问策略；旧修复保存 rejected 并等待核实，重启同一上下文不重发；主模型诊断保存 failed，同一 operation_id 复用结果。未知价格保留已知 tokens 的记录，费用预留仍在。用户确认的千问零费率需要真实用量，不能推导历史缺失 token。

## 确认如何约束一次写入

动作摘要绑定 task、scope、plugin digest/version、policy、命令、完整参数、资源
ID/revision/status/assignee 和 instruction digest。
确认从认证拥有者回复得到，checkpoint、模型“允许”文字或 Skill 都不产生执行权。

| 操作情形 | 程序处理 |
| --- | --- |
| 已校验只读 | allow |
| 明确新建、首次分派、开始、评论 | 状态、实体、Schema 和请求意图通过后可 allow |
| 改派、解决、关闭、重开、已有 priority 变更 | confirm |
| 用户未明确请求的模型写入 | confirm |
| 同名/缺少真实对象 | clarify |
| 状态非法/伪造 ID/越权字段 | deny |
| revision 或审查事实变化 | stale，刷新重评 |

编辑回复提供完整参数，原动作不执行；生成新提案并强制再次确认。
重复相同回复返回已有结果；不同内容返回冲突。
执行前重读目录和资源，不能只依赖提交时的人员候选。
源码：[actions.py](../../decagent/business/actions.py:67)、[BusinessControl.reply](../../decagent/gateway/business.py)。

## 时序：需确认的写入

```mermaid
sequenceDiagram
  actor U as 拥有者
  participant API as Gateway
  participant DB as PostgreSQL
  participant W as BusinessWorker
  participant GOV as BusinessServices/Executor
  participant CLI as Ticket CLI
  U->>API: 提交业务目标、scope、预算
  API->>DB: 冻结任务契约并排队
  W->>DB: 领取 task + lease_generation
  W->>GOV: validate 当前提案
  GOV->>DB: 保存 action 和 approval
  W->>DB: checkpoint human_request 后 interrupt
  API-->>U: 展示命令、参数、revision、digest
  U->>API: approve + expected_digest
  API->>CLI: 只读刷新 directory/get
  API->>DB: 保存认证 policy_record，排队
  W->>DB: 读取当前审批，Command resume
  GOV->>CLI: 执行前刷新资源
  GOV->>DB: 锁任务/动作，检查租约、授权、预算
  GOV->>CLI: 原 operation_id 写入
  CLI->>DB: 工单、历史、receipt 同事务
  CLI-->>GOV: 成功或未知响应
  GOV->>CLI: receipt 查询，再 get 历史
  GOV->>DB: 保存 succeeded + 核实证据
  W->>DB: checkpoint 后续阶段，核对目标
  API-->>U: 持久结果、事件、回执
```

批准时和执行时都会核对事实，发生变化后必须重评。
写入返回 ok 不是任务完成；receipt 的输入绑定和资源历史都要符合。
机器 active_seconds 不包括等待拥有者的时间。

## 未知结果怎样恢复

```mermaid
flowchart TD
  R["重启 / 租约代次接管 / 取消后查看"] --> A["读取 app.actions"]
  A -->|"succeeded"| REUSE["复用原结果，禁止重复执行"]
  A -->|"planned 且尚未发送"| RECHECK["重查契约、资源、授权和预算"]
  A -->|"running / unknown"| RC["仅按原 operation_id 查 receipt"]
  RC --> BIND["核对 command + input_hash + resource_id + revision"]
  BIND -->|"匹配"| HIST["get 最新工单，核对历史中的记录 revision"]
  HIST -->|"核实"| SAVE["保存成功回执，继续原 checkpoint"]
  RC -->|"无回执能力 / 回执不存在"| WAIT["recovering：等待外部核实"]
  BIND -->|"冲突 / 不可信"| WAIT
  HIST -->|"历史不能证实"| WAIT
  WAIT -->|"明确恢复"| RC
  RECHECK -->|"摘要变化"| HUMAN["新提案 / 新确认"]
  RECHECK -->|"仍有效"| EXEC["发送原计划操作"]
```

operation_id 从 task、logical key 和 action digest 稳定派生。
TicketStore 把工单、历史和回执放进同一事务；同键同输入复用结果，
同键不同输入返回 operation_conflict。
进程超时、响应丢失或 checkpoint 保存前崩溃时，都不能因此换新键重复写。

旧 worker 失去租约后无权保存结果或开始新动作。
取消撤销租约并终止受控进程；后台已经提交的写入可能仍存在，应查原回执。
调用/token/费用额度用尽仍允许核实已发送操作；机器时间用尽先明确扩充时间预算。
任务达到 128 阶段后只保留明确恢复的回执核实，不继续自主业务。

## 工单生命周期

```mermaid
stateDiagram-v2
  [*] --> open: create
  open --> assigned: assign
  assigned --> assigned: reassign
  assigned --> in_progress: start
  in_progress --> assigned: reassign
  in_progress --> resolved: resolve + 用户解决记录
  resolved --> closed: close
  resolved --> open: reopen
  closed --> open: reopen
```

comment 和 set_priority 是附加操作，不单独产生生命周期新状态。
每次已有工单写入使用最新 expected_revision。
目录人员是业务实体，不能用作多用户登录身份。
工单关闭是业务状态，不等于 Agent 任务成功的统一终点。

## 数据实体与关联

实线表示数据库外键/归属；虚线表示快照或回执 ID 的程序核对，不表示 SQL 外键。

```mermaid
erDiagram
  BUSINESS_SCOPES ||--o{ TASKS : business_scope_id
  TASKS ||--o{ EVENTS : task_id
  TASKS ||--o{ ACTIONS : task_id
  TASKS ||--o{ APPROVALS : task_id
  ACTIONS ||--o{ ACTION_ATTEMPTS : action_id
  TASKS ||--o{ DECISION_RECORDS : task_id
  TASKS ||--o{ USAGE_RECORDS : task_id
  TASKS ||..o{ CHECKPOINTS : thread_id
  PEOPLE ||--o{ TICKETS : requester_or_assignee
  CATEGORIES ||--o{ TICKETS : category_id
  TICKETS ||--o{ TICKET_HISTORY : ticket_id
  TICKETS |o--o{ OPERATION_RECEIPTS : resource_id
  ACTIONS ||..o| OPERATION_RECEIPTS : snapshot_operation_id
  BUSINESS_SCOPES {
    uuid id PK
    string plugin_id
    string plugin_version
    string manifest_digest
    json manifest
    boolean enabled
  }
  TASKS {
    uuid id PK
    string kind
    uuid business_scope_id FK
    string graph_version
    json authorization_snapshot
    json budget
    integer lease_generation
  }
  ACTIONS {
    uuid id PK
    uuid task_id FK
    string logical_key
    string digest
    json snapshot
    string status
    json receipt
  }
  TICKETS {
    uuid id PK
    uuid category_id FK
    uuid requester_id FK
    uuid assignee_id FK
    string status
    string priority
    integer revision
  }
  OPERATION_RECEIPTS {
    string operation_id PK
    string input_hash
    string command
    uuid resource_id FK
    integer revision
    json result
  }
```

CHECKPOINTS 表示 LangGraph checkpoint 家族的概念节点，不是假定它只占一个 SQL 表。
USAGE_RECORDS 表示 app.usage_records 的持久调用预留及结算；工单 receipt 和 app.actions.receipt 是不同层：
前者证明业务事务提交，后者保存 Agent 已核实的结果。
business schema 由 006 迁移建立，备份包含业务数据与共享任务记录。

## 公平评测与实际边界

```mermaid
flowchart LR
  DATA["冻结 120 合成案例"] --> DEV["40 development"]
  DATA --> HOLD["80 holdout：48常见/16复杂/16边界"]
  DEV --> COL["真实开发采集"]
  COL --> FIT["逐请求候选校验 + profile"]
  FIT --> RUN["rule/main/hybrid 三轮交错：720 trial"]
  HOLD --> RUN
  LED["累计 Jev 账本：历史 $0.000253176，上限 $1"] --> COL
  LED --> RUN
  RUN --> E["CLI 回执、usage、机器时间、等待分离"]
  E --> REPORT["失败留在分母，unknown/未完成不能通过"]
```

三策略共用实际 BusinessServices、BusinessWorker、CliRunner、同一契约/Skill/目录和治理。
每个 trial 初始资源重建；参考标签仅用于评分和开发拟合，不进入规划上下文。
服务端缓存是否存在没有被证明；客户端 Jev cache_ttl=0、retries=0，任务之间不复用记录。

真实验收需要完整 80 案例×3 策略×3 轮且必要 usage 已知：
hybrid 正确率不低于 main、所有策略禁止写入为零、常见机器耗时 p50 严格更低、
混合案例总机器耗时不高于 main。
机器耗时包括规划、CLI、治理和生产核实；人工等待单独报告。
即时脚本确认 actor 的 human_wait=0 不是真人等待测量。

千问使用已授权免费额度；全局 Jev 账本跨探测/校准/验收保留历史。
pending/unknown 费用不当作零；预算停止保存部分报告。
当前实测收益没有通过结论；合成参考的结果也不能外推至真实企业业务。
完整命令与统计约束见[业务基准评测说明](0844_业务基准评测说明.md)。

## 当前部署拓扑

Windows API 和 business worker 共用 .decagent/local-control，
PostgreSQL 用既有卷经 55432 映射，Docker 修复 broker 经 8001 映射。
原 /data 复制与追加迁移应保留 owner、历史任务和证据，不重新初始化拥有者。
PostgreSQL 和 broker 额外接 windows-control 非 internal 网络以启用 loopback 端口；
修复 worker、出口 proxy 与执行容器未加入这个网络。
本机已完成 schema 6 迁移、服务健康、插件登记、目录初始化及一条无模型 rule 创建。
原 owner、数据库卷与原 /data named volume 保留；升级前历史任务计数为 0，
旧任务恢复由独立集成场景验证。
备份、旧镜像标签和具体回执见[本机验收记录](0900_本机业务验收记录.md)。
这些证据没有证明 Jev 提效或独立 Linux 业务可用。

历史修复架构仍可在[旧架构详解](../261011/0616_智能体架构详解.md)查阅。
它的修复质量/PR/介入结论不证明本次业务 Agent 质量。
源码全索引见[源码结构与脚本职责](../261011/0639_源码结构与脚本职责.md)。


真实服务的舍入兼容与无效响应处理见[兼容性和评测协议](1512_真实模型兼容性与评测协议.md)。开发收集完成与有效覆盖分别报告；无效响应不拟合，涉及的 family 禁用。
