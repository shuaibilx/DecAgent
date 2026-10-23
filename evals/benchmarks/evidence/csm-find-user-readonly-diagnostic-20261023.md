# CSM `find_user` 隔离只读诊断

执行日期：2026-10-23（Asia/Shanghai）

## 范围

此诊断只调查 v10 CSM/ReAct 记录中的 MCP `ValidationError` 假设。它没有恢复或重放原始路线，也没有运行模型、官方评分器或业务写入。环境来自冻结的 CSM 镜像与快照；查询使用合成的不存在用户标记，不使用历史任务参数。

## 结果

- 创建并清理一个全新 CSM 隔离环境；cleanup verified。
- 发起 1 次 `find_user` MCP 只读调用；模型请求为 0。
- MCP 返回非错误结果；评测目录 `EvaluationCatalog.observe` 接受该响应，没有复现 output-schema `ValidationError`。
- 调用前后数据库状态指纹相同。
- 未读取或输出原始用户记录、响应正文、任务 prompt、私有 verifier 或标签。

## 结论

该合成响应不能复现历史异常，只能否定“此类无匹配响应必然触发 schema 错误”。历史路线没有保存 MCP 原始响应、调用参数或 traceback，因此无法判断原异常来自 output schema、其他适配层，还是具体返回数据。历史 attempt 继续保持 `unknown`，不更改账本、不重放；9.6.F 仍未完成。后续如获准新建评测试次，应保留经过脱敏的验证阶段、schema/instance 路径及异常链摘要，再定位同类失败。
