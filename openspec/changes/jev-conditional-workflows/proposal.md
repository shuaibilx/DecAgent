# Proposal

## Why

当前 DecAgent 在普通工具队列耗尽后继续调用主模型，尚不能一次表达后续 Jev 判断、条件分支和未来参数依赖，限制了 Jev 低成本、低延迟的价值。需要把业务执行改为可验证、可恢复的条件计划，并在完整的模拟业务工作流中公平比较多种执行路线，检验 Jev 的实际收益和适用边界；不预设 Jev 必然优于代码规则或主模型。

## What Changes

- 将已注册 MCP/Skill 业务编排为有类型、版本和界限的条件计划；LLM 引用可信判断模板，代码绑定真实参数并执行，已知分支不追加主模型。
- 采用可替换规划器；普通任务以 1–3 次实际通用模型请求为目标，异常局部重规划另设上限。区分主模型、Jev 与执行预算，所有重试和辅助请求均记账。
- 增加 Choice/Noul/Score 分类型确定性策略和持久人工待办；低分直接由人选择、纠正或补充，达到阈值仍须满足权限及独立的写入确认。
- 让任务工作记忆、Jev 决策和真实回执进入受限的后续上下文；跨任务记忆只通过已发布工作流显式声明的稳定槽读取，以记忆类型与 subject/key 绑定，每次按当前员工身份解析有效记录，不自动注入通用规划上下文。
- 发布可验证的复用计划，适用任务可零通用 LLM 执行，仍重新读取业务事实与权限。
- 在工作台展示计划路径、阈值、实际模型请求、等待原因和 trace；同步指南、架构、泳道、流程、时序与恢复图。
- 以 EnterpriseOps-Gym 的 CSM/ITSM 任务作为主要端到端工作流评测，以 τ-Bench 作为对话、澄清和用户模拟的补充评测。意图分类数据不得替代完整业务流程的成功率证据。
- 对照 ReAct + Skill + MCP、一次规划 + 确定性执行器 + MCP、一次规划 + 确定性执行器 + Jev 运行时语义判断。结构化条件由代码处理；Jev 只评估运行时语义分支，并与同一状态和候选上的主模型二次判断比较。只有在质量可接受且成本或延迟确有改善时才报告 Jev 收益。
- 自动处理部分实测正确率目标至少 95%，报告统计置信区间并最大化满足目标的覆盖率；默认阈值在验证集选择，测试结果不足或不达标则如实标记。
- **BREAKING（新业务任务内部运行协议）**：新任务使用新的图与计划版本，替代当前普通队列循环。旧 business-2 任务保留隔离恢复路径，不原地改写 checkpoint、不重放已提交操作。旧低分自动 primary fallback 不用于新条件计划；硬权限、审批和未知提交核实保持有效。

本轮不接入 Pi 实现、不支持任意未注册工具、不连接御龙真实服务、不变更 MCP 协议、不实施部署或删除历史仓库修复功能。实施后可删除新业务默认路径中的重复队列逻辑；兼容恢复和基线所需实现仍隔离保留。

## Capabilities

### New Capabilities

- `typed-business-plans`：可信判断模板、类型化条件计划、参数引用、静态校验与规划器边界。
- `conditional-plan-execution`：持久节点执行、批量判断、局部重规划、分层预算、旧任务兼容和副作用恢复。
- `confidence-gated-intervention`：分类型阈值、有效策略、持久人工接管与原节点恢复。
- `business-context-memory`：业务工作记忆、证据上下文、受限跨任务记忆及隔离、纠错和撤销。
- `verified-workflow-reuse`：复用计划发布、适用性校验、版本失效和零主模型执行。
- `conditional-workflow-workbench`：计划与决策展示、人工操作、可观测性和使用文档。
- `selective-business-evaluation`：EnterpriseOps-Gym 主评、τ-Bench 补充评测、完整工作流筛选、Jev 与主模型配对比较、阈值选择及质量/成本/延迟验收。

### Modified Capabilities

无已落盘的主规格可直接修改：`openspec list --specs --json` 返回空列表，因此以上均为 ADDED 能力。相关历史契约仍位于 business-workflow-agent、mcp-business-runtime 和 build-decagent-workbench 的 change specs；本 change 明确新运行版本的差异并保留治理约束，不在本轮隐式归档或重写旧 change。

## Impact

- 规划根和实现目标均为 DecAgent 项目根目录；规划文件保存在 openspec/。本次只生成规划产物，后续 apply 才修改 DecAgent。
- 主要影响 decagent/business/{services,graph,worker,planner,questions,calibration}.py、新计划/上下文/记忆模块、models/primary.py、storage/usage.py 及追加迁移、gateway/business.py 与业务工作台、evaluation/business 和 evals。
- 复用现有 MCP 客户端/服务端、employee actor、prepare/确认/commit、幂等回执、租约与 PostgreSQL/LangGraph 持久化；不引入必需 Pi、向量数据库或外部 trace 服务。
- 评测环境只在许可、版本和运行要求核实后固定 benchmark/task/容器镜像/数据库快照指纹；不得以缩小或改写任务来适配 Agent，也不得将意图分类成绩称为流程成绩。此前累计 Jev 1 美元预算继续生效，千问免费额度仍记录请求与 token；部署和独立 Linux 验收保持后续待办。

