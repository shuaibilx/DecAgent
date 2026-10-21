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

### Requirement: Atomic dispatch reservations and unknown usage

每次模型派发 MUST 原子预留全站、游客与任务额度；不能建立可靠费用上界时 MUST 不派发。已知用量按实际结算，未知用量 MUST 保留预留并停止该任务的新模型请求，不以重启、换会话或跨日归零规避。unknown 所属日期保持可审计。

#### Scenario: Concurrent requests exhaust remaining allowance
- **WHEN** 两个请求竞争仅够一个的剩余额度
- **THEN** 至多一个获得派发资格，另一个得到明确限额原因，不能先调用再补账

#### Scenario: Provider timeout
- **WHEN** 已派发请求超时且 usage 未知
- **THEN** 原预留保持占用，该任务不自动重试；其他任务仅可在扣除全部占用后的剩余额度内准入

### Requirement: Bounded execution and waiting queue

试用 MUST 限制每游客同时执行 1 个任务、全站 2 个、等待队列 20 个。排队、重启、取消、租约过期和恢复 MUST 使用持久状态，不重置额度或重复分配执行权。限额与等待原因 MUST 对游客可见。

#### Scenario: Full queue
- **WHEN** 新任务到达且等待队列已满
- **THEN** 系统拒绝准入并提示稍后重试，不创建不可控后台执行或扣除模型用量

### Requirement: Anonymous admission rate control

系统 MUST 对游客创建、任务提交和人工回复限流；可信客户端地址只能从配置受信代理解析。浏览器标识/IP MUST NOT 被视为可靠个人身份；全站模型和资源限额 MUST 在新建游客会话后仍不可绕过。

#### Scenario: Repeated new guest sessions
- **WHEN** 访问者清除 Cookie 后重复创建游客
- **THEN** 创建速率和全站额度继续生效，不能通过新 workspace 获得无限全站预算
