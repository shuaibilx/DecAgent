# Python 修复与独立验收

生成时间：2026-10-10 16:52（北京时间，来自 UTC 时钟 08:52:43）。实施位置为独立 DecAgent；原 Waku 与当前 OpenSpec 规划目录保持只读。

## 使用范围

支持 Python 3.11 固定 runner 中的普通 Python 缺陷、pytest 检查、根目录 `requirements*.txt`、PEP 621 `pyproject.toml` 和 `uv.lock`。识别 `.python-version`、`requires-python`、uv 锁中的 Python 要求，以及 pyproject/pytest.ini/setup.cfg/tox.ini 的 `testpaths`。所有推断命令保留来源；setup 执行经同一动作治理、代码信任和 task lease。

`uv.lock` 使用镜像中固定 uv 版本与 `uv sync --frozen`；宿主机无需 uv。pip 使用任务卷中的 `.venv`，不会向只读镜像或宿主安装仓库依赖。准备命令上限 600 秒，普通检查 120 秒。

配置冲突、无法解析的配置、Python 版本不匹配、私有依赖凭据、GPU/交互式服务要求会明确阻塞。只允许不改变选择范围的 `-q/-v/--disable-warnings` 配置选项；`-k`、自定义测试收集规则、插件和项目 conftest 所需 fixture 需要明确调整受控验收，不能静默忽略后宣称成功。

当前固定材料通过有界 broker 参数传输：一个 base64 参数最多 16,000 个字符；完整冻结测试集超过此限制时阻塞。该版本未提供大仓库测试材料卷上传能力。已有业务测试依赖自定义 conftest 时，受控执行会产生环境错误，需要独立 checker 或认证验收调整。

## 后端与流程接口

`decagent.tools.repair` 提供 `RepairBackend`、`BrokerRepairBackend`、`HttpBrokerClient`、`ProjectPlan`、`Command`、`ValidationCommand`、`BundleStore`、`ReproductionBundle`、`RepairSession`、`VerificationRecord` 和 `DatabaseVerificationSink`。

后端能力为 `snapshot/list_files/read/write/diff/execute`。生产后端仅调用认证 broker；没有宿主 subprocess、原始 Docker 参数或任意宿主路径回退。snapshot 是 worker 私有材料，不能直接作为模型工具结果或 UI payload。

```python
client = HttpBrokerClient(internal_broker_url, service_token)
backend = BrokerRepairBackend(client, scope_request, authorize)
store = BundleStore(evidence_store, workspace_root=workspace_metadata_root)
sink = DatabaseVerificationSink(
    database, store, owner=scope_request.owner,
    lease_generation=scope_request.lease_generation,
)
session = RepairSession(
    task_id, backend, store, runner_digest=pinned_runner_digest,
    record_sink=sink,
)
plan = infer_project(backend.snapshot(), task_description=issue)
session.prepare(plan)
bundle_ref = session.reproduce(
    base_sha=exact_base_sha, issue=issue,
    related_cases=("test_bug.py::test_bug",),
    reproduction_targets=("test_bug.py",), plan=plan,
)
# 经授权的 Agent 现在可通过同一 backend 修改代码或测试。
verification = session.verify(bundle_ref, repair_sha=repair_sha)
result = session.export_result(verification)
```

`authorize(Action, Command | None) -> GovernedAction(action_id, digest)` 必须由可信控制服务提供。它应运行预算和治理、加载认证审批、执行前重查并持久化动作；`Command.kind == 'setup'` 的 broker 动作 snapshot 另记录 `execution_kind='setup'`。Command 的来源不能来自模型伪造授权。

固定验证命令为 `python -P -m decagent_check <payload>`，模块源文件是 `decagent/tools/repair/check_runner.py`，镜像将它复制到 `/opt/runner/decagent_check.py`。`-P` 移除可写工作区对模块名称的遮蔽。只有控制服务构造的 `ValidationCommand` 可以调用治理入口 `govern(..., verification_intent=command)`；模型直接选择该模块会被硬拒绝。`validate_validation_intent` 核对完整 argv、严格 payload、材料和目标的哈希、代码来源、收集模式、cwd 和超时；它不会免除代码信任、边界检查或所有者审批。

## 固定材料和验收门槛

Bundle 保存在可写仓库以外的 EvidenceStore，记录 base SHA、基线树、runner digest、依赖声明哈希与已准备依赖快照、命令、固定源文件、相关失败用例、完整原回归清单和日志引用。每次读取都验证证据哈希和长度。敏感固定材料若会被脱敏改变，拒绝冻结，不能使用改变后的内容作为原始检查。

基线检查只有实际失败的相关用例才能建立缺陷证据。收集/导入/fixture/setup 错误、超时、输出截断、没有有效用例或未复现不算修复起点。相关用例由可信任务验收确定；自动生成的断言并不替代正确业务规格。

独立检查在容器 `/tmp` 中重新创建固定材料，使用受控 pytest 配置、禁用仓库 conftest 与插件自动加载。pytest 先从可信镜像导入，再加入目标源码路径；固定 helper 优先于可修改测试目录。修复验收运行原固定目标，回归运行原冻结测试源和原命令选择范围。测试新增、删除、skip/xfail、断言改写、用例减少及测试配置改变会单独标注。

通过记录要求相关基线失败、同一固定材料独立通过、原回归有效通过、无 skip/xfail/错误、同一收集清单以及验证期间文件树不变。`publishable(record)` 再次比较当前树哈希。编辑之后旧通过记录失效；失败或部分修复仍可用 `export_result` 导出完整差异，状态为 `unverified`。

过时验收使用 `acceptance_digest` 创建具体提案，随后 `change_acceptance` 仅接受可信记录仓库加载的 `ApprovalKind.ACCEPTANCE`、认证所有者、精确摘要和 confirmed 状态。替换回归材料/命令也必须纳入同一摘要。新条件在只读 `/baseline` 源码上重新检查，旧 Bundle 保留为 `previous_bundle`。不要把模型 DTO、仓库文本或日志反序列化为可信 Approval。

`DatabaseVerificationSink` 在任务 lease 下记录 baseline/independent/regression 三行并注册完整证据。环境字段包括相关失败标记、实际 Python/pytest、runner digest、依赖快照、收集清单/哈希、skip/xfail/error 数量、退出码、执行编号、bundle hash、repair SHA、验收审批和失败原因。source tree 哈希复用 `decagent.executor.archive.snapshot_hash`，即确定性归档字节的 SHA256。

## 验证证据与运行方法

2026-10-10 已验证宿主 Python 3.13：repair 39 个确定性测试，加 trusted verification governance 5 个测试，总计 44 passed；本领域 Ruff E/F/I/UP/B、行宽 100 通过。

Windows Docker Desktop Linux/WSL2 上，镜像 `sha256:97d1759e83bdfd2febbeb095dec21f778118004f02658bdf8c53d4702d94d81c`（Python 3.11）完成两个容器集成测试，`2 passed`（47.33 秒）。首次容器测试宿主未复制 pyproject，出现 integration marker 注册警告；退出状态和两个测试断言均通过。

1. 自编语义 fixture 将“上界不包含自身”错误实现为包含自身；准备 venv/pip 后基线断言失败，修复源码后同一独立检查与两个原回归用例通过，真实 PostgreSQL 保存三条对应记录。再次编辑后旧记录不能交付。
2. 改写仓库测试为无条件通过，并加入恶意 conftest、pytest.ini、pytest.py 和 decagent_check.py 同名文件；固定独立检查和原回归仍发现原缺陷，不能伪造通过。

Fixture 是项目自编、MIT 许可的确定性语义示例，base/repair SHA 是测试身份；这不是实际外部 GitHub 仓库或付费模型验证。生产源码、后端、证据与治理路径与 fixture 测试共用实现。

```powershell
# 在 DecAgent 根目录，运行离线领域验收
.\.venv\Scripts\python.exe -m pytest evals/deterministic/test_repair_inference.py evals/deterministic/test_repair_checker.py evals/deterministic/test_repair_session.py evals/deterministic/test_repair_backend.py evals/deterministic/test_validation_governance.py -q
.\.venv\Scripts\python.exe -m ruff check decagent/tools/repair evals/deterministic/test_repair*.py evals/integration/test_repair*.py
```

容器集成入口为 `evals/integration/test_repair_docker.py`。在一次性 Linux 测试宿主中设置 `DECAGENT_REPAIR_INTEGRATION=1`、固定 `DECAGENT_RUNNER_IMAGE`、`DECAGENT_PROXY_CONTAINER`、`DECAGENT_REPAIR_TESTHOST` 和一次性 `DECAGENT_TEST_DATABASE_URL`；当前用例明确只允许 executor-test-only 数据库。每个用例新建自己的 task UUID；清理仅涉及该任务的容器、网络和卷，不执行 prune，不删除用户数据卷。

Durable graph 的阶段调度、审批暂停/恢复和实际交付服务由根集成负责接线；本领域不会使用模型最终文本作为修复成功标记。未运行的真实模型、真实外部仓库和其他平台验证需要单独报告。
