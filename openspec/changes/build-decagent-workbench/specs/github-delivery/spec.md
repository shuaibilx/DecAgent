# Spec Delta

## Purpose

将经过验证的修复交付为指定 GitHub 仓库中的草稿 PR，使任务授权、发布内容与检查证据保持一致。交付过程能够处理权限错误和结果不明的重试，并提供可靠的合并事实供项目记忆激活使用。

## ADDED Requirements

### Requirement: Repository-scoped GitHub access
系统 MUST 使用用户配置的细粒度凭据访问明确授权的仓库，并检查任务所需权限；凭据权限更广 MUST NOT 扩大 Agent 的任务范围。读取或发布失败 MUST 提供脱敏的可操作错误。

#### Scenario: Token can access another repository
- **WHEN** Token 允许访问多个仓库，但任务只授权其中一个
- **THEN** Agent 只能访问任务授权的仓库

#### Scenario: Pull request permission is missing
- **WHEN** 凭据无法创建草稿 PR
- **THEN** 系统保留本地修复证据并报告缺失权限，不反复发布或声称已创建 PR

### Requirement: Automatic draft publication requires evidence
只有任务明确授权发布，且具备关联缺陷的基线失败、独立修复通过和有效回归通过证据时，系统 SHALL 自动发布草稿 PR；缺少条件 MUST 暂停自动交付并呈现原因。

#### Scenario: Evidence and authorization are complete
- **WHEN** 同一修复提交满足全部验证条件且任务允许草稿 PR
- **THEN** 系统向指定仓库的任务分支发布，并创建草稿 PR

#### Scenario: Only modified tests pass
- **WHEN** 现有测试修改后通过，但没有独立缺陷修复证据
- **THEN** 系统不自动发布，并请求用户确认必要的验收条件

### Requirement: Publication is bound to verified content
发布 MUST 绑定已验证的提交或工作区内容哈希、目标仓库和基础分支；验证后内容变化 MUST 重新验证。系统 MUST NOT 自动推送主分支、强制覆盖分支或合并 PR。

#### Scenario: Patch changes after verification
- **WHEN** Agent 在检查通过后继续修改源码或测试
- **THEN** 旧通过结果不能授权新内容发布，系统重新验证

#### Scenario: Base branch advances incompatibly
- **WHEN** 目标基础分支变化导致修复无法以已验证条件交付
- **THEN** 系统重新准备和验证，无法处理时明确阻塞

### Requirement: Recoverable and deduplicated delivery
每次交付 MUST 有稳定任务标识和发布凭据；网络失败或服务重启后 MUST 核实分支与 PR 是否已存在，再决定重试。不能确认结果时 MUST 停止自动重试并请求处理。

#### Scenario: GitHub response is lost
- **WHEN** GitHub 完成 PR 创建但响应丢失
- **THEN** 系统通过任务分支和标识找到既有 PR，并复用其链接

### Requirement: Evidence-rich draft description
草稿 PR MUST 包含原问题、修改行为、基础与修复版本、复现和回归结果、测试修改及剩余限制；用户 SHALL 从任务界面访问 PR 和对应证据，描述不得包含密钥或编造通过结果。

#### Scenario: Draft is delivered
- **WHEN** 草稿 PR 创建成功
- **THEN** 任务展示 PR 链接，PR 描述包含可核查的验证摘要与测试变更说明

### Requirement: Verified merge observations
系统 MUST 从 GitHub 核实来源 PR 的合并或关闭状态，并记录实际合并提交及相关内容；单纯发布、审批通过或关闭 MUST NOT 等同于合并。观察重试 MUST 不重复激活记忆。

#### Scenario: Squash merge occurs
- **WHEN** 来源 PR 通过 squash 合并，最终提交不同于修复分支提交
- **THEN** 系统使用实际合并内容检查记忆适用性，而不只比较提交是否相等
