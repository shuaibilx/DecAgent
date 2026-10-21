# DecAgent 项目开发技能

本目录是开发助手的技能库，不属于业务 Skill、MCP 工具或 Jev 判断模板，不参与应用运行。安装不会改变应用依赖、模型配置、预算或部署状态。

## 已有官方工程技能

| 技能 | 官方来源 | 对应后续任务 |
| --- | --- | --- |
| [supabase-postgres-best-practices](supabase-postgres-best-practices/SKILL.md) | [Supabase](https://github.com/supabase/agent-skills) | 游客 3.1–3.4 的 PostgreSQL 配额、队列、锁、迁移与索引 |
| [webapp-testing](webapp-testing/SKILL.md) | [Anthropic](https://github.com/anthropics/skills) | 游客 2.5、2.6、6.1 的真实浏览器流程与隔离服务生命周期 |
| [security-best-practices](security-best-practices/SKILL.md) | [OpenAI](https://github.com/openai/skills) | Python/FastAPI、原生 JavaScript、会话与跨空间授权的安全实现 |
| [security-threat-model](security-threat-model/SKILL.md) | [OpenAI](https://github.com/openai/skills) | 公网开放前审视游客、模型密钥、MCP 与私有 Runner 的信任边界 |
| [docker-compose-patterns](docker-compose-patterns/SKILL.md) | [Docker](https://github.com/docker/skills) | 主任务 11.1 的隔离容器夹具、游客 4.1–4.4 的 SOP Compose 与业务健康探针 |

此前五个技能共 70 个文件保留，本轮未覆盖或升级。

## 本轮补充的官方技能

| 技能 | 官方来源 | 当前用途 |
| --- | --- | --- |
| [ecosystem-primer](ecosystem-primer/SKILL.md) | LangChain | 修改框架与模型组合前明确现有分工 |
| [langgraph-fundamentals](langgraph-fundamentals/SKILL.md) | LangChain | 状态、条件路由、节点与合流 |
| [langgraph-human-in-the-loop](langgraph-human-in-the-loop/SKILL.md) | LangChain | 人工中断、回复与恢复 |
| [langgraph-persistence](langgraph-persistence/SKILL.md) | LangChain | 检查点、线程与跨任务持久状态 |
| [langgraph-decision-models](langgraph-decision-models/SKILL.md) | LangChain | Jev Noul/Choice/Score、路由和校准审阅 |
| [eval-engineering](eval-engineering/SKILL.md) | LangChain | 隔离环境、Verifier、防泄漏与失败分类 |
| [gh-fix-ci](gh-fix-ci/SKILL.md) | OpenAI | GitHub Actions 失败 PR checks 与日志定位 |
| [mcp-builder](mcp-builder/SKILL.md) | Anthropic | Python MCP 工具契约、错误与辅助测试 |
| [cloudflare-one](cloudflare-one/SKILL.md) | Cloudflare | 后续 Cloudflare Tunnel/Zero Trust 网络设计与验证；不会自动替所有公开访客启用 Access |

其中两项补充 Jev/评测方法，五项将当前会话已有框架/MCP 参考固定到项目内，另补一项 CI 专用诊断。不会重复安装此前已有的项目技能。

截至本次复核，项目内有 15 项官方开发参考（136 个文件）、2 项 GitHub 组织仓库收录的社区贡献工作流参考（11 个文件）、6 项 OpenSpec 工作流及 1 项 TypeSafe 补充参考；合计 24 个技能目录、155 个技能文件。其中 17 项 GitHub 来源的官方/社区参考（147 个文件）记录于 [skill-sources.json](../skill-sources.json)，包含固定提交、逐文件 SHA-256 与许可证；本地生成的 OpenSpec 工作流和 TypeSafe 副本不在该上游来源清单中，来源边界见下文。更新 GitHub 来源技能时必须审阅并固定提交，不跟随浮动 main 自动升级。

## 本次新增的工作流与安全参考

| 技能 | 来源说明 | 对应后续工作与边界 |
| --- | --- | --- |
| [security-audit](security-audit/SKILL.md) | [Cloudflare 官方源码](https://github.com/cloudflare/security-audit-skill) | 对外开放前的防御性代码审计；仅当明确要求审计时运行完整流程。其严格 sandbox 条件必须遵守；它不是 Cloudflare Tunnel 配置指南，也不代表已经完成审计。 |
| [github-actions-efficiency](github-actions-efficiency/SKILL.md) | [GitHub 组织维护的 Awesome Copilot 仓库](https://github.com/github/awesome-copilot)，仓库内容由社区贡献 | CI 变慢时按证据检查缓存、并行、触发条件和竞争取消；作为实践参考，不减少必需测试、不替代 GitHub 文档。 |
| [github-actions-hardening](github-actions-hardening/SKILL.md) | 同上，社区贡献内容 | 审阅公共 CI 与私有 CD 的权限、触发器、第三方 Action 固定、secret 边界和自托管 Runner；作为检查清单，不作为规范来源。 |

### TypeSafe / Jev 补充参考

项目还包含 [typesafe-ai](typesafe-ai/SKILL.md)，它从用户现有的全局技能复制到项目，便于后续 Jev 开发使用。未找到可核验的上游技能仓库，因此不把它计入上述 14 项官方技能，也不声称该技能文件由 TypeSafe 官方发布；其 MIT 标记是该本地技能自身的声明，未纳入 GitHub 上游来源清单。该指南要求以 [TypeSafe 官方文档索引](https://docs.typesafe.ai/llms.txt)和当前 API/SDK 文档为准；项目仍使用已实现的 OpenRouter Decisions 适配，不因这份参考自动切换到 TypeSafe SDK 或直连 API。

补充说明：GitHub awesome-copilot 属 GitHub 组织维护的公开仓库，但其内容自述为社区贡献。本项目安装了两项高采用率 workflow 参考并明确标作社区实践，不称为 GitHub 官方规范；Actions 行为、Runner 安全与权限结论仍以 [GitHub 官方安全文档](https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions)和实际 workflow 验证为准。Cloudflare 的官方安全审计 skill 不覆盖 Tunnel 配置；检索到的专用 Tunnel skills 均为非官方社区作品，因此没有安装，Tunnel 后续按 [Cloudflare 官方连接器文档](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)实施。在已检查的 OpenAI、Anthropic、LangChain 官方技能目录中，也未找到专门覆盖 Python 进程树取消/监督、SQLite 持久预算并发或 τ-Bench/EnterpriseOps-Gym 官方适配的技能；这几项继续依据 Python/SQLite 官方文档、上游 benchmark 原始代码与项目的隔离故障测试验收，不用泛化社区技能代替。

完整任务映射、来源认可度与适配限制见 [官方技能与任务对照](../../generated/261021/开发技能/1502_官方技能与任务对照.md)。没有取得稳定安装量的细分技能不宣称已广泛验证。

## 现有 OpenSpec 工作流

本目录同时安装了已在本会话使用的六个 OpenSpec 官方生成技能：explore、propose、apply-change、update-change、sync-specs、archive-change。它们来自现有 OpenSpec 1.14.1 生成内容，复制后已校验文件摘要，后续可以直接在 DecAgent 项目中使用，不依赖旧 Waku 目录。此操作没有初始化或修改 OpenSpec 规格。

## 使用边界

- 从 DecAgent 根目录打开开发任务，以便发现本目录技能；若会话仍以旧 Waku 为工作目录，可按本索引中的路径读取。
- TypeSafe / Jev 改动先读本地 `typesafe-ai` 指南并核对当前官方文档；该指南不是已核验的 TypeSafe 上游官方技能，不能替代官方 API 文档。本项目继续使用现有 LangGraph、MCP、CI/CD、测试驱动开发和文档工作流，不重复引入实现框架。
- PostgreSQL 技能不意味着迁移到 Supabase，也不能作为公开评测 SQLite 账本正确性的证明。SQLite 仍依据其官方事务文档与项目真实并发/崩溃测试验收。
- webapp-testing 的服务辅助脚本只用于受控本机测试。其通用终止逻辑不替代 DecAgent 的进程树取消与监督排他；有 EventSource 的页面应等待具体界面就绪，避免盲用 networkidle。
- Docker 通用示例不能覆盖本项目既有网络隔离、镜像摘要、回执与数据库备份要求。不能仅为套用示例就重命名运行配置、删卷或改变部署网络。
- 安全技能只在相应开发或明确的安全评审范围内启用；安装不等于已经完成安全审计。
- 技能不提供 EnterpriseOps/τ-Bench 的实验资格。公开评测继续采用固定上游源码、官方评分器、同状态对照及精确分母；不为使用某个技能引入云端评测服务或上传私有轨迹。
- langgraph-decision-models 的 alpha SDK、直连/Gateway 和样例阈值不是本项目默认值；保留现有 OpenRouter 适配及 95%/100 样本验收条件，不用示例性能数字证明收益。
- eval-engineering 按需参考环境、评分和失败审计；不自动引入 Harbor、World Skill、新数据集或另一套 Task.md，不能替换 OpenSpec 与官方 benchmark。
- mcp-builder 使用 Python 参考，不因其 TypeScript 推荐或通用只读问答样例改变当前协议、依赖锁或公开评测范围。gh-fix-ci 不代替流水线性能分析或私有 CD 设计。

## 本次安装检查

本轮新增八项共 45 个文件与上游固定提交逐个匹配，既有 76 个文件摘要仍一致；八个新增 Python 文件通过 AST 语法检查，64 个新增技能内相对链接可解析。许可证保留；未运行评测模型、Harbor、SQLite 数据导出或 MCP 评测脚本。既有 webapp-testing 的 with_server.py --help 曾验证，未在本轮启动服务。
