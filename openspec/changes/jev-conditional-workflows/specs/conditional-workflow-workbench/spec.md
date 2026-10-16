# Spec Delta

## Purpose

让员工能够在工作台理解条件计划如何推进、Jev 为什么选择某个分支、何时需要本人处理以及实际消耗了多少模型资源，并为调试、恢复和使用指南提供一致且可追溯的事实。

## ADDED Requirements

### Requirement: Visible plan and decision state

工作台 MUST 展示计划版本、实际激活路径、节点状态、参数来源、判断类型、原始分数、有效阈值和等待原因。未执行分支 MUST 与已执行节点区分；显示 MUST 与持久任务状态一致，不能以模型文字替代真实状态。

#### Scenario: Branch selected
- **WHEN** 当前计划沿某条分支推进
- **THEN** 员工能识别已走路径、未走分支、当前节点与下一待办

#### Scenario: Unverified submission
- **WHEN** 写入结果仍未知
- **THEN** 界面显示原操作核实状态，不能显示为已完成或可直接重新提交

### Requirement: Distinct human actions

工作台 MUST 为语义选择、参数补充、写入确认、预算处理、重新认证和未知结果核实提供明确区分的交互。低确定性待办 MUST 展示合法候选/评分含义、必要证据、Jev 建议及无匹配/拒绝选项。

#### Scenario: Score correction
- **WHEN** 用户处理低分 Score 决定
- **THEN** 界面列出各档业务含义并允许用户选定，而不是仅提供同意模型按钮

#### Scenario: Semantic selection does not approve write
- **WHEN** 用户完成语义待办后任务需要提交写操作
- **THEN** 界面继续展示具体参数的独立确认，不能将此前回复当作已批准写入

### Requirement: Explainable usage and thresholds

系统 MUST 展示通用模型与 Jev 的实际请求数、token、费用/未知费用、重规划原因和策略版本。阈值说明 MUST 区分答案确定性、实测正确率与统计置信区间；主模型调用 target 与 hard limit MUST 分开显示。

#### Scenario: Fourth primary request
- **WHEN** 任务使用第 4 次通用模型请求
- **THEN** 用户能看到该请求的异常原因和剩余额度，不能仍显示为一次规划调用

#### Scenario: User reads 0.95 certainty setting
- **WHEN** 用户查看 0.95 阈值设置
- **THEN** 界面不承诺 95% 正确率，而指向该策略对应的实际验证指标与适用范围

### Requirement: Correlated protected trace

每次规划、判断、人工响应、工具调用、审批、恢复和费用事件 MUST 能通过任务、计划/节点、模型请求、策略及操作/回执身份关联。系统 MUST 提供授权范围内的脱敏查看或导出，凭据和无权限业务内容不得泄漏。

#### Scenario: Investigate repeated model request
- **WHEN** 员工查看一次异常重规划
- **THEN** trace 可关联触发观察、原计划片段、新修订和实际模型费用

#### Scenario: Switch employee session
- **WHEN** 用户切换员工身份
- **THEN** 前一员工详情、候选和记忆缓存被清理，后续查询重新按当前身份授权

### Requirement: Durable UI recovery

刷新、断线和服务重启后工作台 MUST 重新读取持久任务和待办状态；重复点击 MUST NOT 重复消费人工回复或产生副作用。业务文本 MUST 安全显示，不执行内容中的脚本或指令。

#### Scenario: Browser reconnects while waiting
- **WHEN** 浏览器在等待人工选择时断线重连
- **THEN** 原待办和证据版本重新显示，未经有效回复任务保持等待

#### Scenario: Script in tool text
- **WHEN** 工具结果或记忆包含 HTML/脚本样式内容
- **THEN** 界面将其作为安全文本显示，不触发脚本或自动业务操作

### Requirement: Updated visual operating documentation

交付 MUST 包括当前使用指南、阈值和预算参考、流程扩展说明、架构与评测说明，配备架构图、流程图、泳道图、时序图、恢复状态图及实测结果图。文档 MUST 标明模拟业务、未实现/待验证范围和独立 Linux 验收状态，并与当前入口同步。

#### Scenario: User follows guide
- **WHEN** 用户按指南启动本机服务、提交已注册业务并处理人工待办
- **THEN** 文档所示命令、页面、参数与实际行为一致，能找到中断恢复和失败解释

#### Scenario: No real evaluation evidence
- **WHEN** 某个阈值或性能结果尚未实测
- **THEN** 文档明确标记设计/待验证，不能展示虚构成绩或把脚本夹具当真实模型评测

