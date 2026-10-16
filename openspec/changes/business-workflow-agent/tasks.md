# Business Workflow Agent Implementation Plan

> Use task-by-task implementation with specification and quality review. OpenSpec tasks below are the tracking source; target code is ../DecAgent. No Git initialization, commit or push is authorized by this change.

**Goal:** 独立承接 CLI + Skill 业务插件，并验证 Jev 对本地企业工单的判断与执行价值。

**Architecture:** 插件注册表统一工具与执行契约。LangGraph 在规则/Jev 与主模型之间路由，共用程序治理、事务回执和持久化任务基础设施。

**Tech Stack:** Python 3.11+、LangChain/LangGraph、FastAPI、PostgreSQL、JSON Schema、TypeSafe Jev Decisions API。

## 1. 业务任务基础设施

- [x] 1.1 新增 005 迁移与业务作用域，按 task kind 保留修复约束；验证新旧任务创建、事件外键、迁移重入及旧迁移 checksum 未改变。
- [x] 1.2 按类型分派兼容检查和 worker 领取，业务不初始化 GitHub/broker；验证旧 checkpoint 恢复及无 GitHub 的业务启动，并更新迁移说明。

## 2. 插件与工单服务

- [x] 2.1 实现双级插件契约、注册、禁用和摘要冻结，以及同源工具 JSON Schema；测试无效清单、嵌套参数、未知参数、修改和禁用，交付插件接入说明。
- [x] 2.2 实现受控 JSON CLI runner；测试 shell 注入、密钥环境隔离、超时、输出限制和非法回执，记录可信插件执行边界。
- [x] 2.3 实现 PostgreSQL 工单目录、全生命周期、历史、乐观版本和事务回执；测试非法状态、重名、过期写入、相同键不同输入与响应丢失核实。
- [x] 2.4 提供可独立运行的工单 CLI、增强契约与 Skill；按文档运行目录查询、创建、分派、解决、关闭、重开及回执查询，验证安装包资源可用。

## 3. 类型化业务判断

- [x] 3.1 扩展 Score 请求、响应、序列化和严格概率校验，兼容旧 Choice/Noul；通过新三类型测试与旧决策回归，更新决策格式说明。
- [x] 3.2 构造契约分支、真实候选和 Noul/Score 问题，批量独立判断并保留来源；测试 no_match、缺失证据、重名及版本绑定，交付题目与评分示例。
- [x] 3.3 增加 synthetic-reference 校准和版本化业务路线；测试无校准、低确定性、模型变更、非法响应和不可用降级，不冒充人工标注，更新校准操作说明。

## 4. Agent 执行与人工介入

- [x] 4.1 为主模型注入插件同源工具并实现延迟初始化；基础插件及复杂请求能规划注册操作，测试非法计划拒绝和 finish 回执核实，记录规划协议。
- [x] 4.2 实现持久化业务 LangGraph 和 worker，执行有界 prepare/decide/plan/validate/execute/verify；测试常见路径、复杂路径、预算、取消及 checkpoint 恢复。
- [x] 4.3 实现按动作确认、澄清、编辑、拒绝和资源新鲜度重评；测试直接关闭禁止、过期确认、重复回复和缺失参数，更新人工介入指南。
- [x] 4.4 实现未知写入的回执查询和状态核实，复用已完成动作；故障注入测试写入后崩溃、worker 租约过期和重复恢复，交付恢复说明。

## 5. API、CLI 与工作台

- [x] 5.1 新增插件和业务任务 API，复用任务查询/SSE/取消/恢复/回复，保留 owner 认证和幂等要求；测试 auth/CSRF/输入/重复请求与旧修复 API，更新接口说明。
- [x] 5.2 新增管理 CLI 的插件和业务提交操作，并让默认网页展示业务与决策证据，保留修复入口；验证浏览器创建、确认、澄清、重连与历史任务查看。
- [x] 5.3 同步 README、旧指南入口并生成当前使用指南、详细架构及离线图册；验证命令、链接和 Mermaid 流程/泳道/时序/组件图，文件按北京时间归档至 generated。

## 6. 评测与本机综合验收

- [x] 6.1 生成并冻结 40 开发/80 验收合成案例和三策略对照工具；测试同等信息、失败分母、标签来源、三轮交错、人工等待分离和预算停止，交付评测说明。
- [x] 6.2 完成离线、PostgreSQL、真实 CLI 和浏览器综合测试及旧功能必要回归；运行 Ruff 与安装检查，生成本机验收证据。
- [x] 6.3 在已有模型配置和累计 Jev 1 美元上限内完成探测、开发校准与真实固定集三策略三轮评测；核对实际 usage、回执和延迟，按验收指标记录通过或未通过，不把模拟结果当作实测。
- [x] 6.4 按既有目标完成契约修复辅助读取导致的错误失败，核实 W⊆C⊆O 并拒绝 goal_unproven；在固定运行结束后实施，完成离线、真实 PostgreSQL/CLI 与有限真实探测，保留修复前完整实测并同步现版本与验收范围说明。
- [x] 6.5 修复完整主模型响应因提案拒绝而丢失已知用量的问题，保留严格用量、模型与安全错误边界，以及可信派发前零结算、旧修复等待不重发和主模型诊断幂等记账；在固定运行结束后实施，验证预算及持久化回归，不回填历史未知记录，并同步说明。

## 当前实施证据（2026-10-13）

完成协议与拒绝用量修复分别通过规格和质量审查。当前源码验证：Windows deterministic 852 passed/1 skipped；Docker Python 3.11、network none 835 passed；PostgreSQL integration 187 passed/22 skipped；真实 PostgreSQL/CLI 评测集成 3 passed；Ruff 219 文件与两个环境 pip check 通过。专项完成协议 26 项、严格拒绝用量 117 项、持久化/预算/诊断 18 项包含在综合结果中，不另行加总。

[现版本修复与功能验证](../../../generated/261013/0158_现版本修复与功能验证.md)保存三个独立新案例、三策略共9次真实探测：main/hybrid各3/3，rule 2/3，复合请求等待澄清；全部用量已知、禁止写入0，无新增Jev调用。Windows API/business worker及Docker repair worker已更新，健康正常，原owner与工单保留；当前wheel目录外导入、内置资源和6个核心源码指纹均核实。[本机验收记录](../../../generated/261012/0900_本机业务验收记录.md)列出镜像、包指纹、9种图册视图、10张Mermaid、92个本地文档链接及实际截图。

[修复前完整720次实测](../../../generated/261012/2341_业务固定集完整实测.md)严格not_passed（7个hybrid trial用量不完整），原报告、失败分母和未知用量保持不变；旧39次部分报告独立保留。当前有限功能验证不替代新的性能验收；六类Jev快路线禁用，没有Jev提效证明。独立Linux业务适配及部署按用户确认保留后续待办，不属于已完成的本机范围。本change不自动归档。

## Workflow follow-up

- 独立 Linux 部署待用户具备主机后另行验收；本 change 不以本机容器替代该结论。
- 全部实施与评测结果经过审查后再显式归档，本轮不自动归档历史 change。
