# τ-Bench 独立运行时桥与验证

记录时间：2026-10-18 19:28:43（北京时间）。Change：`jev-conditional-workflows`。本轮为本地未提交实现。

你已选择保留 τ2 与 MCP 各自依赖锁，通过专用子进程桥连接。本轮已落实并通过定向验证；OpenSpec 仍为 **52/60**，9.6 和 10.1–10.6、11.3 未全部完成。主要/补充公开任务覆盖仍为 **0/206 + 0/80**，没有新的模型调用或任务得分。

## 两套环境与调用边界

| 部分 | 位置与职责 |
| --- | --- |
| DecAgent 应用 | 项目 `.venv`，保留 MCP 2.3.0 及应用锁；承载 MCP 客户端/服务端、生产计划编译器、绑定器、PlanRuntime 和试验日志 |
| 原始 τ2 | 忽略目录 `.decagent/evaluation-runtimes/tau2-96a3ef9-py312`，Python 3.12.7、τ2 1.0.1；保留固定提交的 core 依赖 |
| 固定源码与数据 | `evals/benchmarks/vendor_cache/tau2-bench-source`，由 source manifest 固定到 `96a3ef92329baf908ffe9dfe188cea34e8701d2a` |
| 桥接客户端 | `decagent/evaluation/business/tau_process.py`；每试验创建一个专用进程，不能由模型提供执行命令 |
| 子进程入口 | `decagent/evaluation/business/tau_worker.py`；只导入标准库和原始 τ2，不导入 MCP/DecAgent 依赖 |
| MCP 适配 | `tau_tools.py`；原工具跨真实 MCP list/call 协议，原始可变状态元数据映射为 readOnlyHint |
| 业务评分 | 后续独立官方评分器；桥接没有模型、用户模拟器或评分器接口，不能把执行结束改成官方任务成功 |

~~~mermaid
flowchart LR
  subgraph A["应用环境：应用依赖锁"]
    H[可信评测器] --> P[生产 Compiler / Binding / PlanRuntime]
    P --> J[独占试验日志]
    P --> C[MCP Client]
    C --> S[MCP Server / TauToolSession]
    S --> B[TauProcessEnvironment]
  end
  subgraph T["独立 Python 3.12：原始 τ2 依赖锁"]
    W[专用 stdio worker] --> E[原 Environment.get_response]
    E --> K[原 Tool / Toolkit]
    K --> D[当前试验的原始数据库]
  end
  B -->|带请求 ID 的有界 JSON| W
  W -->|原文本结果 / 原错误标志| B
~~~

依赖冲突来自实际解析结果：MCP 2.3.0 的 httpx2 依赖链要求更高的 idna，而上游锁固定 idna 3.11。因此没有把 MCP 安装进 τ2 环境，也没有升级上游依赖。

上游声明允许 Python 3.13，但该固定版本会经 eager voice import 引用已被 Python 3.13 移除的 audioop。本机改用独立 Python 3.12，应用 Python 不受影响。core 安装还需补上原 uv.lock 已列出的可选依赖 `websockets==15.0.1`；本机共 78 个包通过 `uv pip check`。这项补充显式记录，不把它描述为未加任何包的 core 环境。

## 协议、错误与回收

启动参数显式使用 `-I -X utf8`。Windows 下仅设置 PYTHONUTF8 无法覆盖 -I；缺少 -X utf8 会使电信政策文件按 GBK 读取并失败。

- 工具名称、参数 schema、参数 JSON 类型、原结果文本与 error 标志保留。字符串数字不会被适配器自动强转。
- 每帧最多 4 MiB；初始化上限 60 秒，工具请求 15 秒；写入管道阻塞也计入请求超时。
- 请求 ID 不匹配、损坏/超大响应、EOF 或超时会关闭该试验，不重试工具。普通上游工具错误仍是原观察，可按原恢复规则处理。
- worker 不继承模型密钥、代理凭据、PYTHONPATH 或应用配置；禁用 dotenv，并通过 Python 网络审计钩子阻止连接和 DNS 请求。
- Windows 回收当前拥有的进程树，覆盖虚拟环境 redirector 启动的实际解释器；POSIX 使用独立进程组。POSIX 分支尚未在独立 Linux 验收。
- MCP 退出和启动失败会回收已创建的环境。写入若失去结果，既有日志保留 unknown 并阻止自动重放。
- 私有状态摘要只供评测器核对；原任务参考答案、私有用户情景和目标状态不进入 MCP 工具目录。

~~~mermaid
sequenceDiagram
  participant H as 可信评测器
  participant R as PlanRuntime
  participant M as MCP 适配
  participant W as τ2 子进程
  H->>W: 固定来源、域、test task ID
  W->>W: 核对归档/源码并应用原始 initial_state
  W-->>H: 公共政策、工具及原始读写元数据
  H->>R: 校验后的条件计划
  R->>M: 解析前序输出，构造实际工具参数
  M->>W: 原参数 JSON
  W->>W: 原 Environment.get_response
  alt 收到原工具错误
    W-->>M: 原文本 + error=true
    M-->>R: 原错误观察，走显式错误边
  else 进程退出或超时
    M-->>R: 传输异常
    R->>R: 写入结果 unknown，停止自动重放
  end
~~~

## 实际验证结果

| 检查 | 结果与范围 |
| --- | --- |
| 官方任务离线初始化 | retail test 40、telecom test 40，**80/80** 双重全新初始化状态一致；0 模型请求 |
| 固定来源 | 源归档 SHA256、uv.lock、源码和两个域数据共 **279 个文件**比对通过 |
| 本轮定向回归 | **142 passed，1 skipped，77.65 秒**；套件之间有重叠，不与历史测试数量相加 |
| 原始环境经 MCP | 零售有效计算、原始错误、无参数强转；电信原始错误与调用前后状态一致 |
| 生产计划参数绑定 | 两步只读协议回归：查用户 ID → 从前序原响应绑定 user_id → 查详情；不调用官方评分器，不代表完成所选官方任务 |
| 进程故障 | 超时、进程退出、超大帧、损坏 JSON、ID 失配、阻塞 stdin、请求过大及 Windows 后代进程回收 |
| 网络与凭据 | 模型密钥不继承；网络探针被审计钩子阻断 |
| 静态/规格 | Ruff、OpenSpec strict、应用 uv.lock、正式记分来源门禁通过 |
| 构建 | wheel 包含 tau_process.py、tau_worker.py、tau_tools.py；SHA256：`4784377911fbc5ef9b062271ee36a86a1151893eec10f4b54d2dc78796c6dd4a` |

唯一 skip 是旧的“在应用环境直接 import tau2”可选测试；应用环境有意不装 τ2。本轮通过独立进程实际执行上游工具的测试均已启用并通过。

先前完整 PostgreSQL / 全量确定性结果见 [1858 实施记录](1858_隔离评测工具注册与验证.md)。本轮未重跑整套 PostgreSQL，也未触发远程 GitHub CI 或重新部署；不把先前测试当成本轮全部改动后的全量结果。

80 条初始化证据见 [运行时就绪记录](../../../evals/benchmarks/evidence/tau2-runtime-readiness-20261018T110840Z.json)。它只包含任务 ID、摘要与就绪状态，不包含标准答案。正式尝试数为 0，成功率为 null。

本轮源码摘要、测试分母、构建校验与未完成边界见 [机器可读验证记录](../../../evals/benchmarks/evidence/tau2-process-bridge-verification-20261018T113100Z.json).

## 本机复验

以下命令从 **DecAgent 项目根目录**执行。依赖环境已准备好，无需你再下载数据或安装 MCP 到 τ2 环境。

~~~powershell
$env:PYTHONUTF8 = '1'
$env:DECAGENT_TEST_TAU_PYTHON = (Resolve-Path ".decagent/evaluation-runtimes/tau2-96a3ef9-py312/Scripts/python.exe").Path
.\.venv\Scripts\python.exe -m pytest evals/deterministic/test_business_tau_process.py evals/deterministic/test_business_tau_tools.py evals/deterministic/test_business_public_evaluation_session.py -q -rs
uv pip check --python $env:DECAGENT_TEST_TAU_PYTHON
~~~

重跑全部 80 条离线初始化可使用以下命令；输出文件必须不存在：

~~~powershell
& $env:DECAGENT_TEST_TAU_PYTHON -X utf8 evals/benchmarks/verify_tau_runtime.py --output .decagent/evaluation-runtimes/tau2-readiness-new.json
~~~

Windows also needs UTF-8 mode for this data. On 2026-10-20, running the task
initializer without it yielded `UnicodeDecodeError` for all 40 telecom cases
(40/80 ready). Re-running with `$env:PYTHONUTF8 = '1'` yielded 80/80 ready; the
separate-process τ bridge regression passed 24 tests. The failed and successful
readiness records are preserved at `evals/benchmarks/evidence/tau2-runtime-readiness-20261020.json`
and `tau2-runtime-readiness-20261020-pythonutf8.json`. Both are offline
initialization evidence only: 0 model requests, 0 official task attempts, and no
official task score. Setting `$env:PYTHONUTF8` in PowerShell changes the
environment for child processes launched from that terminal; it is not a
permanent machine-wide setting.

在新机器重建时，先按 source manifest 下载和校验固定源，再将 UV_PROJECT_ENVIRONMENT 指向独立忽略目录，使用 `uv sync --project <固定源目录> --python 3.12 --frozen --no-dev` 安装原锁。随后只添加 `websockets==15.0.1`（`uv pip install --python <独立python> --no-deps websockets==15.0.1`），再运行 pip check 与上述离线验证。保留应用的 requirements/uv.lock，不能把两套环境混装。

## 尚未通过的验收

- **9.6**：隔离目录、执行器复用与原始 τ 工具桥已落地；business-2 历史服务适配、Enterprise 独立恢复状态验证、可信语义模板、完整用户模拟器与正式配对/消融仍待补齐。
- **10.1**：qwen3.8-flash 获准的单次修正重测超时、用量未知，未生成有效计划；按既定约束停止追加请求，不更换模型。
- **10.2**：独立验证样本不足，不发布合格生产阈值；自动正确率至少 95% 和每族至少 100 个自动样本的要求保持不变。
- **10.3–10.4、10.6**：未完成官方全流程运行、官方评分和成本/延迟对照，不能声明 Jev 已证明提速或降本。
- **10.5**：单人六个工作台探索场景、真实员工研究和 τ 用户模拟器证据仍分开待验收。
- **11.3**：最终审计保持待办；本机工程验证不替代正式性能结论或独立 Linux 生产验收。

后续先补齐可离线验证的适配、评分与状态隔离证据；依赖主模型的真实比较仍受失败停止规则约束。这里没有把未通过项勾选为完成。
