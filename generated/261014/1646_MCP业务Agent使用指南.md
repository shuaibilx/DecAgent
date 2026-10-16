---
title: MCP 业务 Agent 使用指南
type: tutorial
audience: [A1, A2, A4]
runs: yes
verified_on: 2026-10-14
sources:
  - ops/local/start.py
  - decagent/cli.py
  - decagent/business/worker.py
  - decagent/gateway/static/business.html
  - compose.windows.yaml
---
# MCP 业务 Agent 使用指南

你将以模拟员工身份提出业务目标，查看 Agent 的判断过程，并核实服务端回执。
当前版本是 business-2、service-tickets 2.0.0、数据库 schema 11。
本机服务已启动在 [员工工作台](http://127.0.0.1:8000/)。
首次阅读可从第 3 步开始；重启电脑后按第 1、2 步启动。

## 1. 确认目录与环境

在 PowerShell 中进入 **DecAgent**，不是原来的 Waku 目录：

```powershell
$DecAgentRoot = Read-Host '请输入 DecAgent 项目目录'
Set-Location $DecAgentRoot
$env:PYTHONUTF8 = '1'
.\.venv\Scripts\python.exe -m pip check
```

本次验证使用项目现有 .venv（Python 3.13），MCP SDK 固定为 2.3.0。
`PYTHONUTF8` 是当前 PowerShell 会话的环境变量，让随后启动的 Python 使用 UTF-8。
它应在终端执行，不是写入 Python 文件的语句。已激活、依赖一致的 Conda 环境也可使用 `python` 替代这里的完整解释器路径；本轮没有重新安装 Conda 环境。

`ops.local.start` 读取项目 .env 与 .decagent/secrets，转换本机数据库地址，
共用 .decagent/local-control 保存 owner、员工凭据及运行证据。
源码在 decagent、ops、evals；生成说明和图片在 generated/日期目录；
运行数据在 .decagent。不要把这些密钥文件复制进文档。

## 2. 启动服务

先启动 Docker Desktop。当前 PostgreSQL 数据卷已经存在，下面的启动命令保留该卷：

```powershell
docker compose -f compose.yaml -f compose.windows.yaml up -d postgres
```

在项目目录依次执行管理命令：

```powershell
.\.venv\Scripts\python.exe -m ops.local.start migrate
.\.venv\Scripts\python.exe -m ops.local.start seed-tickets
.\.venv\Scripts\python.exe -m ops.local.start plugins install-tickets
.\.venv\Scripts\python.exe -m ops.local.start business accounts seed
```

迁移成功显示 Database migration complete。插件版本应为 2.0.0。
重复初始化账号返回 preserved，不会重置现有密码。历史匿名工单保留且只读，
旧未完成 business-1 任务进入恢复待处理状态，不会冒充某位员工继续执行。

新空实例可以用 `python -m ops.local.start init` 初始化 owner 运维凭据。
本机已有 owner，不要重复执行；重复命令会明确报已初始化并保留原凭据。
需要仓库修复时，先按[运维指南](1646_MCP本机运维与恢复.md)构建与启动匹配 schema 11 的 Docker 镜像。

接着打开三个终端，均位于 DecAgent 目录。按顺序分别运行：

```powershell
.\.venv\Scripts\python.exe -m ops.local.start business-server
```

```powershell
.\.venv\Scripts\python.exe -m ops.local.start serve --port 8000
```

```powershell
.\.venv\Scripts\python.exe -m ops.local.start worker --kind business
```

检查 MCP 健康后再提交任务：

```powershell
Invoke-RestMethod http://127.0.0.1:8001/health
Invoke-RestMethod http://127.0.0.1:8000/health
```

MCP 显示 ready=true、service=service-tickets、version=2.0.0。
API 显示 status=ok。API 的健康响应只表示控制面可访问，不代表 MCP 或模型已就绪。

| 服务 | 本机地址 | 职责 |
| --- | --- | --- |
| PostgreSQL | 127.0.0.1:55432 | 业务、任务、批准与 checkpoint |
| 业务 MCP | 127.0.0.1:8001/mcp | 员工范围内的真实模拟业务操作 |
| Web/API | 127.0.0.1:8000 | 员工登录、任务、批准与审计 |
| 修复 broker | 127.0.0.1:8002 | 保留的仓库修复隔离执行 |
| 业务 Worker | 无监听端口 | 认领任务，执行 LangGraph |

MCP 支持主协议 2026-07-28；mcp-cli 0.3.0 使用兼容协议 2025-11-25。
DecAgent 自身只接受主协议。MCP 停止时业务读取和执行会失败或进入恢复，
没有业务 CLI 回退路径。

## 3. 登录员工并完成第一条任务

打开 [工作台](http://127.0.0.1:8000/)，使用 employee-it。
密码保存在本机 .decagent/local-control/demo-auth/employee-it.password，
通过本机编辑器查看并输入。文档不记录明文。

| 登录名 | 姓名 / 团队 | 角色 |
| --- | --- | --- |
| employee-it | 张伟 / IT Support | 普通员工 |
| employee-facilities | 张伟 / Facilities | 普通员工 |
| handler-it | Zhang Wei / IT Support | 处理人 |
| handler-facilities | Zhang Wei / Facilities | 处理人 |
| handler-access | 王芳 / Access Management | 处理人 |
| supervisor | Chen Li / Network | 主管 |

先选择“规则”，输入“查看工单列表”，提交后等待成功。
规则适合明确的单步目标；复杂自然语言使用“主模型”或“规则 + Jev + 主模型”。
当前新契约尚无通过验收的 Jev 校准配置，hybrid 会保守转交主模型，
不能把界面中的 hybrid 选项理解为已经证明 Jev 加速。

然后提出“请创建 Network 工单，VPN 无法连接”。
若规则无法完整理解，会请求具体信息；可以切换主模型或按提示补充，
不要把等待澄清当成程序异常。
本轮已在当前实例通过规则 + 明确 JSON 参数完成创建烟测，
对应 [本机启动记录](1646_MCP本机启动烟测.json)。

成功后查看“任务结果与回执”：动作应为 succeeded，存在服务操作 ID 和核实回执。
“准备成功”或“已批准”仍未代表写入完成。

## 4. 体验多角色业务闭环

创建人只能创建本人请求。主管可以分配与调整优先级；处理人只能处理当前获分配工单。
按以下顺序切换账号，使用上一步返回的工单 ID：

| 当前账号 | 目标 | 可核实的变化 |
| --- | --- | --- |
| employee-it | 创建工单 | open，revision 1，本人 requester |
| supervisor | 分配给 IT Support 的 Zhang Wei | assigned，assignee 为真实目录 ID |
| handler-it | 开始该工单 | in_progress |
| handler-it | 提供实际处置说明并请求解决 | 待本人确认；提交后 resolved |
| employee-it | 关闭本人已解决工单 | 待本人确认；提交后 closed |

同名“张伟”和“Zhang Wei”都保留多个候选，必须用团队消歧。
中文张伟的两个账号是普通员工，不能成为分配目标。
“解决”记录你提供的处置说明，本系统不会实际修理 VPN 或公司设备。

切换账号后，任务、事件和工单视图会清空并重新按身份查询。
另一员工或主管不能代签原员工的操作，也不能接管其任务。

## 5. 确认、编辑与恢复

“待本人处理”用于 Agent 的澄清与具体动作确认；
“统一操作待办”也能看到独立 MCP 客户端准备的操作。
核对来源、参数、准备时 revision 与输入摘要后再选择批准、拒绝或编辑。

批准有效期为 15 分钟。修改参数会形成新操作并再次确认。
Agent 编辑由图重新规划新动作；外部 MCP 编辑由服务返回 supersedes 关联的新操作。
原 operation_id 及原输入保持不变。

| 状态 | 下一步 |
| --- | --- |
| awaiting_input | 补充实际信息；凭据到期时原员工重新登录并恢复 |
| awaiting_approval | 本人核对具体参数后回复 |
| paused_budget | 在原任务提高预算后恢复 |
| recovering / unknown | 先核实原操作回执，再按页面提示恢复 |
| succeeded | 查看回执和资源 revision |
| failed / rejected | 阅读实际错误或拒绝原因，再提出新的合法请求 |

断线后继续查看原任务与原 operation_id。
服务会排除在途事务，核对回执、历史和资源；只有证明没有提交且当前授权仍有效，
才能沿原 ID 恢复。不要通过不断新建相同任务来处理未知结果。

## 6. 使用独立 MCP 客户端

在“独立 MCP 凭据”中申请 1 小时凭据。网页显示受保护文件路径，
不显示明文 token。按 [MCP 命令行调用指南](../261013/2050_MCP命令行调用指南.md)
将该文件读到当前进程环境变量，再执行 info、prepare、commit、get_operation。

外部 MCP 操作与 Agent 共用同一批准核心。MCP Bearer 只能证明员工身份，
不能代替网页批准，也不能伪造 Agent 的预算、租约或执行 grant。

## 7. 停止与重新启动

前台运行时，在 Worker、API、MCP 三个终端依次按 Ctrl+C。
保持 PostgreSQL 数据卷，下次按第 2 步启动。中断写入后先核实原操作。
本次由助手启动的后台进程记录在 .decagent/local-control/mcp-processes.json，
日志为 api、mcp、business-worker 的 stdout.log / stderr.log。

备份与版本回滚见 [本机运维与恢复](1646_MCP本机运维与恢复.md)。
架构、Jev 三类判断和事务边界见 [架构详解](1646_MCP业务Agent架构详解.md)。
