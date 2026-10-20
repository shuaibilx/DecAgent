# EnterpriseOps 私有评分与真实结果回复实施

生成时间：2026-10-19 14:33:38（北京时间）。代码位于 DecAgent，当前未提交、未推送。

## 本轮结果与边界

| 项目 | 当前结果 | 不代表什么 |
| --- | --- | --- |
| EnterpriseOps 原 verifier 适配器 | 已实现、离线与容器合成检查通过 | 未运行正式官方业务任务 |
| 最终结果回复（任务 9.8） | 已实现、独立规格/代码审查通过 | 未验证真实 Qwen 总结质量 |
| 公开对照清单 | v2，286 全量案例 + 24 冒烟试次，全部 not_run | 没有筛掉 response_check 任务 |
| 主模型门禁 | 仍未就绪；旧 unknown 保留 | 没有新增或重放 Qwen 请求 |
| 正式成绩 | 0/286；无 Jev 优势结论 | 环境就绪和脚本回调不是任务成功率 |

新 Qwen/Jev 网络请求均为 0。脚本回调中的 token、费用和输出是测试夹具，用于验证计量传播，不能作为真实消费或质量证据。

## 原评分链路

`enterprise_scoring.py` 加载固定上游 VerifierEngine/VerifierConfig，先核对源码归档 SHA-256 和 benchmark 包的逐文件内容，再导入实际源代码。加载器只清理由自己加入的 sys.path，保留调用方原路径顺序。

同一次试验的私有 verifier 使用原始配置、真实 database_id 和 context；database_state、tool_execution、response_check 的比较语义仍由原 verifier 执行。所有有效 verifier 的 passed 合取决定官方任务成功。合法 passed=false 是任务失败；配置/SQL/judge 错误、异常或不合法返回是评分失败。环境边界不匹配则在模型、工具和 verifier 之前拒绝。

恢复后的初始指纹与试次预注册 digest 必须匹配；评分后再次记录有界状态指纹。journal 只保存 digest、表/行数量、检查数量、计分状态及 judge 用量等审计投影，不把 SQL、expected_value、原始 verifier 输出或 gold 放入 Agent 上下文/公开结果。

官方最终状态分数不能证明每个中间动作均安全。因此未独立测量的禁止副作用为 null，审计显示 not_measured，不能填 0。

## 最终回复与总结

默认回复把本试次 journal 中实际返回的工具观察确定性渲染为结构化结果，并带 source call ID。回复描述的是观察，不自动声称业务目标已核实。计划 goal.description 不能冒充已执行事实。

公开输出策略在运行前冻结并进入 ComparisonTrial 与 manifest 的摘要。默认 evidence_then_summary；只有预注册叙述要求，或完整确定性结果超过输出上限时，才使用一次显式注入的 Qwen 总结回调。输入只包含公开任务/政策和真实观察。完整证据超过总结输入预算时停止，不截断来制造成功。

~~~mermaid
flowchart TD
    A[计划到达完成节点] --> B{工具结果均已返回？}
    B -->|未知 / 在途| X[停止；不猜结果]
    B -->|是| C[从 journal 读取真实观察]
    C --> D{冻结输出策略可确定性表达？}
    D -->|是| E[零额外模型；结构化回复]
    D -->|否| F{完整证据在总结输入预算内？}
    F -->|否| X
    F -->|是| G[最多一次受控 Qwen 总结]
    G --> H{用量完整且来源 / 格式有效？}
    H -->|用量未知| Y[保留 unknown；跳过评分和后续裁判]
    H -->|格式 / 来源无效| X
    H -->|是| I[保存真实最终回复]
    E --> I
    I --> J[私有原 verifier]
    J --> K[官方任务分数；副作用仍可能未测]
~~~

总结必须返回非空有界文本和有效 source call ID；不允许引用未观察的来源。来源引用/格式检查不能证明每句话在语义上正确，仍需官方 verifier 和正式模型实验。

总结使用 ObservedUsage 的 primary 类别，计入主模型次数、token、费用及 Agent 执行时间，不藏在免费 harness 或 judge 中。未知用量后停止，不调用下一次总结或官方裁判；已知失败消费仍保留。命中复用流程却追加总结的试次如实显示非零 LLM 调用。

ReAct 的真实 finish message 与 Plan 的最终输出受同一冻结 UTF-8 字节上限。τ Plan 结束语也改为真实观察结果，不再使用目标描述。

~~~mermaid
sequenceDiagram
    participant R as 执行器
    participant J as 私有 Journal
    participant Q as 注入的 Qwen 回调
    participant V as 原始 Verifier
    R->>J: 读取本试次已返回观察
    alt 确定性输出足够
        J-->>R: 原始结果与来源
        R->>J: 保存确定性最终回复
    else 需要受控总结
        R->>Q: 公开任务/政策 + 真实观察
        Q-->>R: ModelReply + 实测用量
        R->>R: 计 primary、校验来源和输出上限
        R->>J: 保存实际最终回复
    end
    alt 用量完整
        R->>V: 实际回复/工具调用 + 私有原配置
        V-->>R: 合法任务 verdict 或评分故障
        R->>J: 有界私有评分证据
    else 用量未知
        R->>J: 保留未知消费；不进入评分
    end
~~~

## Judge 的计量与停止

response_check 默认没有 judge；必须显式注入 BudgetedEnterpriseJudge 和受控客户端。该适配器逐次核对请求计数、输入/输出 token、费用与时长。每次实际请求必须有完整的对应增量；未知或计数不匹配时立即持续阻断，复用同一对象也不能再次派发。

评分失败中的已知 judge 用量仍进入 ExecutionResult.usage；未知部分保留在私有审计和 ModelAttempt，complete=false，不生成可报告 RouteRun。调用方仍须提供真实模型的授权、预算预留、readiness 门禁和 provider 适配；本轮仅验证注入式接口。

## 验证

| 验证范围 | 结果 | 证据含义 |
| --- | --- | --- |
| 当前完整确定性套件 | 1667 passed，13 skipped，228.73s | 当前源码最终复跑；跳过不计通过 |
| 评分、状态边界、最终回复、路由、journal、配对与清单组合 | 139 passed | 离线契约与错误边界 |
| 固定原 VerifierEngine 合成 SQL/工具/响应检查 | 4 passed | MockTransport，无网络和正式任务 |
| CSM/ITSM Docker 恢复与合成评分适配器 | 2 passed，16.39s | 两次新鲜容器与实际库；非正式任务分数 |
| τ 子进程全套（显式启用独立运行时） | 24 passed，74.05s | 包含 9 项原运行时检查；ReAct/Plan 另单跑 2 passed/27.25s，均为协议证据 |
| PostgreSQL + 认证 loopback MCP 路线 | 2 passed，21.15s | 专用容器派生隔离库，脚本模型 |
| Ruff | decagent/evals 全部通过 | 静态检查 |
| 来源 / 本机固定镜像门禁 | 通过 | 固定输入可用，非模型质量通过 |
| wheel 与源码外安装 | 通过 | 新模块在发行包中、源码外导入与 CLI --help 通过；无依赖联网 |

全量运行的 13 个跳过：9 个 τ 子进程需要显式选择独立运行时，1 个旧应用内 τ 导入缺可选运行时，3 个 Windows 符号链接权限不足。随后显式启用独立运行时，τ 子进程全套 24 项全部通过，覆盖前述 9 项；应用内 τ 和 3 项符号链接检查仍跳过。没有把环境跳过记成通过。wheel 和安装目录保存在本机忽略的 .decagent/verification 下，原始基准缓存未包含在包内；实际 PostgreSQL 路线的派生 test/template 数据库已核对清理为 0。

独立审查先发现并复现导入路径误删、unknown 后继续评分、ReAct 输出额度不一致；均补失败测试后修复并复核通过。上游代码被修改、缺失或增加文件时，在 import 之前拒绝。

从 DecAgent 根执行：

~~~powershell
.\.venv\Scripts\python.exe -m pytest evals/deterministic/test_business_final_response.py evals/deterministic/test_business_enterprise_scoring.py -q
.\.venv\Scripts\python.exe -m pytest evals/integration/test_business_enterprise_scoring_offline.py -q
.\.venv\Scripts\python.exe -m ruff check decagent evals
openspec validate --changes --strict
~~~

可选真实容器检查需要本机已有固定镜像：

~~~powershell
$env:DECAGENT_ENTERPRISEOPS_DOCKER_SMOKE = "1"
.\.venv\Scripts\python.exe -m pytest evals/integration/test_business_enterprise_environment.py -q
~~~

## 后续计划

1. 9.6B 保留部分完成：必须在放行后的正式试次贯通原官方任务、fresh database、全部原 verifier；本轮没有这个实测证据。
2. 9.6C 的协议链路已有脚本证据，后续补跨域正式对话/评分及故障验收。
3. 9.6D–F 继续可信模板/同状态语义配对、暴露分组和资源/冷热/记忆消融、正式冒烟门槛；无模板保持 N/A。
4. 10.3–10.6 的真实模型评测继续受 readiness/unknown 门禁限制，不自行增加授权。
5. 游客空间、预算、本机 SOP Compose 与私有 CD 按 guest-sop-local-trial 另行实施；不以当前评测模块说明替代这些未完成任务。

当前源文件：decagent/evaluation/business/enterprise_scoring.py、final_response.py、public_session.py、routes.py、journal.py、comparison.py。当前清单：evals/benchmarks/manifests/comparison_trial_manifest.json；旧 v1 保存在 evals/benchmarks/evidence/comparison-trial-manifest-v1-before-final-response.json。
