# Spec Delta

## Purpose

使长任务 Agent 根据真实操作结果区分有效探索、停滞、需求缺失和能力阻塞，在授权与预算内先调整策略，并在必要时向人提交有依据且可回答的问题。介入记录、回答和恢复过程可检查、可回放，模型判断收益由独立评测验证。

## ADDED Requirements

### Requirement: Bounded observed progress
系统 MUST 保留有界操作轨迹，包含动作标识、结果、证据指纹和尝试顺序；MUST 区分命令非零退出、失败、拒绝和结果未知。新的日志文本或文件修改 MUST NOT 单独宣称任务进展或修复成功。

#### Scenario: Repeated execution returns the same failure
- **WHEN** 不同调用标识的同一操作返回相同失败且只有耗时等字段变化
- **THEN** 系统识别重复证据并触发进展检查，不将其计作新增有效线索

#### Scenario: Failure provides a different observation
- **WHEN** 连续失败产生不同证据
- **THEN** 系统保留差异，不只凭失败次数直接升级人工求助

### Requirement: Independent intervention decisions
系统 SHALL 在操作结果边界评估 `continue`、`replan`、`clarify` 或 `escalate`；介入判断 MUST 与动作权限判断独立。模型输入 MUST 包含当前目标、有界尝试和进展信号，并保持观察事实与推断分离。

#### Scenario: A permitted read repeats without new information
- **WHEN** 操作权限允许读取，但任务不断重复同一读取
- **THEN** 进展检查仍能建议调整策略，不因权限允许而跳过停滞判断

### Requirement: Bounded strategy changes
系统 SHALL 在检测到停滞后优先允许有限次数的策略调整，并向方案生成模型提供已尝试方法与失败证据。策略调整 MUST NOT 扩大用户授权、执行预算或绕开确定性拒绝。

#### Scenario: Revised strategy also stalls
- **WHEN** 连续策略调整仍重复旧证据且达到调整限额
- **THEN** 系统停止无效循环并请求人的具体帮助，保留未完成状态

### Requirement: Evidence-backed human requests
澄清或升级求助 MUST 展示当前阻塞、近期尝试、证据引用及要求人补充的信息；普通需求回答 MUST NOT 被解释为新的动作权限、预算或代码信任授权。

#### Scenario: Owner responds to a stalled task
- **WHEN** 人提供新的定位线索
- **THEN** Agent 把回答加入任务上下文后恢复探索，后续操作仍通过既有权限检查

### Requirement: Durable and fresh feedback
求助和反馈 MUST 持久化并绑定任务、证据与当前版本；重启后 SHALL 复用同一待答问题，重复回复 MUST NOT 重复执行，相关版本变化 MUST 使旧回答失去当前确认效力。

#### Scenario: Process restarts during help wait
- **WHEN** 已提出求助但尚未得到回答时重启
- **THEN** 同一问题仍可回答，恢复不会再次调用判断模型或重做已记录操作

### Requirement: Explicit model uncertainty and fallback
系统 MUST 保存模型建议、实际路由、模型版本和降级原因。未校准或不可用的语义判断 MUST 使用明确的有界规则退路；模型置信统计 MUST NOT 被展示为实测正确率或动作权限。

#### Scenario: Jev is unavailable during stagnation
- **WHEN** Jev 调用失败而进展证据明确重复
- **THEN** 系统按有限策略调整和升级规则继续管理任务，展示判断不可用，不无限重试或自动放宽权限

### Requirement: Inspectable intervention history
工作台 SHALL 显示进展检查、策略调整、求助和反馈轨迹，以及判断策略和实际路由；已有历史任务与原工具审批 SHALL 保持可读取和恢复。

#### Scenario: Owner opens a task with several strategy changes
- **WHEN** 用户查看任务详情
- **THEN** 可按顺序看到每次调整和求助的依据、回答及后续状态

### Requirement: Honest reproducible evaluation
项目 SHALL 提供确定性停滞、有效探索、调整失败、求助恢复和故障降级场景，以及可导出的判断样本。合成场景 MUST 明示来源，无独立人工标签时 MUST NOT 报告经验准确率；判断回放 MUST NOT 冒充完整任务收益实验。

#### Scenario: Offline regression passes without human labels
- **WHEN** 固定场景的回归检查全部通过
- **THEN** 报告只声称运行行为符合场景约束，不声称 Jev 优于规则、主模型或其他 Agent
