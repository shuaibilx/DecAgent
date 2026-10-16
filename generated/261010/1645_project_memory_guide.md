# DecAgent 项目记忆与上下文指南

创建时间：2026-10-10 16:45 北京时间。实施范围为 OpenSpec tasks 9.1–9.7 的独立记忆模块；当前目录为 DecAgent 项目根目录。

## 生命周期和证据

`decagent.memory.MemoryManager(database, evidence_store, decisions)` 使用应用数据库中的候选、版本、判断、事件、审批和交付记录；不使用图 checkpoint 保存长期知识。默认 `DecisionEngine(RuleStrategy())` 保持 conservative：可保存待审候选，不自动激活。

`CandidateInput` 限定 fact、cause、design 类别，必须有项目、来源任务、不可变证据引用和 `Applicability`。适用范围包括稳定冲突 key、精确 tags、已知来源 commit versions 和可选时区明确的 expires_at。秘密、未知敏感性、标记为 speculative 的推测，以及要求忽略审批或读取密钥的指令会被阻止。敏感性标签由模型提供时并不代替内容脱敏和证据检查。

事实必须关联同一任务的独立验证成功记录、非零测试数和匹配的证据哈希；原因还必须关联失败的基线复现。每次使用证据都会检查卷上的长度与 SHA-256。模型声称“已验证”不会创建验证记录。临时日志没有这些来源条件时不能成为事实。

设计候选必须引用经过 `DurableApprovals` 归档的拥有者明确确认。调用 `candidate_subject(candidate)` 获得精确主题，再使用 `confirmation_digest(ApprovalKind.MEMORY_CONFLICT, subject, current_policy_context)` 绑定当前任务、项目、代码、授权和策略版本。只有 authenticated owner 的 confirm 回复有效；action 命令 approve、模型文本和仓库说明都不能代替这一确认。提交使用 `submit(candidate, confirmation_id=..., context=...)`。

候选状态为 task_local、pending_review、pending_merge、active、rejected、revoked、superseded。任务内候选只由 `task_candidates(source_task_id)` 返回来源任务；`retrieve` 从不读取这些未激活候选。未发布的代码知识仍限制在来源任务。指纹绑定内容、项目、来源任务、证据、blob 和适用范围；同一候选的重复整理返回同一记录。

事务使用项目行锁，候选 revision 和操作历史保存在 applicability 元数据中；审核与撤销必须提供 expected_revision。相同冲突 key 的不同内容会保留双方来源并进入 pending_review，不能静默覆盖。

## 合并后激活

受信 GitHub observer 必须先从真实 HTTP 结果核实 PR 已合并，读取实际 merge SHA 和候选来源路径的 blob，再保存对应 `app.deliveries`。接入字段如下：

```python
from decagent.memory import VerifiedMerge

receipt = VerifiedMerge(
    project_id=project_id,
    source_task_id=task_id,
    source_pr=pr_url,
    merged_sha=actual_merge_sha,
    blobs={"src/cache.py": actual_blob_sha},  # 删除或缺失路径使用 None
    evidence=registered_observer_evidence,
)
memories = memory_manager.observe_merge(receipt)
```

此 DTO 只能由受信 observer 构造，不暴露为模型参数或普通客户端 JSON。数据库中的 delivery 必须 status=merged，pr_url 与 merge_sha 匹配，并保存 `merged_content={"blobs": receipt.blobs, "evidence_hash": receipt.evidence.sha256}`。仅有 DTO、测试通过、PR closed 或声称 verified 的布尔值均不足以激活。

squash/rebase 可能使修复 head SHA 与 merge SHA 不同；核实依据是来源文件 blob 和仍可验证的证据。来源匹配后才将适用 versions 绑定到实际观察到的 merge SHA，原来源 versions 保留在 _source_versions；无需预先猜测合并 SHA。缺失、变化的 blob 继续 pending_review，需要重新验证及新的候选，不允许人工确认直接跳过 blob 条件。

自动路径限于无冲突事实和原因，并在合并时再次检查当前 memory_write 校准结果为 retain；校准必须绑定真实模型、问题、策略和标注数据集版本。提交时曾获 retain 不代替合并时的判断。设计候选始终需要拥有者进一步审核；默认 conservative 候选合并后继续待审。

`review_subject(id, "activate")` 和 `review(..., confirmation_id, context, expected_revision)` 允许拥有者确认待审候选；仍需合并、blob、证据与无冲突条件。与分支无关的直接项目设计约束可经明确确认及拥有者审核激活。

重复合并观察返回同一已激活 memory version，不插入重复版本。PR 关闭但未合并、实际内容改变或证据损坏均不会自动成为 active。

## 纠正、撤销和检索

`revoke(id, expected_revision=..., context=...)` 需要受信拥有者上下文，在事务中记录 revoked_at、候选状态与审计事件。新任务和在语义判断后重新检查的检索立即排除撤销项；旧事件和此前检索记录保留。

`correction_subject(old_id, replacement)` 绑定原 revision 和新候选指纹。拥有者确认后调用 `correct(old_id, replacement, confirmation_id=..., context=..., expected_revision=...)`。替换内容必须同项目、同冲突 key、有自己的证据和有效合并依据。激活替代版本、memory_versions.replaces 关系和原候选 superseded 状态在同一事务中完成；有额外冲突会阻止纠正。design 替代还需要独立 candidate_confirmation_id，纠正操作确认不冒充设计主题确认。

`RetrievalRequest(project_id, task_id, current_version, task_summary, query, tags)` 先通过 SQL 限定项目、active 状态、当前精确 commit、未到期和未撤销版本，再使用 PostgreSQL simple 全文检索与精确 tag 过滤。初期中文与代码标识依靠关键词及标签，不使用向量或 embeddings；短中文子串和同义词召回有限。

之后由 memory_read_needed 决定是否读取，memory_relevant 筛选每条相关性；决定输入只包含当前判断必要的摘要与证据引用。决策不可用、未校准或不确定时不扩大范围，结果保留 degraded/reason 与选入、排除记录。拥有者可以明确指定 `owner_selected` 活跃项，并提供经过认证的 owner_provenance；仍不能绕过项目、版本、状态、标签和证据限制。

检索返回 `MemoryRecord.authority="untrusted_data"`。内容不能改变工具权限、授权或审批。即使存储中出现指令注入，检索仍排除并记录异常。后续版本没有新的适用性核实便不会继续自动复用旧 commit 的记忆。

## 提取与上下文费用

`ModelCall(model, max_tokens, reserved_cost, invoke, ..., usage_hook)` 注入实际客户端，返回 `ModelOutput(value, tokens, cost)`。提取调用 `extract(task_id, call)`，仅接受最多 20 个 typed CandidateInput；任务与项目不能被提取结果改变。设计提取使用受信 context 与 fingerprint -> confirmation id 映射，模型只关联既有确认，不能创建拥有者授权。

提取与压缩调用前使用持久化 UsageLedger 预留费用与调用/token 上限，成功记录实际使用量；超时、未知费用或未知使用量保留预留，不按零费用继续。未知价格只在任务已有明确授权及调用/token 限额时允许。Jev 判断客户端仍负责自身预留与重试记账。

`ContextBuilder(...).build(events, max_tokens=..., output_reserve=...)` 从原始追加事件组织上下文，保留当前目标、授权范围、待审批和关键验证记录。工具调用与结果作为完整配对一起选入，缺结果的调用不会伪造输出，结果在调用之前时拒绝；多个工具调用的全部结果也保持同组。

默认 token_counter 使用 UTF-8 字节加消息开销的保守上界，可注入解析到实际模型版本的 tokenizer。输入上限扣除 output_reserve。必须保留的记录装不下时抛 ContextBudgetError，不悄悄丢掉授权或验证。超过 100 条的可选旧事件可以压缩；较大日志只选必要事件或保留证据引用。

SourceSummary 带准确事件 ranges、version、证据引用与 untrusted_data 标记。摘要以带来源的数据消息使用，不能成为新的 system 指令。压缩不修改原事件、授权或 checkpoint；可选模型 compressor 必须绑定 database/task_id 和预算，费用即使最终摘要超长无法使用也已记录。

## 离线验证及范围

从 DecAgent 项目根目录运行：

```powershell
.venv/Scripts/python.exe -m pytest evals/deterministic/test_memory_contracts.py -q
$env:DECAGENT_TEST_DATABASE_URL = "postgresql://USER:PASSWORD@127.0.0.1:PORT/test_admin"
.venv/Scripts/python.exe -m pytest evals/integration/test_memory_lifecycle.py -q
.venv/Scripts/python.exe -m ruff check decagent/memory evals/deterministic/test_memory_contracts.py evals/integration/test_memory_lifecycle.py
```

PostgreSQL 用户需要在测试专用服务上创建数据库的权限。每个集成用例创建 memory_UUID 新库并保留记录；不会 DROP、reset 或清空指定根库及现有数据。没有环境变量会显示 skipped，不等价于集成通过。

覆盖候选校验、明确设计确认、幂等与冲突、默认及校准激活、squash/缺路径/未合并、即时撤销、纠正版本关系、项目/版本/到期过滤、读门控/相关性/注入/故障、提取预算与压缩审计/配对/checkpoint 隔离。

两个任务的数据库用例首先证明未合并不可跨任务读取，再用受信 observer fixture 记录实际内容核实后的合并，证明第二任务可检索。这里是真实 PostgreSQL 和离线 observer/模型 fixture；不声称已经完成真实 GitHub 合并、真实 Jev 校准或付费模型接入。API、worker 轮询和受信认证边界由上层集成接入本模块；真实服务验收仍需各自的凭据与测试仓库。
