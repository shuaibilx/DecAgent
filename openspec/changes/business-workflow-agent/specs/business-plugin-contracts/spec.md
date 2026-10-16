# Spec Delta

## Purpose

为独立业务智能体提供可发现、可验证、可冻结版本的插件能力，让 CLI 与 Skill 能按基础和流程增强两个级别接入。执行能力来自用户注册的可信契约，而不是模型对技能文本或命令帮助的猜测。

## ADDED Requirements

### Requirement: Two admission levels
插件 MUST 提供版本、可信 CLI 入口、命令白名单、JSON Schema 参数、操作影响和结果格式；增强插件 MUST 额外提供版本化流程、分支条件、判断题、参数来源和完成条件。缺少必需契约 MUST 拒绝注册。

#### Scenario: Basic plugin admitted
- **WHEN** 用户注册完整基础契约但未提供流程
- **THEN** 插件可由主模型规划调用，系统不声称其拥有流程快速路径

#### Scenario: Incomplete catalog rejected
- **WHEN** 插件仅提供命令名称和帮助文本
- **THEN** 系统指出缺失的参数和执行契约，不生成虚假的可执行能力

### Requirement: Shared frozen capability definitions
模型可见工具和程序参数校验 MUST 来自同一注册表；任务 MUST 冻结插件版本及内容摘要。禁用插件或版本变化后 MUST 阻止旧契约未经复核执行。

#### Scenario: Unknown argument generated
- **WHEN** 主模型生成契约未声明的参数或命令
- **THEN** 执行前拒绝，并提供具体校验错误

#### Scenario: Registered contract changes
- **WHEN** 任务运行中已注册插件的内容摘要改变
- **THEN** 旧任务暂停，要求显式复核，不静默换用新能力

### Requirement: Controlled CLI execution
执行 MUST 使用用户注册的固定可执行入口和参数列表，不经 shell 拼接；MUST 限制运行时长、输出和子进程，最小化传递环境变量，并严格解析声明的 JSON 回执。Skill 与工具输出 MUST NOT 扩大授权。

#### Scenario: Shell metacharacters in data
- **WHEN** 业务文本包含命令替换或 shell 分隔符
- **THEN** 它们作为数据参数传递，不执行额外命令

#### Scenario: Invalid or excessive CLI output
- **WHEN** CLI 超时、输出超限或返回不符合契约的 JSON
- **THEN** 操作明确失败或结果不明，且不将未核实结果当作成功
