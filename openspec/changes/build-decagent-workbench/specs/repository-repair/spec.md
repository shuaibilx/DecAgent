# Spec Delta

## Purpose

让用户将 Python 缺陷或失败测试交给 Agent，获得能够独立核查的修复结果。该能力覆盖项目准备、问题复现、源码和测试修改、回归验证，并区分真正修复与通过改变检查条件获得的表面通过。

## ADDED Requirements

### Requirement: Automatically infer project preparation
系统 SHALL 从仓库配置和任务描述推断 Python 版本、依赖安装、复现及回归命令；执行前 MUST 记录命令与来源并经过操作治理。推断冲突或依赖缺失 MUST 明示并请求必要信息。

#### Scenario: Recognized Python repository
- **WHEN** 仓库包含可识别的 Python 依赖声明和测试配置
- **THEN** 系统提出并记录准备与检查命令，在授权和治理通过后执行

#### Scenario: Unsupported environment requirement
- **WHEN** 复现依赖未配置的私有凭据、GPU 或其他不受支持的运行环境
- **THEN** 系统报告具体阻塞，不伪造运行结果或在宿主机直接执行

### Requirement: Preserve baseline failure evidence
系统 MUST 在修改前保存基础提交、环境、执行命令、收集到的用例、失败输出和可重复的复现材料；这些材料 MUST 独立于可修改工作区保存。

#### Scenario: Failure reproduced
- **WHEN** 在基础提交运行复现检查得到与任务相关的失败
- **THEN** 系统保存基线证据，并将该失败与任务问题关联

#### Scenario: Issue cannot be reproduced
- **WHEN** 现有描述和测试无法得到相关失败
- **THEN** 系统继续在预算内调查或请求用户补充信息，不自动宣称完成修复

### Requirement: Reviewable code and test changes
系统 SHALL 允许 Agent 修改授权工作区中的源码和测试，并展示完整差异；测试删除、跳过、断言放宽或检查范围减少 MUST 被显式标注，不能仅因修改后检查通过就视为修复成功。

#### Scenario: Existing test is updated
- **WHEN** Agent 修改原测试以匹配它推断的正确行为
- **THEN** 用户能够查看测试差异及修改理由，系统仍要求独立修复证据

#### Scenario: Failing test is skipped
- **WHEN** 修改将失败用例改成 skip 或从检查命令中排除
- **THEN** 系统标注检查被削弱，并阻止仅依据这次通过结果自动发布

### Requirement: Verify the defect independently
系统 MUST 在修改后执行不可被 Agent 静默改写的复现检查，并证明同一缺陷从失败转为通过。若原检查本身过时或无法保持独立性，系统 MUST 请求用户确认新的验收条件后重新验证。

#### Scenario: Same reproduction passes after repair
- **WHEN** 固定复现材料在基础提交失败、在修复提交通过
- **THEN** 系统生成绑定两个提交及同一检查材料的验证记录

#### Scenario: Acceptance must change
- **WHEN** 修复只能通过改变原验收条件才成立
- **THEN** 系统暂停自动交付，请用户确认验收变化，并保留原始失败记录

### Requirement: Complete regression evidence
系统 MUST 记录项目回归命令、用例收集数量、退出状态和日志；错误、无有效用例或无法解释的检查范围减少 MUST NOT 当作回归通过。

#### Scenario: No tests collected
- **WHEN** 修改后的检查没有收集到有效测试
- **THEN** 系统将该结果作为验收不足而不是检查成功

#### Scenario: Required regression fails
- **WHEN** 项目约定的回归检查失败
- **THEN** 系统保留失败证据，不进入自动草稿 PR 发布阶段

### Requirement: Honest bounded investigation results
系统 SHALL 在环境阻塞、预算耗尽或无法证明修复时提供调查结果、已有差异和下一步所需信息；MUST 将这些结果与已验证修复区分。

#### Scenario: Task stops with a partial patch
- **WHEN** Agent 已生成部分修改但未完成独立验证
- **THEN** 用户可以查看和导出差异，结果明确标为未验证，且不会自动发布草稿 PR
