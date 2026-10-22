# CI 关键路径与分片验证

日期：2026-10-22

范围：公共 GitHub Actions CI；不包含发布或自托管部署。

## 基线

检查 GitHub Actions run [39691794406](https://github.com/shuaibilx/DecAgent/actions/runs/39691794406) 的服务端 job 时间：该次全绿运行从 23:11:42Z 到 23:26:36Z，墙钟 14 分 54 秒。Python 3.11 job 用时 14 分 50 秒，其中 PostgreSQL 集成步骤 11 分 24 秒、确定性测试 2 分 51 秒；Python 3.13 job 用时 13 分 11 秒，其中集成步骤 10 分 19 秒、确定性测试 2 分 20 秒。构建/镜像 job 用时约 2 分 30 秒，Docker executor 边界 job 约 2 分 05 秒，Workbench 检查约 6 秒。job 已并行运行，不能把总耗时归因于串行。

历史运行显示波动：近几次 CI 墙钟约 18 分 48 秒至 27 分钟。因此问题不是每次都超过半小时；主要关键路径仍是两套 Python 矩阵中的数据库集成步骤。当前 setup-python pip 缓存、依赖锁文件绑定、xdist 四 worker 和独立 job 已存在，不能把它们写成尚未做过的优化。

## 本轮变更

- 将 Ruff 与完整确定性套件放在 Python 3.11/3.13 的 `checks` job。
- 单独建立 Python 3.11/3.13 × 2 个稳定测试分片，共 4 个并行 PostgreSQL 集成 job；每个 job 有自己的 PostgreSQL service，继续使用每个分片内 `pytest-xdist -n 4 --dist=worksteal`。
- 新增 `ops.testing.pytest_ci_shard`，以 pytest node id 的 SHA-256 前 64 位稳定映射分片；没有传分片选项时不改变本地 pytest 行为。配置不完整、越界或空分片会失败。
- 新增确定性测试，检查不同分片数下映射稳定、覆盖每个分片且无重复/遗漏，并拒绝无效输入。

本地验证：确定性套件 `2023 passed, 18 skipped`（4 分 30 秒）；Ruff 通过；OpenSpec 严格校验 6/6；分片收集将当前 707 个 integration node ids 完整划分为 329 和 378，且各分片均成功启动 xdist。Windows 原始中文工作路径会触发 execnet 的 surrogate 编码错误；使用临时 ASCII `subst` 盘符后收集与测试可运行。

两片并行的 PostgreSQL 执行首轮中，shard 0 为 `316 passed, 12 skipped, 1 failed`（482.72 秒），shard 1 为 `363 passed, 15 skipped`（560.99 秒）。唯一失败是 `test_authenticated_employee_workspace_is_database_bound_and_request_cannot_override`：测试使用 guest workspace，却沿用默认关闭的试用设置，因此收到预期的 404。修正 fixture，使该测试显式开启试用并提供测试 HMAC key 后，整个 `test_business_employee_auth.py` 文件 `8 passed`（49.71 秒），然后修复后的完整 shard 0 重跑为 `317 passed, 12 skipped`（373.15 秒）。shard 1 为 `363 passed, 15 skipped`；fixture 修改只影响显式传入 `True` 的一个测试，默认 false 分支保持原状。当前全部 707 个 integration node ids 均有实测覆盖，合并结果为 `680 passed, 27 skipped, 0 failed`。这属于本机 Python 3.13/PostgreSQL 验证，不能替代 GitHub 3.11/3.13 矩阵。

本机 integration 的 27 个 skip 原因：17 个 executor Docker 用例缺少显式 disposable Docker/SQL fixture；4 个 repair Docker 用例需要 `DECAGENT_REPAIR_INTEGRATION=1`；5 个企业环境测试需要 opt-in pinned Docker 环境；1 个 benchmark container 测试缺少固定镜像。确定性套件 18 个 skip 包括未配置的 pinned τ 子进程/工具运行时，以及 Windows 符号链接权限限制。CI 的另一条 Docker executor job 会独立验证容器/SQL 边界；上述 integration skips 仍保留并单列，不计作通过。

CI 的 `workbench-ui` job 当前运行 Node.js 语法和浏览器逻辑回归脚本，不启动真实浏览器；Python `TestClient` 集成测试也不是浏览器端到端自动化。虽然项目有本机 Playwright 验收记录，但 workflow 尚未安装/运行 Playwright。因此“核心真实浏览器 E2E 已纳入 CI”仍是缺口，OpenSpec 5.1 不得勾选，后续应增加隔离浏览器 job 并明确其运行与 skip 口径。

测试后检查既有 `decagent-conditional-postgres` 专用容器，发现 521 个历史 `case_`/`template_` 数据库；其中最新数据库目录修改时间为 2026-10-20 06:24:54Z，早于本次运行。未删除这些容器或数据库；本次临时 ASCII 盘符映射已移除。

## 解释与验收边界

## 2026-10-22 当前工作区复验

最新完整确定性套件为 `2024 passed, 18 skipped, 0 failed`（277.80 秒）。首轮全量运行曾有 1 项 deadline-sensitive 本地 loopback 测试失败：其 3 秒限时覆盖了 spawn 启动与响应解析，返回的是超时而非该测试所验证的 malformed-response 分类。该用例单跑和整文件均通过；将测试专用 timeout 调整为 10 秒后，cancellable-model 文件 `24 passed`，全量确定性套件再次通过。该调整只放宽本地 fixture 的 deadline，不改变生产 timeout 或取消语义。18 个 skip 原因仍为 pinned τ runtime 未启用、可选 τ 工具未安装及 Windows symlink 权限限制。

当前 709 个 integration node IDs 按 CI 的稳定规则完整拆成两片，在 Windows/Python 3.13 和同一隔离 PostgreSQL 容器上顺序执行：shard 0 `318 passed, 12 skipped`（398.98 秒）；shard 1 `364 passed, 15 skipped`（485.57 秒）。合并为 `682 passed, 27 skipped, 0 failed`；该序列墙钟约 14 分 45 秒，不是两片并行时间，也不代表 GitHub runner 的提速结果。27 个 skip 仍是显式 opt-in 的 Docker/repair/EnterpriseOps 环境测试，按 unverified 处理。Ruff、OpenSpec strict（6/6）和 `git diff --check` 均通过。仅验证了 Python 3.13；没有同 SHA GitHub CI，当前工作树尚未形成可派发 live 的 clean candidate。

分片旨在降低 PostgreSQL 集成测试的关键路径，代价是 runner 并发与可能更高的 runner-minutes。两片本地耗时约 8:03 和 9:21（并行运行）；这不能直接外推为 GitHub 托管 runner 墙钟改善。提交到受审分支后，需用同一工作流、同一 commit 的全绿 run 对比墙钟、integration job 最大时长、runner-minutes、失败数及 skip 数。若总墙钟没有实质改善或分片导致资源争用，应按该 run 复盘调整分片数/worker 数，而不是删覆盖或接受更多 skip。`actionlint` 当前未安装；最终 GitHub Actions 调度结果仍是 workflow 语法和运行验证的一部分。
