# Spec Delta

## Purpose

为匿名游客试用建立服务端共享资源边界，使模型免费额度、实际支出、任务并发和持久化资源不被重复会话绕过，同时保留未知请求的预留与审计，不影响独立公开基准账本。

## ADDED Requirements

### Requirement: Layered daily trial limits

试用 MUST 遵守全站每日 $5 美元等值 API 费用及 5,000,000 Qwen token 上限、每游客每日 30 个任务及 500,000 Qwen token 上限。日界 MUST 使用北京时间；原有每任务限制继续生效，公开基准 Jev ledger 保持隔离。

#### Scenario: Free main model quota
- **WHEN** 千问当前免费且实际支付为零
- **THEN** 仍累计其调用和 token，达到 token 或任务上限后拒绝新准入，不因零支付无限调用

### Requirement: Conservative currency billing and snapshots

游客费用 MUST 按抵扣前 API 费用计入 `$5` 上限，并另存券后实付。人民币账单 MUST 使用每个北京时间预算日不可变的 CFETS USD/CNY 中间价快照，首版留 10% 保守余量；报价日期超过 7 个日历日 MUST 停止 CNY 派发。换算 MUST 使用十进制数并向上取整预留；缺少可核验汇率或可靠费率上界时 MUST 拒绝派发。

#### Scenario: Voucher-backed CNY usage
- **WHEN** Qwen 抵扣前费用为 ¥2，代金券使实付为 ¥0，当日冻结中间价为 6.7200 CNY/USD
- **THEN** 预算使用 `floor(6.7200 × 0.90, 2) = 6.04 CNY/USD` 换算抵扣前费用为至少 `$0.331126`，并分别记录 ¥2 API 费用与 ¥0 实付；该示例来源报价见[CFETS 官方汇率页面](https://www.chinamoney.com.cn/english/)，其报价日期为 2026-10-20

#### Scenario: Frozen rate outside a trading day
- **WHEN** 北京时间预算日为周末或市场休市日
- **THEN** 系统使用最近一个已发布的官方中间价，并将报价日期、基础汇率、来源 URL、安全余量及有效汇率写入当天不可变快照

#### Scenario: CNY provider charges use gross usage
- **WHEN** Qwen 返回人民币计价的抵扣前 API 费用，用户用代金券抵扣了全部或部分实付
- **THEN** 系统仍按该预算日已冻结的 CFETS 汇率和安全余量将抵扣前费用计入 `$5` 美元等值上限，并单独记录券后实际付款；缺少可靠用量或费率时保留 unknown 预留或拒绝新派发

### Requirement: Atomic model attempt reservations

每个模型 request attempt MUST 在派发前以独立编号原子预留 task 与 trial 额度，并使用同一 usage record 关联。没有可靠费率版本或费用上界时 MUST 拒绝派发；任务预算拒绝时所有预留 MUST 回滚。

#### Scenario: Concurrent requests exhaust remaining allowance
- **WHEN** 两个请求竞争仅够一个的剩余额度
- **THEN** 至多一个获得派发资格，另一个得到明确限额原因，不能先调用再补账

#### Scenario: One concurrent request is denied
- **WHEN** 并发模型请求竞争仅够一个的剩余额度
- **THEN** 最多一个 attempt 获得派发资格；另一个不得先调用 provider 再补记账

#### Scenario: Unpriced request is rejected before dispatch
- **WHEN** 游客模型请求没有可靠费率版本、费用上界或 CNY 换算所需的有效汇率
- **THEN** 系统拒绝派发，且不创建可计为 provider 已发送的 attempt

#### Scenario: Jev guest request has no verified fee ceiling
- **WHEN** 游客任务请求 Jev，而配置中没有冻结且可核验的费率上界与强制请求/响应 token 上限
- **THEN** 系统在模型派发前拒绝该调用，不发送 provider 请求，也不写入 task 或 trial 模型预留

#### Scenario: Generic routing price preference is not an endpoint fee guarantee
- **WHEN** 游客 Jev 费用上界仅依据通用 Provider Routing 文档中的 `provider.max_price`，而 Decisions API 专属契约没有明确保证该端点执行这个上限
- **THEN** 系统不得将该偏好当作已验证的费用上界，必须在模型派发前拒绝游客 Jev 请求

### Requirement: Native-currency model settlement

账本 MUST 保存 provider 本币费用、费率版本与费用依据；已知费用按抵扣前费用结算。Voucher 与 cash 只有在 provider 响应或账单有证据时分别记录，缺失时 MUST 保持未知，不得推断为零。

#### Scenario: Known Qwen usage settles in native currency
- **WHEN** 游客任务的 Qwen 请求有 CNY 版本费率并返回确定的输入/输出 token
- **THEN** 系统按固定费率计算抵扣前 CNY 费用，以当天冻结的 CFETS 汇率和安全余量换算并结算 task/trial 账本；没有账单凭证时不虚构代金券或现金金额

#### Scenario: DashScope provider alias preserves Qwen token accounting
- **WHEN** 游客任务使用 `dashscope` provider 标识调用 Qwen 模型，并提供有效的 CNY 费率与输入/输出 token 上界
- **THEN** 系统按 Qwen 语义原子预留全站与游客 Token 额度，并在已知用量后按本币费率结算；`dashscope` 别名不得绕过 Qwen Token 上限或被策略校验错误拒绝

#### Scenario: Jev request reports USD billing
- **WHEN** 游客业务的 Jev 请求经 OpenRouter 返回确定的 token 与 usage cost
- **THEN** 同一 task/trial attempt 以 USD 保存本币费用及 provider-billed 依据，费用进入 trial 日上限，公开评测账本仍保持隔离

### Requirement: Unknown usage retains reservation

已派发模型 attempt 出现 unknown 后 MUST 保留原费用和 token 预留，并阻止该游客 task 的后续模型派发；重启、换会话、state 路径或跨日 MUST NOT 释放占用。只有可信本地代码证明请求未发送时才可释放预留。

#### Scenario: Provider timeout
- **WHEN** 已派发请求超时且 usage 未知
- **THEN** 原 task 与 trial 预留保持占用，该游客任务不自动重试或切换 fallback；重启进程或跨日后仍保留同一 unknown attempt，其他任务仅可在扣除全部占用后的剩余额度内准入

### Requirement: Bounded execution and waiting queue

试用 MUST 限制每游客同时执行 1 个任务、全站 2 个、等待队列 20 个。排队、重启、取消、租约过期和恢复 MUST 使用持久状态，不重置额度或重复分配执行权。限额与等待原因 MUST 对游客可见。

#### Scenario: Full queue
- **WHEN** 新任务到达且等待队列已满
- **THEN** 系统以429拒绝准入并提示稍后重试，任务和每日任务计数在同一事务中回滚，不创建不可控后台执行

#### Scenario: Enqueue and daily task count commit atomically
- **WHEN** guest workspace 提交业务任务、等待队列尚有名额且每日任务额度充足
- **THEN** 任务、队列 admission 和每日任务计数原子提交；并发重试使用同一幂等结果，不重复计数

#### Scenario: Per-workspace and global active limits
- **WHEN** 多个 Worker 并发领取 guest 任务
- **THEN** PostgreSQL 串行化领取账本，同一 guest workspace 最多有一个有效运行 lease，全站最多两个；其他 workspace 可在剩余槽位继续领取

#### Scenario: Fair claim skips a busy workspace
- **WHEN** 最早的等待项所属 workspace 已有有效运行 lease，后续 workspace 仍有等待任务
- **THEN** Worker 跳过该 workspace 的后续任务并按入队时间领取最早的其他可运行任务；不因单个 workspace 阻塞全局队列

#### Scenario: Cancel queued task
- **WHEN** 用户取消尚未领取的 guest 任务
- **THEN** durable queue state 变成 cancelled、等待名额可复用，但成功入队已占用的每日任务计数不退还

#### Scenario: Expired lease recovery
- **WHEN** Worker lease 过期且多个 Worker 同时尝试恢复同一 guest 任务
- **THEN** 任务保留原 ID 和 checkpoint，只生成一个新 lease generation；旧 owner 的 generation 被拒绝，不发生双重领取

#### Scenario: Guest sees queue status
- **WHEN** guest 查询任务详情或任务列表
- **THEN** 响应显示持久 queue state、等待位置、等待原因及并发/等待上限；非 guest 员工响应不泄漏 guest 队列数据

### Requirement: Guest request rate limiting

系统 MUST 使用共享 PostgreSQL 滑动窗口限制游客新会话（每可信客户端 IP 每小时 5 次）、游客任务提交（每分钟 10 次）和人工回复（每分钟 30 次）。有效会话恢复或 CSRF 修复不消耗新会话额度。检查发生在 CSRF/Origin 验证后、业务处理前；超限返回含秒数的 `Retry-After`。

#### Scenario: Repeated new guest sessions
- **WHEN** 访问者清除 Cookie 后重复创建游客
- **THEN** 同一客户端 IP 的第六个新 session 在滚动一小时内被 429 拒绝并返回 `Retry-After`；Cookie 清理不重置计数，不能通过新 workspace 获得无限全站预算

#### Scenario: Guest task and manual reply limits
- **WHEN** 同一客户端在滚动一分钟内超过 10 次任务提交或 30 次人工回复
- **THEN** 超额请求在创建任务/改变审批状态前收到带 `Retry-After` 的 429；其他客户端、员工请求与独立限流 scope 不互相消耗额度

### Requirement: Trusted proxy client resolution

系统 MUST 默认使用 TCP 对端作为客户端限流地址。仅当对端属于显式配置的可信代理 CIDR 时，才解析 `X-Forwarded-For` 和单值合法 `X-Forwarded-Proto`；Uvicorn 不得隐式信任代理头。

#### Scenario: Untrusted forwarding headers are ignored
- **WHEN** 直接连接客户端发送伪造的 `X-Forwarded-For` 或 `X-Forwarded-Proto`
- **THEN** 速率限额按 TCP 对端地址计算，scheme/客户端地址不受未配置代理的头部影响

#### Scenario: Trusted proxy chain resolution
- **WHEN** 连接对端属于显式受信 CIDR，且代理提供了有效转发链
- **THEN** 系统从右向左跳过可信代理地址，使用第一个非受信地址作为限流主体；未知或畸形链退回代理对端地址

#### Scenario: Proxy chain contains client-supplied addresses
- **WHEN** 可信代理转发客户端提供的地址链
- **THEN** 代理覆盖或追加实际上游对端地址；系统从右向左跳过可信代理，只选取首个非可信地址，否则回退到连接对端

### Requirement: Privacy-preserving rate-limit identity

限流账本 MUST NOT 保存原始客户端 IP；系统使用部署级随机 HMAC key 保存不可直接还原的地址摘要。限流数据跨进程、重启和清除浏览器 Cookie 保持有效。IP 仅为限流信号，不代表个人身份；全站资源额度不得因新建 session 而绕过。

#### Scenario: Rate limits survive cookie clearing and restart
- **WHEN** 访问者清除 Cookie、切换 API 进程或服务重启
- **THEN** 已记录的限流事件仍计入对应滑动窗口，数据库中没有原始客户端 IP

#### Scenario: Database snapshot does not reveal the HMAC secret
- **WHEN** 限流事件写入共享数据库
- **THEN** 数据库保存 HMAC 地址摘要和不可逆的部署密钥指纹，不保存部署级 HMAC 密钥；该密钥由 secret/env 注入且所有 API 实例使用同一稳定值

#### Scenario: Missing deployment HMAC secret
- **WHEN** 操作者开启游客试用但没有配置 32 字节部署级 HMAC 密钥
- **THEN** 配置校验拒绝启动该游客配置，限流器不得生成或读取数据库密钥，也不得放行游客准入

#### Scenario: HMAC secret rotation
- **WHEN** 操作者需要更换部署级 HMAC 密钥
- **THEN** 必须先关闭游客准入，等待 2 小时限流事件保留窗口结束，再将新密钥同步配置到所有 API 实例后重新启用

#### Scenario: Early HMAC key rotation is blocked
- **WHEN** 部署密钥与数据库中记录的密钥指纹不匹配，且过去 2 小时内存在限流事件
- **THEN** 限流器不写入新事件，游客准入返回可重试的服务不可用响应，并给出旧窗口过期前的 `Retry-After`

### Requirement: Guest quota and queue status visibility

游客界面 MUST 显示当前预算日、全站和当前空间资源用量及上限，并在任务详情中显示队列状态/位置和服务端给出的重试秒数。

#### Scenario: Guest usage and queue information is visible
- **WHEN** 游客打开工作台或查看任务
- **THEN** 页面显示北京时间预算日、全站和当前游客的任务/Token/费用占用及上限；游客任务详情显示排队状态、位置、等待上限和并发限制，429 错误展示服务器给出的重试秒数
