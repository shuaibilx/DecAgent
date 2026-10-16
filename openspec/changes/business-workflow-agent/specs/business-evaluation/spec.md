# Spec Delta

## Purpose

以可复现的合成业务案例检验 Jev 混合路径的正确性、覆盖率、延迟和降级行为，让项目特色由实测支持。评测须公开数据与模型版本、失败分母和真实限制，不能把决策回放当作端到端业务收益。

## ADDED Requirements

### Requirement: Fair reproducible comparison
规则、主模型和 Jev 混合策略 MUST 使用相同案例、业务规则、候选数据、工具及执行约束。评测 MUST 分离开发与固定验收集，记录成功、失败、确认、降级、调用量及机器耗时，人工等待另计。

#### Scenario: Model baseline supplied
- **WHEN** 比较主模型与增强流程
- **THEN** 主模型获得同样业务契约，不能通过隐藏规则制造 Jev 优势

#### Scenario: Supplementary verification reads
- **WHEN** 策略完成目标动作，并额外执行插件契约声明的只读查询
- **THEN** 评分核实全部目标动作及参数的顺序、写入和最终业务状态，不因补充查询判失败；缺失目标读取、额外写入或参数错误仍判失败

#### Scenario: Read before clarification
- **WHEN** 任务通过只读查询发现仍需用户澄清
- **THEN** 澄清目标可判正确，但任何尝试写入均不得被忽略

### Requirement: Honest performance acceptance
验收 MUST 要求混合路径正确率不低于主模型、禁止操作为零、常见流程机器耗时中位数下降、混合任务集累计机器耗时不增加。未满足或未完成实测 MUST 明确报告，不能预设提速结论。

#### Scenario: Jev slower
- **WHEN** 真实对照表明 Jev 混合路径更慢
- **THEN** 报告失败及实际指标，不删去失败案例或宣布提效通过

### Requirement: Scoped data and spending
首版 SHALL 默认生成 120 个合成案例，40 个用于开发、80 个固定验收并交错运行三轮；标签 MUST 明示来源。真实 Jev 调用 MUST 遵守既有累计 1 美元上限，预算不足 MUST 停止并保留未完成状态。

#### Scenario: Offline evidence only
- **WHEN** 仅完成确定性测试与模拟模型测试
- **THEN** 报告离线行为验收，不将其称为真实模型性能通过

#### Scenario: Known-cost invalid development response
- **WHEN** 冻结的 v2 开发采集协议遇到费用已知、tokens 完整、HTTP 2xx 且绑定完整的 invalid_response
- **THEN** 原样记录失败而不重复筛选成功答案、不将无效答案用于拟合，并禁用该请求涉及的判断 family；收集完成与有效覆盖分开报告

#### Scenario: Unknown development spending
- **WHEN** 开发采集的 usage、实际费用或模型绑定无法核实
- **THEN** 立即停止并保存部分结果，不能以失败兼容协议绕过预算和绑定要求

### Requirement: Explicit deployment pending items
本机实施 SHALL 可验收；缺少独立 Linux 主机时 MUST 保留该部署待办，不由 Docker 内测试替代独立主机验收。

#### Scenario: Local containers pass
- **WHEN** Windows 本机容器通过业务集成测试
- **THEN** 报告该环境证据，并保留独立 Linux 部署未验收

