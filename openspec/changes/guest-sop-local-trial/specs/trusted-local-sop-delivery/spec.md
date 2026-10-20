# Spec Delta

## Purpose

把经过验证的 SOP Agent 发布为可恢复的 Windows 本机试运行服务，隔离公共代码检查与具备本机 Docker 权限的部署任务，并以真实业务健康、备份和兼容回滚提供可追溯交付证据。

## ADDED Requirements

### Requirement: SOP-specific local service topology

本机交付 MUST 运行 API、业务 Worker、业务 MCP 与 PostgreSQL，使用正确条件计划配置和内部身份。业务端口 MUST 默认仅本机访问；数据库、MCP、管理接口及 Docker MUST NOT 向游客开放。仅旧 repair 容器健康不能证明 SOP 已部署。

#### Scenario: Healthy API but unavailable business worker
- **WHEN** API 存活而业务 Worker 或 MCP 不可用
- **THEN** 部署验证失败，不宣称服务整体健康

### Requirement: Public CI and private trusted deployment

公开仓库 MUST 执行检查、测试和发布，独立私有仓库 MUST 控制本机部署 Runner。部署 MUST 独立验证发布来源、main 提交、成功 CI、版本标签和不可变镜像 digest；公共 PR 及任意 dispatch 参数 MUST NOT 执行本机代码。

#### Scenario: Untrusted deployment request
- **WHEN** 请求引用失败 CI、非 main 发布、任意镜像或脚本
- **THEN** 私有部署在本机执行前拒绝，不拉取未验证镜像或运行调用方脚本

### Requirement: Backup and compatibility-aware application rollback

部署 MUST 保存旧镜像/配置与数据库备份，串行更新并验证业务核心链路。健康失败时仅在当前数据库与旧版本兼容的条件下自动回滚应用；不兼容时 MUST 停止并保留恢复材料，不自动覆盖数据库或隐藏失败。

#### Scenario: Failed application upgrade with compatible schema
- **WHEN** 新镜像无法通过业务健康检查且旧版本可读取当前 schema
- **THEN** 系统恢复旧 digest 与配置、重新验证健康，并将本次发布记录为失败且已回滚

#### Scenario: Incompatible schema after migration
- **WHEN** 旧版本无法安全读取升级后的数据库
- **THEN** 系统停止并给出人工恢复步骤，不盲目启动旧应用或自动恢复覆盖数据库

### Requirement: Verified CI coverage and startup recovery

CI MUST 覆盖 Python 3.11/3.13、代码检查、确定性、真实 PostgreSQL 集成、核心浏览器 E2E、发行包 MCP 与 Compose。缓存和并行优化 MUST 不移除关键覆盖；Runner/Docker 登录启动及重启恢复 MUST 有实测证据，不能将缺环境 skip 记为通过。

#### Scenario: Cached successful dependencies
- **WHEN** 后续运行命中依赖或镜像缓存
- **THEN** 仍执行对应变更必需的测试，记录各 job 和总墙钟时间以验证优化

### Requirement: Separate operational guides and deferred ingress

交付 MUST 按代码仓库、CI、CD、本机部署、游客使用、评测分别组织指南和图示。域名与公网开通 MUST 标为后置；本机短时中断可接受但恢复方式须说明，Windows/Docker 通过不能替代独立 Linux 验收。

#### Scenario: Local-only acceptance
- **WHEN** 本机服务和发布链路通过但没有域名或公网入口
- **THEN** 文档明确只通过本机验收，不提供虚构公网地址或声称已对外开放
