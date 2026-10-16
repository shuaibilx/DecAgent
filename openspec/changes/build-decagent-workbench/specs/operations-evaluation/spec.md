# Spec Delta

## Purpose

使 DecAgent 能够作为可维护的自部署产品运行，并通过明确证据验证运行可靠性和决策层价值。部署、诊断、恢复、成本记录和对照评测需要可复现，实际限制及未验证结果必须如实呈现。

## ADDED Requirements

### Requirement: Reproducible self-host deployment
系统 SHALL 提供 Linux 容器的 Compose 部署、配置模板及 Windows Docker Desktop/WSL2 指南；启动诊断 MUST 检查数据库、worker、执行边界和配置，边界检查失败 MUST 阻止仓库执行。

#### Scenario: First deployment succeeds
- **WHEN** 用户按指南配置依赖、凭据和拥有者认证并启动服务
- **THEN** 用户可登录工作台，诊断显示各服务状态，且无需手工编辑源码

#### Scenario: Executor isolation is unavailable
- **WHEN** 部署不能证明执行容器隔离控制网络和宿主服务
- **THEN** 系统报告阻塞并保持执行不可用，不改为宿主直接运行

### Requirement: Recoverable persistent data
系统 MUST 提供任务、审批、记忆及关联证据的备份恢复流程，验证备份一致性；恢复 MUST 保留标识与状态关系，默认操作 MUST NOT 清空原 Waku 或现有 DecAgent 数据。

#### Scenario: Instance is restored from a backup
- **WHEN** 用户在停止执行后恢复数据库和对应证据备份
- **THEN** 已有任务、审批和记忆可访问，外部副作用先核实再继续

### Requirement: Complete redacted audit and usage
系统 MUST 关联记录任务、操作、决策、审批、验证、记忆版本和发布凭据；所有主模型、决策和提取调用及重试 MUST 纳入使用量记录。未知费用 MUST 标为未知而不是零。

#### Scenario: Jev call is retried
- **WHEN** 决策请求发生有限重试
- **THEN** 每次请求的已知使用量或未知状态进入任务费用记录

#### Scenario: User exports a task report
- **WHEN** 用户导出任务审计
- **THEN** 报告含可追溯证据引用和费用说明，但不含密钥或未经脱敏的敏感内容

### Requirement: Separate deterministic and live verification
项目 MUST 提供不依赖真实模型的权限、审批、恢复、记忆和交付回归测试；真实服务与执行环境集成测试 MUST 显式标记，缺少服务时标为未验证而不能冒充通过。

#### Scenario: Offline regression runs
- **WHEN** 测试环境没有任何模型密钥
- **THEN** 确定性回归仍可运行，且不会隐式访问外部模型

#### Scenario: Live Jev credentials are absent
- **WHEN** 发布检查无法调用真实 Jev
- **THEN** 报告明确列出真实接入尚未验证，不将 mock 测试当作真实接入成功

### Requirement: Comparable decision ablations
系统 SHALL 在相同任务授权、工具、硬策略和验证条件下比较规则、主模型判断与 Jev 判断；标签与评测集 MUST 独立于被评测的判断模型。报告 MUST 包含误放行、误中断、记忆质量、成功率、延迟和费用，不预设 Jev 更好。

#### Scenario: Decision strategies compared
- **WHEN** 同一人工标注的保留评测集分别运行三种策略
- **THEN** 报告提供样本数、各类结果及策略、模型和数据集版本

#### Scenario: Jev does not improve results
- **WHEN** Jev 的任务效果更差或成本收益不成立
- **THEN** 报告保留这一结果并说明适用限制，不宣称已证明性能提升

### Requirement: Budget-controlled reproducible live runs
真实模型评测 MUST 在运行前给出样本范围、模型、调用限制、价格依据及费用上限，并取得运行授权；达到限额 MUST 停止新增调用。数据集 MUST 保存来源、许可、提交与复现条件。

#### Scenario: Evaluation budget reached
- **WHEN** 对照评测达到配置费用或调用上限
- **THEN** 系统停止启动新样本，报告已完成与未完成样本，而不缩减结果分母隐藏未完成部分

### Requirement: Release evidence reflects product readiness
发布检查 MUST 分别报告静态检查、离线回归、数据库和容器集成、真实模型及真实仓库修复闭环结果；自动交付验收 MUST 使用授权测试仓库，不操作未授权仓库或合并 PR。

#### Scenario: End-to-end release verification
- **WHEN** 在授权 Python 仓库完成真实复现、修复、验证、草稿 PR 和一次中断恢复
- **THEN** 发布报告关联真实任务与 PR 证据，未完成的检查仍明确列出

### Requirement: Verified GHCR release and local deployment
版本发布 MUST 在完整 CI 通过后，才将 control、executor 和 runner 镜像发布到 GHCR；镜像 MUST 同时具有语义版本标签和不可变 digest。发布标签 MUST 符合 `vMAJOR.MINOR.PATCH`，且对应提交必须是 `main` 的祖先。部署 MUST 通过专用 `local-deploy` environment 和本机 Windows Docker Desktop Runner 执行；Runner MUST 不接收 Pull Request 或其他不受信任的工作流任务。部署必须沿用当前 Compose 项目与 Windows override，迁移前创建数据库备份，启动后通过健康检查；不得删除或重建持久卷。

#### Scenario: Pull Request cannot reach the local runner
- **WHEN** 外部或内部 Pull Request 触发检查
- **THEN** 所有检查仅使用 GitHub 托管 runner，本机 Runner 不会接收该工作流任务

#### Scenario: Versioned release passes CI
- **WHEN** `main` 可达的版本标签完成全部 CI 检查
- **THEN** 发布工作流向 GHCR 推送三类镜像，并以对应 digest 更新本机服务

#### Scenario: Local deployment fails health verification
- **WHEN** 迁移或服务更新未通过本机健康检查
- **THEN** 发布任务失败且部署前数据库备份保留，现有数据卷不被删除
