# SOP 回滚协调器实现记录（2026-10-22）

## 范围

本记录覆盖 `ops/deploy/sop_rollout.py` 的确定性协调逻辑与状态记录。它不表示 4.3 的真实 Docker 升级/回滚验收已完成。

已实现的保护边界：

- 接受固定 `ghcr.io/shuaibilx/decagent-control@sha256:<64位摘要>`，拒绝 tag 和其他仓库引用。
- 部署前检查 SOP 业务 readiness，记录当前镜像、Compose 配置指纹、数据库迁移版本和 graph 版本。
- 写入独占发布记录后，先停止 API、Worker 和业务 MCP，再生成 PostgreSQL dump、`/data` 快照和脱敏 Compose 配置，避免备份期间应用并发写入。
- 备份或停写准备失败时，记录失败类别并尝试恢复原镜像；恢复失败会尝试停服并保留失败状态。
- 候选镜像未通过 readiness 时仅尝试恢复旧应用镜像；旧版本遇 schema/checksum/graph 不兼容时停止 API、Worker、业务 MCP，不自动覆盖数据库。
- 操作者在备份准备或候选切换期间按 Ctrl+C 时，协调器记录中断并尝试恢复旧版本；恢复不成功则尝试停服。
- 发布锁拒绝并发部署。发布记录仅保留白名单字段，避免写入运行时凭据。
- 备份文件包含数据库与 `/data` 的业务内容。当前依赖 Windows 用户 ACL/Linux 文件权限保护，未实现应用层加密；在主机磁盘加密和保留策略确认前，不应把该备份目录作为真实敏感数据的生产备份。

## 验证

- `uv run --extra dev pytest evals/deterministic/test_sop_rollout.py evals/deterministic/test_sop_compose_preflight.py evals/deterministic/test_sop_business_probe.py evals/deterministic/test_sop_smoke_diagnostics.py -q`：33 passed。
- `uv run --extra dev pytest evals/deterministic -q`：2,075 passed、18 skipped，用时 232.50 秒。
- Ruff lint、格式检查、`git diff --check`：通过。
- `openspec validate --all --strict`：6 个 change 全部通过。
- `compose.sop.yaml` 只读解析与配置预检：通过。

## 尚未执行的真实容器验收

本机 Docker daemon 可用，但不能用当前公开发布版本证明这项真实部署路径：公开 Git tag 最新为 `v0.1.4`（commit `089cd02715a5b0ae4934db953f412086988b09f4`，2026-10-17），该 tag 不包含 `compose.sop.yaml` 或 `ops/deploy/sop_rollout.py`。GHCR package versions API 因当前 GitHub 凭证没有 `read:packages` scope 返回 403；本轮没有拉取或启动任何应用容器，也没有触碰现有数据库、卷或服务。

因此，以下仍待对同一个 reviewed SHA 发布出真实 SOP control-image digest 后，在独立 Compose project/数据卷完成：升级成功、readiness 失败后的兼容应用回滚、schema 不兼容停服且数据库未被覆盖。4.3 继续保持未勾选；当前测试没有调用 Qwen、Jev 或其他外部模型。
