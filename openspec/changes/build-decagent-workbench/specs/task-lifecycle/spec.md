# Spec Delta

## Purpose

为一次仓库修复提供可持久化的任务生命周期，使用户能够检查任务授权、当前阶段和停止原因，并在人工介入或服务重启后继续工作。任务进度与副作用执行证据必须可追溯，不能仅依赖聊天历史恢复。

## ADDED Requirements

### Requirement: Explicit task authorization
系统 MUST 在创建任务时保存仓库、基础提交、问题描述、代码信任范围、执行预算和是否授权发布草稿 PR；缺少必要授权时 MUST 拒绝创建可执行任务。

#### Scenario: Authorized task accepted
- **WHEN** 已认证用户为允许访问的仓库指定问题、基础提交、预算与草稿 PR 授权
- **THEN** 系统创建唯一任务并返回任务标识及不可由仓库内容修改的授权记录

#### Scenario: Repository instructions cannot expand authorization
- **WHEN** 仓库文件要求切换到其他仓库或发布未获授权的变更
- **THEN** 系统维持原任务范围并阻止该操作

### Requirement: Persistent and truthful progress
系统 SHALL 持久化任务阶段、执行状态、已完成操作和停止原因；等待审批、预算耗尽、失败、取消与成功 MUST 在用户界面上可区分，未验证的任务 MUST NOT 标为修复成功。

#### Scenario: Human approval is pending
- **WHEN** 一个操作需要人工批准
- **THEN** 用户看到等待状态、待批准操作和暂停原因，任务不继续执行该操作

#### Scenario: Verification fails
- **WHEN** 修复后的回归检查失败
- **THEN** 任务保留失败证据并继续在预算内修复或明确停止，不报告修复成功

### Requirement: Recover without repeating known effects
系统 MUST 在服务重启后恢复持久化任务；对已确认完成的操作 MUST 复用执行凭据。对结果不明的外部副作用 MUST 先核实结果或请求人工处理，不得盲目重试。

#### Scenario: Restart while waiting for approval
- **WHEN** 服务在等待审批期间重启
- **THEN** 同一任务和审批仍可查看，批准后从正确阶段继续

#### Scenario: Restart after a PR was created
- **WHEN** GitHub 已创建 PR，但服务在写入本地完成状态前崩溃
- **THEN** 恢复流程核实已有 PR，并关联它而不是重复创建

### Requirement: Exclusive execution ownership
系统 MUST 确保同一任务在同一时刻只有一个执行者拥有有效执行权；重复投递、过期执行者和重复恢复请求 MUST NOT 引发重复工具执行。

#### Scenario: Duplicate task delivery
- **WHEN** 两个 worker 同时获取同一个待执行任务
- **THEN** 只有一个 worker 可以启动操作，另一个观察到该任务已被领取

### Requirement: Enforced execution budgets
系统 MUST 在执行前检查任务的时间、模型调用和费用预算，并在限额达到时停止新增操作、保存证据并呈现停止原因。费用无法估算时 MUST 明示未知，不能以零费用代替。

#### Scenario: Budget exhausted before a tool call
- **WHEN** 下一个操作将超过配置的预算限制
- **THEN** 操作不启动，任务进入预算暂停状态，用户可调整预算后继续

### Requirement: Cancellation and resumable pauses
系统 SHALL 允许用户取消任务及恢复可恢复暂停；取消 MUST 停止新增操作并终止受控执行进程，保留审计和已有产物。不可撤回的既有外部副作用 MUST 如实显示。

#### Scenario: User cancels a running test
- **WHEN** 用户取消正在运行测试的任务
- **THEN** 系统终止对应进程及其子进程，记录取消结果并保留已有日志

#### Scenario: Cancellation after publication
- **WHEN** 用户取消已经创建草稿 PR 的任务
- **THEN** 系统保留并显示 PR 链接，不声称撤销了该外部操作
