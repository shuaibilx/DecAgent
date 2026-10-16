---
title: 业务 Agent 使用指南
type: how-to
audience: [A1, A4]
runs: yes
verified_on: 2026-10-13
applicability: Windows 原生业务控制服务与受信任工单插件；保留 Docker 仓库修复
verification_scope: 当前源码、CLI help、本机服务更新、独立测试库及9次真实功能探测；生命周期示例未逐条现场复做，Jev提效与独立Linux待办
sources:
  - decagent/cli.py
  - ops/local/start.py
  - compose.windows.yaml
  - decagent/business/administration.py
  - decagent/business/tickets/manifest.json
  - decagent/gateway/business.py
  - decagent/gateway/static/business.js
  - decagent/runtime/worker.py
  - decagent/evaluation/business/__main__.py
---

> 历史版本说明：本页保留 CLI / owner 业务路线的当时内容。当前业务已迁移为员工身份下的 MCP + Skill；请使用[当前指南](../261014/1646_MCP业务Agent使用指南.md)和[当前架构](../261014/1646_MCP业务Agent架构详解.md)。

# 业务 Agent 使用指南

用这份指南在 Windows 上运行本地工单业务，提交自然语言任务，并处理确认、澄清和恢复。
默认工作台是[业务页](http://127.0.0.1:8000/)，已有仓库修复在[修复页](http://127.0.0.1:8000/repair)。
业务目录和工单是本地合成服务数据，完成工单操作不表示真的修好了企业设备。

本文以当前源码为准。文档核对没有调用模型。
本机升级、服务健康及单条 rule 创建已经[验收](0900_本机业务验收记录.md)。
标为“本次未执行”的代码块表示文档核对没有逐条重做该示例，不能把它们当作完整生命周期现场验收。

## 准备环境

你需要已安装本项目依赖的 Python 3.11+ 环境、Docker Desktop 和既有 PostgreSQL 数据。
从 DecAgent 根目录运行命令。保持原实例名称、数据库卷、私有密钥和拥有者身份。
原 Waku 的 `.waku/` 与 DecAgent 的 `.decagent/` 各自独立。

本机环境使用 Conda/pip。以下是安装示例，本次只核对了命令入口；安装会修改 Python 环境。
要撤回安装，切回原环境；不要删除运行数据和密钥目录。

```powershell title="not executed in this run"
conda activate Agent311
$env:PIP_CONFIG_FILE = "nul"
python -m pip install --index-url https://pypi.org/simple -r requirements.txt
```

PowerShell 的中文输入和 Python 输出都使用 UTF-8。以下设置只影响当前终端会话：

```powershell title="not executed in this run"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
```

遇到 GBK 错误时先核对终端编码和文件真实编码，不自动转码、替换字符或修补源码。
业务 JSON CLI 接受 UTF-8 stdin；直接管道输入中文尤其需要上述设置。

## 配置 Windows 控制服务

`ops.local.start` 先读取根目录 `.env`，再读取 `DECAGENT_SECRETS_DIR`
（默认 `.decagent/secrets`）中的现有密钥文件。已有环境密钥优先。
密钥不要放在命令行或示例文档里。

| 配置 | 默认或作用 |
| --- | --- |
| `DECAGENT_LOCAL_DATABASE_URL` | 显式主机 PostgreSQL DSN；未设置时用私有 postgres_password 构造 |
| `DECAGENT_LOCAL_POSTGRES_PORT` | `55432`，Compose 将 PostgreSQL 绑定到 `127.0.0.1` |
| `DECAGENT_LOCAL_DATA_DIR` | `.decagent/local-control`，Windows API 与业务 worker 共用 |
| `DECAGENT_LOCAL_BROKER_URL` | 显式主机 broker 地址 |
| `DECAGENT_LOCAL_BROKER_PORT` | `8001`，默认 broker URL 为 `http://127.0.0.1:8001` |
| `DECAGENT_PUBLIC_URL` | 默认 `http://127.0.0.1:8000`；CLI 地址与浏览器 Origin 应一致 |
| `DECAGENT_BUSINESS_CALIBRATION_FILE` | 已验证的业务开发集 profile；缺失时 hybrid 回到主模型 |
| `DECAGENT_MAIN_*` / `DECAGENT_JEV_*` | 原实例模型、供应商、密钥与版本化价格设置 |

模型名称以实例配置和返回的实际模型为准。`~typesafe/jev-latest` 是请求别名，
不能据此沿用另一个实际模型版本的校准。

## 升级并启动既有实例

1. 停止新增任务和旧控制服务写入，保存一致数据库、证据、凭据备份及旧镜像标签。
2. 把原 Docker `/data` 中的运行资料复制到 `.decagent/local-control`，保留原 owner.token、证据和任务关联。
3. 使用 Compose 原数据库卷运行追加迁移。不要运行 owner-init 来覆盖既有拥有者。
4. 启动 Docker 修复 worker/broker 和 Windows 业务 API/worker。

本机已按这个拓扑完成升级：保留旧 control/executor 的 before-business-261012 镜像标签，
停止旧写入角色后保存 `.decagent/backups/261012-0858-before-business/261012-0858-before-business.dump`。
SHA-256 为 `D769B848B774D4810952F85C35FA803668D4556459E579858CC9A582E7362B68`。
原 /data 复制到 local-control，原 named volume 保留；旧 owner 保留。
升级前 tasks/projects/events 均为 0，不能据此声称已经用真实历史任务测试跨版恢复。
schema 6、migrate、broker、Docker 修复 worker、Windows API/业务 worker 已健康启动；
插件登记、目录初始化和 inspect 已验证。证据见[本机验收](0900_本机业务验收记录.md)。
迁移规则与回滚见[业务迁移与恢复](../261011/1840_业务迁移与恢复说明.md)。

```powershell title="not executed in this run"
docker compose -f compose.yaml -f compose.windows.yaml up -d --wait postgres migrate egress-proxy executor-broker worker
```

`compose.windows.yaml` 把 Docker worker 限定为 `repository_repair`。
Docker API 位于显式 `container-control` profile，默认业务 API 在 Windows。
PostgreSQL 与 broker 额外挂独立的非 internal windows-control 网络，以启用主机 loopback 端口；
修复 worker、出口 proxy 和执行容器没有加入这个网络，原修复边界保留。
不要同时启用占用 8000 端口的旧 Docker API。

在两个已激活同一 Python 环境的终端分别运行：

```powershell title="not executed in this run"
python -m ops.local.start serve --host 127.0.0.1 --port 8000
```

```powershell title="not executed in this run"
python -m ops.local.start worker --kind business
```

停止本机角色时在各终端按 Ctrl+C；Docker 服务可用相同两个 compose 文件执行
`stop worker executor-broker egress-proxy`。保留 PostgreSQL 卷和共享运行目录。
回滚应将备份恢复到新的空数据库和证据目录，再启动对应旧版本；
不要把旧应用直接连接到升级后的数据。取消或恢复 Agent 备份不自动撤销已提交业务写入。

只有未初始化的新实例才运行 `python -m ops.local.start init`。
既有实例使用保留下来的 `.decagent/local-control/owner.token` 登录。
令牌不要输出到聊天或日志。

## 登记工单插件

管理员在本机登记已安装的可信插件和合成人员目录。已有登记先 list/inspect。
register 是可信代码接入操作；Skill 是任务资料，不扩展权限。

```powershell title="not executed in this run"
python -m ops.local.start plugins install-tickets
python -m ops.local.start seed-tickets
python -m ops.local.start plugins list
```

从列表取启用的 `service-tickets` 的真实作用域 ID，查看完整契约：

```powershell title="not executed in this run"
$scopeId = (python -m ops.local.start plugins list | ConvertFrom-Json | Where-Object { $_.plugin_id -eq "service-tickets" -and $_.enabled }).id
python -m ops.local.start plugins inspect $scopeId
```

停用使用 `plugins disable $scopeId`；停用或修改摘要阻止冻结旧契约的任务继续执行。
不要为恢复旧任务随意换清单。
登记与独立 JSON CLI 见[插件接入指南](../261011/1851_业务插件与工单CLI接入指南.md)。

## 通过工作台完成任务

1. 使用既有 owner.token 登录业务页。
2. 选择启用插件、策略与明确预算，输入目标。
3. 查看工单结果、动作、判断记录、最终路线和持久事件。
4. 出现具体问题时回复；完成以后核对回执和对象 ID。

可从“请查询人员目录”开始，再尝试“请创建 Network 工单，VPN 无法连接，工作被中断”。
复合目标使用明确操作，例如“请创建 Network 工单，VPN 无法连接；请分配工单给 IT Support 的张伟”。
重名需要部门或准确人员 ID；不要根据同名猜测。

| 策略 | 使用方式 |
| --- | --- |
| `rule` | 确定性解析明确命令；无法保留完整条件时澄清，不调用模型规划 |
| `main` | 主模型基于同一契约规划；逐步经过程序校验 |
| `hybrid` | 简单查询用规则；兼容校准下采用有把握的类型化判断；复杂或不确定转主模型 |

没有兼容校准时 hybrid 仍可使用主模型，不表示已启用 Jev 快路线。
Choice、Noul、Score 都是判断结果，不能作为写入授权。

### CLI 提交和回复

CLI 经 owner API 提交，使用本机数据目录中的令牌。
以下会创建并执行真实任务，本次未执行；先取真实 scope ID：

```powershell title="not executed in this run"
python -m ops.local.start business submit --scope-id $scopeId --instruction "请查询人员目录" --strategy rule --max-calls 10 --max-tokens 20000 --max-active-seconds 300 --max-cost 0.10
```

任务、审批 ID 和 digest 从实际返回资料取值。`tasks`、
`task show <task_id>`、`task cancel <task_id>`、`task resume <task_id>` 适用于新旧任务。
角括号表示要替换的值，不是可直接执行的 PowerShell 文本。
精确回复使用已有变量，不能把其他动作的摘要换进来：

```powershell title="not executed in this run"
python -m ops.local.start business reply --approval-id $approvalId --digest $digest --decision approve
```

编辑使用 UTF-8 JSON 文件中的完整参数，随后再次确认：

```powershell title="not executed in this run"
python -m ops.local.start business reply --approval-id $approvalId --digest $digest --decision edit --params-file .decagent/edited-params.json
```

澄清使用 `--decision confirm --answer "IT Support 的张伟"`；拒绝使用 `--decision reject`。
预算暂停在网页填完整新预算再恢复；未知写入使用“核实并恢复”。
详情见[人工介入与故障恢复](../261011/2349_人工介入与故障恢复.md)。

## 检查确认与业务状态

新建、首次分派、开始处理、评论，只有明确请求且契约与当前资源校验通过才可自动执行。
改派、解决、关闭、重开、已有工单优先级变更始终要求具体确认。
主模型提出的未明确请求写入也须确认。

解决需要用户提供解决记录；关闭只允许已解决工单。
编辑产生新提案，编辑本身不执行。revision、目录或插件变化触发重评，
旧批准不能授权变化后的操作。

成功取决于已核实回执。创建并分派成功时工单可以仍是 assigned；
模型不能为宣布成功擅自解决或关闭。
超时和响应丢失先按原 operation_id 查 receipt，再查资源历史。
缺少可信回执时保持等待，不能换新键盲目重发。

## 查看校准和公平评测

题目、候选和评分尺来自冻结契约与实际业务查询。
业务校准使用 40 条开发案例的 synthetic-reference 标签；80 条固定验收不参与拟合。
见[判断与校准](../261011/2326_业务判断与校准策略.md)和[业务基准评测](0844_业务基准评测说明.md)。

文档核对已执行这个只读入口，输出确认 40 开发、80 验收及 live_calls=0：

```powershell title="executed in this documentation run"
.venv/Scripts/python.exe -m decagent.evaluation.business inspect
```

真实 Jev 开发采集完成 40 条，26 有效、14 无效；六类判断全部禁用，当前没有可启用的业务快路线 profile。固定对照完成 720/720，严格验收 not_passed：hybrid 的 7 个 trial 用量不完整。该实测使用完成判定和拒绝响应记账修复前版本，历史失败及未知用量保持原样；本次速度差异不能归因于 Jev。见[完整实测](2341_业务固定集完整实测.md)与[兼容性记录](1512_真实模型兼容性与评测协议.md)。
千问沿用已授权免费额度；Jev 累计上限 $1，累计已知费用 $0.007450422，其中历史消费 $0.000253176。
Jev 探测、校准、验收共用持久账本；其 unknown/pending usage 停止新的 Jev 调用。
千问零费率来自用户确认的免费额度，缺失用量仍保留为 usage_complete=false，并使完整验收不通过；不能由免费额度推算缺失 token。
脚本模型或数据库成功不能当作真实模型质量、延迟或费用收益。

## 定位问题

| 现象 | 处理 |
| --- | --- |
| 找不到 owner.token | 核对 local-control 与原 /data 复制范围；既有实例不重新 init |
| 连接数据库失败 | 核对 DECAGENT_LOCAL_DATABASE_URL、55432 映射、密码与原实例名 |
| 8000 被占用 | 停止旧 Docker 或本机 API，保留单一业务服务 |
| unsupported_platform | 当前 CliRunner 需要 Windows；独立 Linux 适配和部署待办 |
| 无校准/模型变化 | 查看降级原因，以开发集重新拟合 |
| 同名/缺少解决记录 | 提供用户事实，不让模型编造 |
| goal_unproven | 查看已保存动作；将目标写成明确注册操作或精确 JSON，先核实已发生写入，避免重复创建 |
| planner_response_invalid | 查看固定 rejection_reason、usage 和 resolved_model；完整响应的合法用量仍计入预算，不用重复请求掩盖失败 |
| stale_approval/revision 变化 | 刷新资源，检查并确认新提案 |
| 预算暂停 | 核对 usage 与预留，填写完整新预算 |
| 未知写入 | 核实原回执并保留 operation_id，不能盲目重试 |
| 图版本不兼容 | 保留 recovering 与 checkpoint，按迁移说明处理 |

本机升级实例中的 rule 任务 `78503e89-7929-413e-9ce9-54f2511be841` 已 succeeded/finalize。
create 动作 `c8971dc3-6638-49df-b0b4-5540f0813936` 生成工单
`8e30287e-ca10-4aed-8905-603fe7ac0074`（open、revision 1、normal），
回执 input_bound/history_valid 均为 true，recorded/resource revision 均为 1，模型调用为 0。
这是一条 rule 现场成功；本页列出的状态生命周期示例没有逐条现场复做。
完整 CLI 生命周期、恢复和浏览器场景在独立测试库有各自[验收范围](0900_本机业务验收记录.md)。

文档核对还执行了 `decagent --help`、`plugins --help`、`business submit --help`、
`business reply --help`、`ops.local.start serve --help` 和 `worker --help`，均没有模型请求。
固定集三轮运行已完整记录；后续若改进整批失败隔离粒度或启用 Jev，需要另行冻结协议并独立评测。独立 Linux 适配及部署仍待完成。
现版本已修复完成声明与拒绝响应记账，并更新本机 API、business worker 和 Docker repair worker；健康检查正常，原 owner 与工单保留。修复后创建、列表查询、创建并分派三个独立真实案例中，main/hybrid 各成功 3/3，rule 2/3；全部用量完整、禁止写入为零。详见[现版本功能验证](../261013/0158_现版本修复与功能验证.md)。这是有限功能验证；修复前 720 次实测的未通过结论保持原样。
说明见[架构详解](0852_业务Agent架构详解.md)、[离线图册](0902_业务Agent架构图册.html)和[源码索引](../261011/0639_源码结构与脚本职责.md)。

