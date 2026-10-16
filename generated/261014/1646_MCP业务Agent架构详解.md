---
title: MCP 业务 Agent 架构详解
type: explanation
audience: [A2, A3, A4, A5]
runs: no
verified_on: 2026-10-14
sources:
  - decagent/business
  - decagent/gateway/business.py
  - decagent/runtime/application.py
  - decagent/storage
  - decagent/evaluation/business/mcp_acceptance.py
---
# MCP 业务 Agent 架构详解

DecAgent 用一个可恢复的执行内核，把自然语言目标转换为受员工权限约束的业务操作。
项目的差异化在于“如何决定继续、澄清、确认与恢复”，并为每次写入提供可核实证据。
当前服务工单是模拟业务载体；业务能力以 MCP + Skill 接入。
仓库修复保留为另一类任务，并不限定这个 Agent 的用途。

## 分工与组件

LLM 负责复杂规划与文本参数提议，Jev 负责有限选项的类型化判断，
程序负责候选边界、身份、权限、预算、批准和执行一致性。
Skill 描述业务做法，manifest 固定工具与参数 Schema；二者均不能赋予额外权限。
具体实现见 [规划器](../../decagent/business/planner.py)、
[题目构造](../../decagent/business/questions.py)和[操作核心](../../decagent/business/operations.py)。

```mermaid
flowchart LR
  U["员工浏览器"] --> API["员工 API / 工作台"]
  X["独立 MCP 客户端"] --> MCP["官方 SDK MCP Server"]
  API --> DB[("PostgreSQL")]
  API --> CM["按员工隔离的连接管理器"]
  W["BusinessWorker / LangGraph"] --> CM
  W --> LLM["主模型"]
  W --> JEV["Jev 类型化判断"]
  W --> C["manifest + Skill"]
  CM --> MCP
  MCP --> AUTH["身份 / 角色 / 范围"]
  AUTH --> OP["统一操作与批准核心"]
  OP --> DB
  W --> DB
```

Web API 的工单读取也经过 MCP。批准 API 使用同一 OperationService 的事务接口，
不另外实现一条业务写入路径。数据库访问用于身份、任务、批准和审计；
工单状态变更统一在授权的 commit 事务中执行。

## 员工登录与凭据

```mermaid
sequenceDiagram
  actor U as 员工浏览器
  participant A as 员工 API
  participant DB as PostgreSQL
  participant F as 受保护凭据目录
  participant M as MCP 服务
  U->>A: 登录名、密码、Origin
  A->>DB: 验证账号和密码哈希
  A-->>U: 12小时 cookie 与 CSRF
  U->>A: 申请独立 MCP 凭据
  A->>F: 写入随机 token
  A->>DB: 保存哈希、员工及到期时间
  A-->>U: credential_ref 和文件路径
  U->>M: MCP Bearer 调用
  M->>DB: 重查账号、凭据与角色
  M-->>U: 当前员工范围内的结果
```

网页显示凭据文件路径，明文保存在受保护文件中。独立凭据持续一小时；
退出浏览器不会自动撤销它，需要显式撤销。

## 从用户目标到核实结果

```mermaid
flowchart TD
  A["原员工提交目标"] --> B["prepare：查询实际目录与工单"]
  B --> C{"decide"}
  C -->|明确规则目标| V["validate：参数与政策"]
  C -->|兼容校准可用| J["Jev：有限候选判断"]
  C -->|复杂或缺少校准| P["plan：主模型提案"]
  J -->|足够确定| V
  J -->|不确定或不兼容| P
  P --> V
  V -->|事实不足| H["human：澄清"]
  H --> B
  V --> E["execute：MCP prepare"]
  E -->|待具体批准| HA["原员工网页确认"]
  HA --> E2["MCP commit 原操作"]
  E -->|自动动作且授权有效| E2
  E2 --> Q["verify：原回执和资源"]
  Q -->|目标有证据支持| F["finalize"]
  Q -->|未知| R["reconciliation_wait"]
  R --> Q
```

LangGraph 的 task_id 同时绑定 checkpoint thread_id。
图版本为 business-2，最多 128 个阶段；人工等待、预算等待和核实等待独立 checkpoint。
Worker 认领时获得新的 lease generation，旧 Worker 的写入被拒绝。
预算耗尽可以查询已提交回执，但不能签发新的执行权限。
[执行器](../../decagent/business/execution.py)将 app.actions 关联到服务操作；
prepare、批准和 LLM 的成功文本均不算完成。

## 泳道：谁有权做什么

```mermaid
flowchart LR
  subgraph Employee["员工"]
    U["提出目标"] --> A["核对具体操作"]
  end
  subgraph Agent["Agent"]
    T["固定员工任务"] --> P["生成合法提案"]
    P --> G["签发受预算和租约约束的 grant"]
    V["核实原回执"]
  end
  subgraph Service["MCP 服务"]
    R["prepare 保存不可变输入"]
    C["commit 重查授权"]
    D["工单、历史、回执同事务"]
  end
  U --> T
  G --> R
  R --> A
  A --> C
  R -->|允许自动动作| C
  C --> D
  D --> V
```

员工 cookie 与 MCP token 分离；owner 运维登录也独立。
浏览器批准只允许原操作员工，主管的全局审计权限不能代签。
外部 MCP 调用不带 Agent grant；Agent 路线必须携带可信程序签发的隐藏元数据。
LLM 无法通过工具参数提供 employee_id、role、approved、任意 endpoint 或 grant。

## 时序：准备、批准、提交

```mermaid
sequenceDiagram
  actor U as 原员工
  participant W as Agent 或独立客户端
  participant M as MCP 服务
  participant A as Web API
  participant DB as PostgreSQL
  W->>M: prepare(request_id, command, params)
  M->>DB: 验证身份并保存不可变操作
  M-->>W: operation_id 和 ready/awaiting_approval
  opt 需要具体确认
    U->>A: cookie + CSRF + input_hash
    A->>DB: 原员工批准，15分钟到期
  end
  W->>M: commit(operation_id)
  M->>DB: 重查角色、revision、批准、grant
  M->>DB: 单事务写工单、历史、回执并消费批准
  M-->>W: 原操作结果
  W->>M: get_operation(operation_id)
  M->>DB: 核对回执、历史与资源
  M-->>W: succeeded 和已绑定回执
```

对同一 request_id 重复准备，相同内容返回原操作，不同内容报冲突。
操作输入不能原地编辑。外部编辑建立 supersedes 链；Agent 编辑使旧动作被拒绝，
随后重新规划新动作、新操作并再次确认。

## 未知结果的恢复

```mermaid
flowchart TD
  U["超时、断连或进程退出"] --> Q["查询原 operation_id"]
  Q --> I{"操作锁是否仍在途"}
  I -->|是| W["unknown：等待后重查"]
  W --> Q
  I -->|否| E{"回执、唯一历史和资源是否一致"}
  E -->|一致| S["succeeded：返回原回执"]
  E -->|证据矛盾| H["unknown：人工核实"]
  E -->|均无提交证据| A{"当前授权和批准有效"}
  A -->|否| F["expired / failed"]
  A -->|是| R["沿原 ID ready"]
  R --> G["Agent 需当前租约 grant"]
  G --> C["沿原 ID commit"]
```

这是针对本地业务事务的幂等和恢复设计。它不宣称任意远程第三方 API 都能实现相同保证。
以后接入真实公司 API，需要该 API 提供幂等键、查询回执或补偿机制，并重新验收。

## Jev 的三类判断如何使用

| 类型 | 当前问题 | 候选或尺子由谁提供 | 如何使用 |
| --- | --- | --- | --- |
| Choice | operation、person、category | manifest 的合法命令；MCP 实际查询返回的实体 ID；附 no_match | 选有限分支，重名或不明确时不猜 |
| Noul | explicit_assignee、multi_person | manifest 固定的自然语言命题 | 返回 0～1 的命题支持概率，经阈值与政策处理 |
| Score | work_blockage | 0=工作不受影响；1=有替代办法；2=工作被阻断 | 新工单建议 normal/high/urgent；现有优先级修改仍需具体确认 |

这些题目和评分尺由程序与版本化契约构造，LLM 不动态改写权限题或评分尺度。
动态的是实际业务上下文和查询候选。LLM 可以提议 title、description 等文本，
也可以提出已注册命令；程序继续检查 ID 来源、事实依据和 Schema。

Score 允许浮点期望值；小于 0.5 为 normal，0.5～小于 1.5 为 high，其余为 urgent。
Jev 返回的概率、分数与分布须满足契约一致性检查。
问题版本、工作流版本、插件摘要、数据集版本、实际模型名共同绑定校准配置。

当前迁移保留三种判断和用量规则。旧校准绑定 CLI 契约，不能直接授权新的 MCP 快路线；
没有兼容校准时 hybrid 记录原因并转交主模型。
本次 MCP 验收使用脚本与规则，不产生 Jev 调用，因此没有新的加速或真实业务提效结论。
旧 720 次对照与历史价格账本仍保留其原始范围。

## 身份、数据和授权关系

```mermaid
erDiagram
  EMPLOYEE_ACCOUNT ||--o{ EMPLOYEE_SESSION : owns
  EMPLOYEE_ACCOUNT ||--o{ MCP_CREDENTIAL : owns
  EMPLOYEE_ACCOUNT ||--o{ TASK : binds
  TASK ||--o{ ACTION : plans
  ACTION ||--o{ AGENT_GRANT : authorizes
  ACTION }o--o| OPERATION : binds
  OPERATION ||--o{ OPERATION_APPROVAL : awaits
  OPERATION ||--o| RECEIPT : commits
  TICKET ||--o{ TICKET_HISTORY : records
  RECEIPT }o--|| TICKET : verifies
  TASK ||--o{ CHECKPOINT : persists
```

此图表示逻辑关系；物理外键、唯一约束与触发器以 storage 的迁移文件为准。
历史匿名行保持 employee_id=NULL，主管可审计但新操作不能认领或修改。
新任务的 employee_id 不可变，原员工再认证只更新 credential_ref。

## 工具与本机部署

十二个领域命令是 directory、get、list、receipt、create、assign、start、
comment、resolve、close、reopen、set_priority。
网络目录有十四项：四个 tickets_ 读取工具、八个 tickets_prepare_ 写准备工具，
以及 business_commit_operation、business_get_operation。
账号不同，可发现工具会按角色过滤。

服务输入 64 KiB、输出 256 KiB、单请求 15 秒。
服务绑定 loopback，检查 Origin 与 Bearer；现代连接采用 discover，
兼容客户端使用旧协议初始化。连接按员工凭据隔离，进程生命周期管理，
没有逐调用新建事件循环或业务子进程。

```mermaid
flowchart LR
  subgraph Windows["Windows 本机"]
    B["浏览器 :8000"] --> A["API :8000"]
    W["业务 Worker"] --> M["MCP :8001"]
    A --> M
  end
  subgraph Docker["Docker Desktop"]
    P[("PostgreSQL :55432")]
    R["修复 broker :8002"]
    X["隔离 runner / egress proxy"]
    R --> X
  end
  M --> P
  W --> P
  A --> P
  A -->|修复入口| R
```

独立 Linux 主机部署仍是后续验收项。
本机 Docker/WSL 不能替代该结论。公司业务 API 未接入。

## 阅读入口

- [使用指南](1646_MCP业务Agent使用指南.md)
- [交互架构图册](1646_MCP业务Agent架构图册.html)
- [源码与逐脚本职责](1646_MCP源码结构与脚本职责.md)
- [权限指南](../261013/1623_模拟员工账号与权限指南.md)
- [操作恢复细则](../261013/2039_业务操作恢复与核实指南.md)
