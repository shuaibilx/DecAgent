# Spec Delta

## Purpose

使业务任务拥有独立于仓库修复的授权、执行阶段和恢复依据，用户无需提供 GitHub 仓库或提交版本即可提交业务请求。任务进度必须由真实执行记录支持，并且在预算、人工介入或服务重启时保持可检查。

## ADDED Requirements

### Requirement: Independent business tasks
业务任务 MUST 保存作用域、指令、插件版本摘要、用户来源和预算，不要求仓库、提交 SHA 或修复执行器。历史修复任务 MUST 保持原有必需字段和 API 行为。

#### Scenario: No GitHub configuration
- **WHEN** 用户在未配置 GitHub 和修复执行器时提交有效业务任务
- **THEN** 系统创建业务任务并运行其已配置的业务路径

#### Scenario: Legacy task recovered
- **WHEN** 更新后恢复已有修复任务
- **THEN** 系统按原任务类型和兼容图版本处理，不误入业务流程

### Requirement: Durable bounded execution
系统 MUST 持久化业务任务进度、判断、动作与等待原因；MUST 独占领取任务并执行调用、token、费用与机器运行时间预算。取消 MUST 停止新增操作；等待用户的时间 MUST 单独记录。

#### Scenario: Restart during clarification
- **WHEN** 服务在等待用户补充参数时重启
- **THEN** 原问题与任务仍可查看，回复后继续且不重复已完成操作

#### Scenario: Budget or cancellation
- **WHEN** 任务预算耗尽或用户取消任务
- **THEN** 不启动新的模型调用或业务动作，保存可见停止原因

#### Scenario: Complete primary response contains an invalid proposal
- **WHEN** 主模型返回完整响应或正常结束的完整流，包含合法且总数一致的 token 用量，但提案未通过契约或安全校验
- **THEN** 拒绝全部提案并保留既有失败及人工介入策略，按已验证用量和既有价格结算，保存安全的实际模型名及固定失败原因；不得把拒绝当成未知用量或成功执行

#### Scenario: Primary usage remains unknown
- **WHEN** 主模型传输中断、流未完整结束，或响应中的用量缺失或非法
- **THEN** 缺失用量或费用仍保留未知及预算约束，不根据免费额度补造 tokens 或费用，也不从任意异常属性中取数

#### Scenario: Primary request rejected before dispatch
- **WHEN** 可信本地预检或派发前守卫明确阻止请求且供应商客户端尚未调用
- **THEN** 不计入实际 tokens 或费用；通用响应错误不构成未派发证据，历史未知记录不能据此回填

#### Scenario: Legacy repair sees a rejected primary response
- **WHEN** 旧修复运行时收到完整但被拒绝的主模型响应
- **THEN** 不接受提案，保存安全拒绝及已知用量证据，保留等待核实与重启后不自动重发的行为，不错误声称所有已结算 reservation 仍未核实

#### Scenario: Owner primary diagnostic receives a rejected response
- **WHEN** 拥有者选定的主模型诊断收到完整但被拒绝的响应，或可信本地预检阻止派发
- **THEN** 持久化安全失败及可核实用量，保留未知价格的费用预留；已知完整用量按既有价格结算，未派发按零结算，同一操作 ID 不重复请求

### Requirement: Goal-specific completion
系统 MUST 根据本次任务的目标和核实结果判断完成，不能把工单未关闭当作创建任务失败，也不能把模型声称完成当作业务成功。

#### Scenario: Create and assign completed
- **WHEN** 创建和分派均有可核实回执而工单仍为 assigned
- **THEN** Agent 任务成功，界面保留工单实际状态

#### Scenario: Completion claim omits supplementary reads
- **WHEN** 主模型的完成声明引用全部已核实写入及真实目标操作，但没有列出辅助 directory、get 或 receipt 读取
- **THEN** 系统不因漏列辅助读取判任务失败；所有声明 ID 必须来自本任务已核实操作，已执行写入不能漏列，完整操作记录及最终目标核实仍保留

#### Scenario: Completion claim omits a write or cites an unknown action
- **WHEN** 完成声明漏列已执行写入，或引用陌生、其他任务或未核实动作 ID
- **THEN** 不接受完成声明，不据模型 summary 宣布成功

#### Scenario: Goal cannot be established by the program
- **WHEN** 现有确定性目标识别及 literal 命令均不能建立可核实的注册目标
- **THEN** 不因辅助读取成功接受目标完成，保存程序给出的 goal_unproven 原因及已有动作证据，不启动新的副作用

