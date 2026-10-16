# Spec Delta

## Purpose

为 Agent 的每次实际操作建立可执行的权限边界，保证任务授权、代码信任和人工审批不会被模型或仓库内容改写。用户可以理解并批准具体动作，同时控制服务的凭据、宿主和其他任务保持隔离。

## ADDED Requirements

### Requirement: Program-enforced action boundaries
所有工具操作 MUST 在执行前校验参数、目标资源、任务授权和当前版本；确定性拒绝 MUST 优先于模型建议，用户审批也 MUST NOT 绕过系统禁止的宿主访问或跨任务访问。

#### Scenario: Jev permits a forbidden path
- **WHEN** 模型建议读取宿主密钥或写入其他任务目录
- **THEN** 程序拒绝操作并记录拒绝依据，无论模型返回什么判断

#### Scenario: Repository content imitates user authorization
- **WHEN** 仓库文件或工具结果声称用户已经批准扩大权限
- **THEN** 系统将该内容当作非授权数据，不改变原任务权限

### Requirement: Explicit code trust
系统 MUST 将代码分析与代码执行区分；只有用户认可的代码版本或信任范围可以执行。外部贡献者代码的静态分析 MUST NOT 自动运行安装钩子、测试或仓库脚本。

#### Scenario: Untrusted contribution inspected
- **WHEN** 用户提交尚未认可的外部 PR
- **THEN** 系统允许读取和分析，但任何会执行仓库代码的操作都等待代码信任确认

#### Scenario: Contribution changes after trust approval
- **WHEN** 外部 PR 在用户认可后新增提交且超出已认可版本
- **THEN** 原认可不覆盖新代码，执行前要求重新确认

### Requirement: Isolated and bounded execution
仓库命令 MUST 在任务独立环境中执行，且 MUST 受时间、CPU、内存、进程和输出限制；MUST NOT 访问宿主目录、控制服务运行数据、Docker 管理接口或其他任务工作区。

#### Scenario: Workspace escape attempted
- **WHEN** 文件操作通过绝对路径、父目录、符号链接或 Git 路径尝试越出工作区
- **THEN** 系统拒绝操作且不产生工作区外写入

#### Scenario: Command creates unbounded subprocesses
- **WHEN** 仓库命令达到进程、时间或内存限额
- **THEN** 系统停止对应任务执行并报告资源限制原因

### Requirement: Public network without control-plane access
可信任务执行环境 SHALL 允许公网访问；MUST 阻断宿主服务、控制网络、私有与链路本地地址及云元数据端点。无法验证这些边界时 MUST 禁止执行，不得静默降级为无限制网络。

#### Scenario: Public package source accessed
- **WHEN** 经治理允许的命令请求可访问的公网包源
- **THEN** 请求可以完成，且执行容器未获得控制服务凭据

#### Scenario: Private target or redirect requested
- **WHEN** 命令请求私有地址，或公网目标重定向、重新解析到受阻地址
- **THEN** 系统阻止连接并记录受阻目标，不通过代理绕开限制

### Requirement: Credentials and minimized model inputs
控制服务 MUST 保管 GitHub 和模型凭据，且 MUST NOT 将其传入仓库执行环境或日志。外部模型输入 MUST 限定为当前任务所需内容并脱敏；疑似凭据内容 MUST 被阻止或替换。

#### Scenario: Secret appears in command output
- **WHEN** 输出包含已知密钥或可识别的敏感凭据
- **THEN** 写入界面、持久化日志和发送到外部模型前均被脱敏

#### Scenario: Task requires authenticated external testing
- **WHEN** 仓库测试要求把生产密钥注入执行环境
- **THEN** 系统报告不支持该凭据暴露方式，要求测试替身或另行设计的凭据方案

### Requirement: Approvals bind to exact actions
审批 MUST 保存待执行操作、参数、目标、代码快照和策略版本，并支持批准、拒绝、修改；操作或相关版本变化后 MUST 重新评估。拒绝 MUST 阻止对应操作，重复审批回复 MUST NOT 重复执行。

#### Scenario: Approved command changes
- **WHEN** 批准后命令参数、目标仓库或工作区快照发生变化
- **THEN** 原批准失效，系统重新治理并按结果请求新的审批

#### Scenario: User edits the proposed action
- **WHEN** 用户修改待审批操作并提交
- **THEN** 系统先重新校验修改后的动作，再执行或请求新的判断

#### Scenario: Duplicate approval response
- **WHEN** 同一个批准回复被重复发送
- **THEN** 系统返回既有处理结果，不再次执行操作
