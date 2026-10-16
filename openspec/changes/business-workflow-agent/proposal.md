# Proposal

## Why

企业已有 CLI 与 Skill 能暴露业务能力，但通用模型往往反复解释同一流程，且判断、参数选择、授权与执行结果混在一起。DecAgent 将业务插件承接为独立 Agent 能力，用程序规则与 Jev 加速高频语义判断，并用可恢复的执行链验证结果，展示能够落地、能够评测的 Agent 工程能力。

## What Changes

- 将默认工作台定位改为业务流程智能体；Python 仓库修复保留为非默认路径，原 Waku 不变。
- 支持基础插件接入和流程增强接入；统一版本化命令、JSON Schema 参数、操作影响、结果格式及业务流程契约。
- 常见流程优先使用规则与 Jev，复杂任务交给主模型规划；两条路径共享参数、权限、确认及结果校验。
- Choice 选择分支与真实实体，Noul 判断单一命题，Score 评估工作受阻程度；评分尺子与业务选项由契约定义，主模型不能自行扩大能力。
- 提供本地企业服务工单业务、JSON CLI 与 Skill，覆盖创建、首次分派、处理、解决、关闭、重开和评论。
- 业务任务不要求 GitHub 仓库、提交 SHA 或修复执行器；新增任务类型与作用域，保留既有任务恢复能力。
- 提供绑定资源版本的确认、幂等写入、回执查询与中断恢复，以及公平的合成数据对照评测。
- 同步使用指南、架构说明及可视化图册，生成说明归入独立 generated 目录。

## Capabilities

### New Capabilities

- `business-plugin-contracts`: 可信插件的双级接入、能力发现、版本冻结及统一参数契约。
- `business-task-lifecycle`: 独立业务任务、持久化进度、预算、取消与恢复。
- `business-decision-routing`: 三种类型化判断、可追溯选项来源与受限主模型降级。
- `business-action-governance`: 业务动作的授权、确认、新鲜度、幂等与结果核实。
- `enterprise-service-tickets`: 本地企业服务工单的完整生命周期、目录与执行回执。
- `business-workbench`: 业务任务、判断、确认和业务结果的单用户交互。
- `business-evaluation`: 同等信息与执行约束下的可复现正确性和延迟评测。

### Modified Capabilities

无。正式规格库为空，既有规格仍在历史变更中；本变更新增业务能力，不改写历史规格或验收证据。

## Impact

规划归当前 OpenSpec 根管理，目标代码仍为同级 `../DecAgent`。复用 Python、FastAPI、PostgreSQL、LangGraph、单用户认证和预算设施，新增插件与业务模块及数据库迁移，不改写旧迁移。已有修复 API 保持兼容，默认首页与产品文档转向业务场景。

首版本使用可信已安装 CLI 和本地合成业务数据，不接入原公司后端，不支持多用户 SSO、自动安装第三方插件、实际修复企业系统或自动沉淀业务内容为长期记忆。真实 Jev 评测遵守既有累计 1 美元上限；独立 Linux 部署仍列为未验收项。
