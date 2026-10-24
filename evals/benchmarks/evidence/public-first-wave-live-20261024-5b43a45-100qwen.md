# 公开评测首波诊断报告：100 请求上限

## 结论

2026-10-24 在候选提交 `5b43a45962ee16bc0f61015d1063ee9ad6c676fe` 上执行了已授权的四域首波：4 个固定任务，ReAct 与 Plan 共 8 条路线。8 条均已终态，结果为 **0 成功、3 failed、5 unknown**；8/8 路线清理 verified。只有 ITSM/ReAct 与 τ telecom/ReAct 到达官方评分器，两者均为 **0.0**。因此本次验证了部分真实派发、工具调用、评分和清理路径，但没有通过四域端到端质量门槛，也不能证明 Jev 的收益。

正式 smoke 清单为 24 个任务/48 条路线；本次仅选首波 4/24 个任务和 8/48 条路线。EnterpriseOps 206 条与 τ-Bench 80 条正式 test 的全量成绩仍为 **0/286**。Jev、通用语义裁判和跨模型降级均关闭，本次不包含 Jev 对照。

## 路线结果

| 领域 | 路线 | 终态 | 官方评分 | 主要结果 | 清理 |
| --- | --- | --- | --- | --- | --- |
| EnterpriseOps CSM | Plan | unknown | 未评分 | Qwen worker 在发送确认阶段超时；无可核验 usage | verified |
| EnterpriseOps CSM | ReAct | unknown | 未评分 | `find_entitlements` 工具输出校验异常；该工具 attempt 记 unknown | verified |
| EnterpriseOps ITSM | Plan | unknown | 未评分 | Qwen worker 在发送确认阶段超时；无可核验 usage | verified |
| EnterpriseOps ITSM | ReAct | failed | 0.0 | 模型响应校验失败；官方 verifier 判定未成功 | verified |
| τ-Bench retail | Plan | unknown | 未评分 | Qwen worker 在发送确认阶段超时；无可核验 usage | verified |
| τ-Bench retail | ReAct | failed | 未评分 | 模型 callback 抛出 `ValueError`，未到官方评分器 | verified |
| τ-Bench telecom | Plan | unknown | 未评分 | Qwen worker 在发送确认阶段超时；无可核验 usage | verified |
| τ-Bench telecom | ReAct | failed | 0.0 | 官方 τ-Bench evaluator 判定未成功 | verified |

所有 unknown 均保持终态，没有重放；unknown 后未继续当前路线。CSM 的 unknown 工具按注册元数据标为 write-effect。全批工具账本为 12 settled、1 unknown，其中 2 个 settled 工具也按注册元数据标为 write-effect；禁止副作用的实际发生数量**未测量**，不能据此声称为零。

## 用量与费用边界

| 角色 | 请求 | 已结算 Token | unknown 预留 Token | 批次上限 |
| --- | ---: | ---: | ---: | ---: |
| Agent primary | 23（19 settled、4 unknown） | 200,449 | 257,648 | 60 请求 / 600,000 Token |
| τ 用户模拟器 | 10（全部 settled） | 32,788 | 0 | 32 请求 / 320,000 Token |
| 裁判 | 0 | 0 | 0 | 8 请求 / 80,000 Token |
| **Qwen 合计** | **33 / 100** | **233,237** | **257,648** | **1,000,000 Token** |

按冻结 Qwen 费率计算，已结算用量的抵扣前估算为 **¥0.2045959**；4 个 unknown primary attempt 保留的费用上界为 **¥0.2683776**，二者合计 **¥0.4729735**，低于本批 **¥2.70** 上限。以上是冻结费率估算与预留，不是供应商账单或代金券扣款明细；33 次请求均没有逐请求供应商账单凭证。实际成本完整性为 false。

工具调用 **13/400**（12 settled、1 unknown）；计入已结算用量和 unknown 预留后的 Qwen Token 为 **490,885/1,000,000**。Agent 为 **458,097/600,000**，用户模拟器为 **32,788/320,000**。耗时约 **811.3 秒**，未触及 2 小时停止线。Jev 和 judge 请求均为 0；DeepSeek 降级关闭。CI run [40043584670](https://github.com/shuaibilx/DecAgent/actions/runs/40043584670) 对同一 SHA 通过。

## unknown 与成功率的解释

4 个 primary unknown 均记录为 `model_worker_request_pre_send` / `TimeoutError`，且没有 provider request ID。系统无法从本地证据证明服务端是否收到请求，因此按安全策略保留 Token 与费用预留并禁止重放；这**不等于确认服务端已计费**。本批 Agent primary 请求的 unknown 比例为 4/23，用户模拟器为 0/10；样本很小，只说明本次运行的观察结果，不能作为长期故障概率估计。

这 4 次 Plan 请求的本地输入预留上界分别为 50,794–61,959 Token，另各预留 8,192 输出 Token；输入上界由序列化 UTF-8 字节和 schema 开销估算，不是提供方 Token 用量。19 次已结算 Agent 请求的实际输入为 6,821–15,133 Token，均值 10,154。Plan 与 ReAct 的请求大小和超时有相关现象，但该上界不等于实际 prompt tokens，不能据此认定输入过大是根因。执行日志还出现 `langchain-openai` 关于自定义 HTTP transport 禁用系统代理自动发现的警告。针对实际 `OneShotPrimaryModel` worker 路径，在安装版本 `langchain-openai 1.7.2` / `httpx2 2.13.1` 上用本地假 HTTP 代理运行隔离探针：worker 经注入的 `httpx2.Client` 发出一个 mock 请求并正确解析 3 个 mock Token；warning 虽出现，代理仍实际收到请求。该测试不访问模型提供方，不证明 Qwen 端点可达，但排除了“这条 warning 本身就证明代理被绕过”的推断。4 次 Plan 超时的上游原因仍未知。

本次没有完整成功任务，也没有 ReAct、Plan、Jev 三路线可比配对。两条官方评分结果都为 0.0，另外 6 条未评分；不计算可代表基准的成功率区间，不外推完整 smoke 或全量 benchmark，也不形成成本/延迟优势结论。

## 可复现材料

- [机器报告](public-first-wave-live-20261024-5b43a45-100qwen.json)记录了每条路线、模型 attempt、工具 attempt、评分、清理和预算聚合；未保存 prompt 正文。
- [冻结限额](public-first-wave-limits-20261024-5b43.json)与[冻结 manifest](../manifests/comparison_trial_manifest_v10-100qwen-20261024.json)对应本批授权范围。Manifest digest 为 `a718b5c5c3f9902186d3be5b267fcea9a64f3ff015b5608d80248b0b5613d70a`，profile digest 为 `4fa091d2840928bfb6a00dccfbd452a2dee8c42c465512955fd63923eb0a6721`。
- 在候选 SHA 的隔离工作树、加载项目本地凭证但不输出凭证的环境中执行：

  ```powershell
  $env:DECAGENT_TAU_PYTHON = '.decagent/evaluation-runtimes/tau2-96a3ef9-py312/Scripts/python.exe'
  python -m evals.benchmarks.run_public_trials `
    --root . `
    --manifest .decagent/evaluation/cohorts/manifest-100qwen-20261024.json `
    --report evals/benchmarks/evidence/public-first-wave-live-20261024-5b43a45-100qwen.json `
    --tau-python $env:DECAGENT_TAU_PYTHON `
    --execute --mode smoke --phase first-wave
  ```

- 上述是本次受控执行的参数形状；live 派发会产生真实 API 费用，不能用于重放本批或本批 unknown，必须先冻结新 cohort 并获得新授权。旧 v10 账本未重用；本次使用新的隔离 cohort。因 τ 固定 Python 环境原有 editable import 指向主工作树，执行前临时加入了候选源码路径 overlay，执行后已移除。主工作区原有未提交变更未带入候选工作树。机器报告的 `working_tree_dirty=false` 是启动前源码门禁状态；runner 生成机器 JSON 后，该 JSON 成为候选工作树唯一的未跟踪文件，已按原字节复制到主工作区。
- 主工作区归档副本 SHA-256：机器报告 `d66f50b980472bd9ef1b1f1def730c969d9437805d2a018f936ba84009c1013b`；冻结 manifest `5d435b2da1be9302b99173a84db0868748fa8bdf1fa091c60c7f05fbb4dae144`；限额文件 `6d493505015ce14738a970523c20ad0364f01426f99ae15cc63c0ebcf44c8be1`。

## 后续验收

保持 `9.6.F`、`9.6.G`、`10.3`、`10.4`、`10.5`、`10.6` 未完成。当前阻塞包括计划路线的发送前超时、MCP 输出校验 unknown、两条未评分 ReAct 失败，以及缺少 CSM/τ retail 官方评分。先定位这些工程故障并通过离线/隔离回归；不得重放本批 unknown。补跑剩余 smoke、运行 full 206+80、测量 Jev 或宣称任何模型优势都需要新批次冻结和授权。
