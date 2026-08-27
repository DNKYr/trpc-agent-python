# 多租户与节点部署——4 周裁剪交付计划

> 状态：当前 4 周实施计划
>
> 需求基线：[`issue.md`](./issue.md)  
> 完整设计：[`multi_tenant_node_deployment.md`](./multi_tenant_node_deployment.md)
>
> 本计划裁剪的是**实现深度和可选增强项**，不裁剪 `issue.md` 明确要求的能力。

## 1. 修正说明

上一版把 PostgreSQL 固定为唯一生产后端，这不符合 `issue.md` 的核心要求：

> 支持不同租户选择不同数据后端，例如 InMemory、Redis、SQL、向量库、对象存储或外部 Memory 服务。

本版修正为：

- **不同租户可独立绑定不同后端**；
- 复用 SDK 已有 InMemory、Redis、Redis Cluster、SQL 和外部 Memory 实现；
- 通过统一 `BackendFactory` 装配，不把 PostgreSQL 设为唯一数据面；
- PostgreSQL 只是推荐的控制面配置后端和可选业务后端之一；
- Redis Streams 是首期消息队列，Kafka 作为接口兼容设计，不要求 4 周内实现；
- 4 周内完成 `issue.md` 的四个子项目，每周关闭一个项目。

## 2. 四周范围总览

| 周 | 子项目 | 必须完成的交付 |
|---|---|---|
| Week 1 | 数据同步与多后端 | 租户后端绑定、统一访问抽象、Session 并发方案、event/state/summary 顺序、Memory 可见性、两类迁移方案、IM 幂等、后端一致性取舍和最小数据模型 |
| Week 2 | IM 软件接入 | 企业微信 + Telegram Adapter、消息双向转换、账号/租户绑定、验签去重、群聊/单聊 Session 规则、长度/频率/异步/文件/撤回/重试策略 |
| Week 3 | 治理、监控与安全 | Filter 治理、指标、OTel 全链 Trace、审计字段、配置/数据/工具隔离、密钥管理和日志/Trace/错误脱敏 |
| Week 4 | 故障恢复与运维 | 节点/IM/数据库/模型/工具故障策略、灰度和租户配置回滚、容量评估、最小部署和生产部署方案 |

### 2.1 交付方式

每周交付包含三层，避免把“设计完成”误当成“所有后端都已生产实现”：

1. **设计闭环**：接口、状态机、时序、数据模型、错误语义和安全边界明确；
2. **关键原型**：验证本周最大技术风险；
3. **验收测试**：用最小场景证明设计可实施。

4 周的目标是完成多租户节点部署的**可实施设计与关键骨架**，不是在 4 周内把所有可选后端、所有 IM 富媒体能力和全部运维自动化生产化。

## 3. 不可裁剪项与可延期项

### 3.1 `issue.md` 明确要求，本期不可裁剪

- 租户模型七要素：tenant、应用、模型、工具、IM、数据后端、审计；
- Gateway、Worker、Channel Adapter、Storage Adapter、Admin API、Telemetry Collector 拓扑；
- 多节点路由、正确租户和正确 Session、是否需要 sticky session；
- 配置、数据、工具权限、日志和密钥隔离；
- 不同租户选择不同后端；
- Session、Memory、Summary、Artifact、Knowledge、Audit 的统一访问设计；
- Session 并发、更新顺序、Memory 可见性、Redis→SQL 和本地→远端向量迁移、IM 幂等；
- 至少两类 IM；
- Filter 治理、监控、Tracing、审计、密钥脱敏；
- 故障降级、灰度回滚、容量和部署方案。

### 3.2 可延期，但必须保留扩展点

- Kafka 实现：首期只实现 `QueueABC` + Redis Streams；
- Redis Session V2 全量替换：首期保留现有实现并通过 Session 锁避免并发，V2 追加语义做原型；
- 所有外部 Memory Provider：复用已有 Mem0/Mempalace，新增 Provider 不在本期；
- 所有向量数据库：完成统一接口和本地→一个远端实现的迁移验证，不穷举所有厂商；
- 所有对象存储厂商：实现 S3 协议，COS/MinIO 通过兼容接口接入；
- 微信客服、微信公众号：本期实现企业微信和 Telegram；
- 完整 Admin UI：交付 Admin API/CLI，不做 Web UI；
- 多 Region、Operator、每租户独立 Deployment 自动生成；
- 富媒体高级渲染、复杂卡片模板和自动内容撤回策略引擎。

---

# Week 1：数据同步与多后端支持

## 4. 本周目标

完成租户级后端选择和数据一致性设计，并验证两个租户使用不同 Session/Memory 后端时能够在两个 Worker 间正确运行。

## 5. 多后端架构

### 5.1 控制面与数据面分离

```text
Control Plane
  TenantRegistry(SQL/File)
  TenantConfig / Revision / SecretRef
          |
          v
BackendFactory + TenantRuntimeCache
          |
          +-- SessionService: InMemory / Redis / RedisCluster / SQL
          +-- MemoryService:  InMemory / Redis / RedisCluster / SQL / Mem0 / Mempalace
          +-- SummaryStore:   跟随 Session / SQL
          +-- Artifact:       InMemory / S3-compatible
          +-- Knowledge:      Local vector / Remote vector
          +-- AuditSink:      SQL / Kafka / File / OTel Log
```

控制面推荐 SQL，但本地开发可使用 File Registry。数据面由每个租户的 `DataBackends` 单独选择。

### 5.2 最小后端配置

```python
class BackendSpec(BaseModel):
    type: Literal[
        "inmemory", "redis", "redis_cluster", "sql",
        "s3", "vector_local", "vector_remote",
        "mem0", "mempalace", "kafka", "file", "otel_log",
    ]
    dsn_ref: SecretRef | None = None
    options: dict[str, Any] = Field(default_factory=dict)

class DataBackends(BaseModel):
    session: BackendSpec
    memory: BackendSpec
    summary: BackendSpec | None = None
    artifact: BackendSpec
    knowledge: BackendSpec | None = None
    audit: BackendSpec
```

生产校验规则：

- 多 Worker 租户不能使用 InMemory Session/Memory/Artifact；
- `summary=None` 时跟随 Session；
- Artifact 生产默认要求 S3-compatible；
- Knowledge 共享 collection 时强制服务端注入 `tenant_id` 过滤；
- Secret 只能通过 `SecretRef` 解析，不能写入 `options` 明文字段。

### 5.3 BackendFactory

```python
class BackendFactory:
    def session_service(self, tenant: TenantConfig) -> SessionServiceABC: ...
    def memory_service(self, tenant: TenantConfig) -> MemoryServiceABC: ...
    def summary_store(self, tenant: TenantConfig) -> SummaryStoreABC: ...
    def artifact_service(self, tenant: TenantConfig) -> ArtifactServiceABC: ...
    def knowledge(self, tenant: TenantConfig) -> KnowledgeBase | None: ...
    def audit_sink(self, tenant: TenantConfig) -> AuditSinkABC: ...
```

连接池按 `(backend_type, resolved_dsn_fingerprint, options_fingerprint)` 复用；Service 外层包 tenant-bound facade，业务代码不能传任意 `app_name`。

## 6. 数据同步策略

### 6.1 消息队列和顺序

首期消息队列使用 Redis Streams，按：

```text
partition = hash(tenant_id | app_id | session_id) % N
```

分区只用于降低争抢，不承担正确性。正确性由以下机制保证：

1. per-Session `inbound_seq`；
2. `next_expected_seq`；
3. Session 分布式锁；
4. fencing epoch；
5. Session revision CAS；
6. 确定性 `envelope_id`。

Redis Cluster 下同一 Session 的 queue counter、lock、epoch 和顺序状态使用相同 hash tag。

### 6.2 Session 并发写

- Redis：首期 Worker 必须持有 Session 锁；同时完成 Redis Session V2 的追加/CAS原型；
- SQL：使用 revision 乐观锁；
- InMemory：只允许单进程开发；
- stale Worker 的最终 Session commit 必须被 epoch/revision 拒绝；
- 只允许当前 `inbound_seq == next_expected_seq` 的消息执行。

首期不声称外部工具 exactly-once；副作用工具要求 idempotency key，否则必须进入二次确认流程。

### 6.3 Event、State、Summary 顺序

```text
acquire lock + epoch
  -> verify inbound_seq
  -> append USER event
  -> run Runner
  -> append agent/tool events
  -> apply state delta
  -> commit session revision and next_expected_seq
  -> create post-turn job
  -> release lock
  -> memory ingest raw event range
  -> summary generate and CAS publish
```

约束：

- event 是事实，state 是投影；
- event 和 state 不能跨版本混写；
- `app:`/`user:` state 使用字段级原子更新或各自 revision，不能整体覆盖 JSON；
- post-turn job 保存不可变 `[from_event_seq, to_event_seq]`；
- Memory 必须读取原始 event range，不读取被 Summary 压缩后的 Session 快照；
- Summary 使用 `covers_up_to_seq` 单调发布。

### 6.4 Memory 跨节点可见性

- InMemory：只提供单进程可见性；
- Redis/SQL：成功写入后其他节点立即可读；
- Mem0/Mempalace：最终一致，通过 durable post-turn job 重试；
- Session 保存 `memory_watermark`；Memory ingestion ledger 保存 `ingested_up_to_seq`；
- 读取发现落后时，合并当前 Session 尚未摄取的原始事件进行读修复。

### 6.5 IM 入向幂等

Gateway 必须原子完成去重与入队：

- Redis 模式：同一 Lua 中执行 dedup、分配 `inbound_seq` 和 `XADD`；
- SQL Inbox 模式：同一事务写 dedup identity、Inbox 和 Outbox；
- `envelope_id = UUID5(tenant, channel, external_msg_id)`；
- Worker 再次检查 Turn 状态；
- outbound 使用 `(envelope_id, chunk_index)` 幂等键。

Queue 后端通过 `QueueABC` 抽象；本期只实现 Redis Streams，SQL Inbox 作为故障兜底设计，不强制所有租户业务数据使用 SQL。

## 7. 后端迁移

### 7.1 Redis → SQL Session

设计并实现一个最小迁移 CLI：

```text
dual-write
  -> backfill
  -> shadow-compare
  -> short write barrier
  -> cutover
  -> rollback window
```

4 周范围只要求在测试环境完成一个租户的演练，不建设通用迁移平台。

迁移要求：

- 通过 Session/Event 模型序列化，不复制 Redis 二进制；
- 保留 event ID、顺序、state 和 update time；
- 处理 Redis TTL 与 SQL 过期语义差异；
- 切换使用 migration epoch，旧 epoch 写入被拒绝。

### 7.2 本地向量库 → 远端向量库

完成迁移设计和一个 adapter 级验证：

- embedding 模型相同：导出 ID/vector/payload 后批量 upsert；
- embedding 模型变化：重新 embedding；
- 强制迁移 tenant metadata；
- 校验 count、抽样 recall@k 和 tenant filter；
- 索引完成后才 cutover；
- source 保留至回滚窗口结束。

## 8. Week 1 验收

- Tenant A 使用 Redis Session/Memory，Tenant B 使用 SQL Session/Memory，两个 Worker 均可处理；
- InMemory 后端在多 Worker 配置下启动失败；
- 同一 Session 并发 100 条消息不丢 event、不覆盖 state、顺序单调；
- stale lock owner 无法提交 Session；
- Memory watermark 最终追平；
- 同一 IM 消息重复 20 次只产生一个 Envelope；
- Redis→SQL 完成一次测试迁移和回滚；
- 本地→远端向量库完成 count/filter/recall 验证；
- 输出后端一致性、延迟、成本和运维复杂度对比表；
- 最小表结构覆盖 tenant、app、session、event、memory、summary、channel binding、audit。

---

# Week 2：IM 软件接入

## 9. 本周目标

完成企业微信和 Telegram 两类 Adapter，并形成统一 Channel 接口。

## 10. Channel Adapter

```python
class ChannelAdapterABC(ABC):
    async def verify(self, request, binding) -> None: ...
    async def parse(self, request, binding) -> list[MessageEnvelope]: ...
    def ack_response(self, envelopes) -> HttpResponse: ...
    async def render(self, event, context) -> list[OutboundChunk]: ...
    async def deliver(self, chunk, binding) -> DeliveryResult: ...
```

### 10.1 企业微信

- route key 查候选 binding；
- token/signature/timestamp/nonce 校验；
- AES 解密；
- 文本、图片/文件引用解析；
- 最终回复和已有 `reply_stream` 能力接入；
- access token 共享缓存和刷新锁。

### 10.2 Telegram

- webhook secret header 校验；
- update/message/user/chat 映射；
- 文本、图片/文件引用解析；
- `sendMessage` 和节流后的 `editMessageText`；
- 429 `retry_after`。

## 11. 身份和 Session

使用 tenant-scoped HMAC-SHA-256，不使用无密钥 SHA-1：

```text
single:         tenant|app + mapped_user + channel/user_hmac
per_group:      tenant|app + synthetic_group_user + channel/group_hmac
per_group_user: tenant|app + mapped_user + channel/group_hmac/user_hmac
per_thread:     tenant|app + synthetic_thread_user + channel/group_hmac/thread_hmac
```

真实发言人保存在可信 metadata，用于权限和 Prompt，不用于共享群 Session 主键。

## 12. 平台限制设计

本周必须覆盖：

- 5 秒内 ACK，附件只入队平台媒体引用，异步下载到 Artifact；
- 文本长度按段落/句子分片；
- per-chat + per-tenant Redis 令牌桶；
- 流式 edit 节流；
- 图片/文件通过 S3 Artifact 引用，队列不放二进制；
- outbound delivery 状态机和指数退避；
- 保存 platform message ID，定义撤回接口和失败降级；
- `/new`、`/stop`、`/help` 命令；`/stop` 通过持久 control record + Pub/Sub 唤醒执行节点。

## 13. Week 2 验收

- 企业微信和 Telegram 沙箱完成单聊、群聊和 thread/session 规则验证；
- 伪造签名、错误 secret、过期 timestamp 均被拒绝；
- 平台重复投递不重复调用模型或重复最终回复；
- 长消息正确分片，429 正确退避；
- 图片/文件不进入队列正文，异步落 S3-compatible Artifact；
- 同一用户跨群、跨通道、跨租户不串 Session；
- `/stop` 可跨 Worker 通知正在执行的 Turn；
- Adapter 接口没有平台类型泄漏到 Worker 核心逻辑。

---

# Week 3：治理、监控与安全

## 14. 本周目标

完成租户级 Filter 治理、全链路观测和安全隔离设计。

## 15. Filter 治理

必须覆盖 `issue.md` 列出的五类策略：

| 策略 | 落点 | 首期实现 |
|---|---|---|
| 工具白名单 | ToolPredicate + TOOL Filter | 默认拒绝、deny 优先、执行期二次校验 |
| 敏感信息脱敏 | MODEL/TOOL Filter | 输入、输出、工具参数和结果；流式跨 chunk 缓冲 |
| 预算限制 | MODEL Filter | 调用前原子预留，调用后按 usage 结算，失败释放 |
| 危险工具二次确认 | TOOL Filter + long-running/HITL | confirm token 绑定 tenant/session/tool call，短 TTL、防重放 |
| IM 用户权限 | Gateway Ingress Middleware | tenant status、allowlist、binding、role |

Filter Registry 改为注册 factory/class；TenantRuntime 每租户实例化 Filter 和 Model，禁止共享可变 API key。

## 16. 指标和 Trace

首期必须提供以下指标：

- 请求量；
- 模型调用耗时；
- 工具调用耗时；
- IM 投递成功率；
- 错误率；
- token 消耗；
- 每租户成本；
- Session 后端延迟。

OTel Trace 必须串起：

```text
IM callback
  -> tenant resolve / verify / dedup / enqueue
  -> queue consume
  -> Runner
  -> Model
  -> Tool
  -> Session / Memory IO
  -> IM render / deliver
```

Metric 禁止 user/session 高基数标签；Trace 中身份只记录 HMAC。

## 17. 审计与密钥

审计字段至少包含：

```text
tenant_id, channel, user_id, session_id,
agent_name, tool_name, decision, latency,
error_type, cost, trace_id
```

并补充 envelope ID、revision、action、decision reason、token usage。

安全要求：

- TenantConfig 只保存 SecretRef；
- Secret resolver 支持 env、Vault/K8s 接口设计，首期至少实现 env/K8s；
- IM token、模型 API key、DB password 不进入日志、Trace 和错误消息；
- 日志 RedactingFilter 默认开启；
- Collector 再做 attribute 脱敏；
- tenant-bound facade 阻止伪造 `app_name`/Artifact ID；
- Knowledge tenant filter 服务端强制注入；
- `agent_factory` 只允许管理员 allowlist。

## 18. Week 3 验收

- 禁用工具不暴露且不能通过伪造调用绕过；
- 危险工具未确认不执行，重复 confirm token 无效；
- 并发预算请求不能穿透预留额度；
- 流式跨 chunk Secret 被脱敏；
- 一条消息在 Jaeger 中呈现 callback→Runner→Tool→Storage→reply；
- 指标包含 `issue.md` 要求的八项；
- 审计字段齐全；
- 跨租户对抗测试和 Secret 泄露测试通过。

---

# Week 4：故障恢复与运维

## 19. 本周目标

完成故障策略、灰度回滚、容量模型和部署方案。

## 20. 故障恢复

| 故障 | 策略 |
|---|---|
| Worker 故障 | consumer pending 接管；Turn/Envelope 幂等；Session epoch 拒绝 stale writer |
| IM 重试 | Gateway dedup + Queue 幂等 + Event/Outbound 幂等 |
| Redis 不可用 | Redis 后端租户 fail closed/排队；不自动切 InMemory；SQL 后端租户可继续运行 |
| SQL 不可用 | SQL 后端租户 fail closed；Redis 后端租户按其审计策略运行或暂停；配置使用最后成功 revision |
| 模型超时/限流 | ModelRetryConfig；备用模型；最终返回友好错误 |
| 工具失败 | timeout、结构化错误、有限重试；副作用工具依赖 idempotency key |
| Memory/向量库故障 | 跳过增强、写 durable retry job，不阻塞基本对话 |
| IM 出向失败 | delivery 状态机、429 retry_after、指数退避、死信和告警 |

降级按**组件和租户后端**判断，不能因为共享 PostgreSQL 或 Redis 单点异常就摘除所有租户。

## 21. 灰度和回滚

- stable/canary Worker；
- 租户 release epoch；
- 切换前停止旧 epoch 新入队；
- 等待旧队列 watermark 和在途 Turn 排空；
- 新 epoch 生效；旧 epoch 写入被拒绝；
- 配置采用 append-only revision；
- 在途 Turn 固定 revision；
- 回滚通过创建新 revision 前进，不篡改历史；
- 数据库变更遵守 expand-contract。

## 22. 容量评估

必须给出并通过压测校准：

- 每 Worker 并发 Session/Turn；
- 平均输入/输出 token；
- Redis QPS、内存、stream lag；
- SQL QPS、连接池和锁等待；
- IM callback 峰值及 ACK P99；
- Session P50/P95/P99 大小及序列化带宽；
- Artifact 和向量库调用延迟。

不能只用 `XPENDING` 代表全部 backlog，需统计未投递、pending 和 processing。

## 23. 部署方案

### 23.1 最小可运行

Docker Compose：

```text
Gateway + Channel Adapter
2 × Worker
Admin API/CLI
Redis
PostgreSQL
MinIO
OTel Collector + Jaeger + Prometheus
```

提供两个示例租户：

- Tenant A：Redis Session/Memory + S3 Artifact；
- Tenant B：SQL Session/Memory + S3 Artifact。

### 23.2 生产推荐

Kubernetes：

- Gateway Deployment + HPA；
- Worker stable/canary + KEDA；
- Admin Deployment；
- Channel Adapter 可独立部署；
- post-turn reaper、usage reconcile、audit partition CronJob；
- OTel agent/gateway；
- 外部 Redis、PostgreSQL、S3、Vector DB；
- NetworkPolicy、ExternalSecret、PDB、跨 AZ spread；
- liveness 只检查进程；readiness 按可服务组件判断，不要求所有可选后端同时健康。

## 24. Week 4 验收

- 杀任一 Worker 后，消息可接管且 stale Worker 不能提交；
- Redis/SQL 分别故障时，只影响绑定该后端或依赖该控制面的租户，不切 InMemory；
- IM、模型和工具故障均按状态机进入重试/降级/死信；
- stable→canary→rollback 演练不让同 Session 跨 epoch 并发；
- Tenant config revision 可 apply 和 rollback；
- Compose 一键启动两个不同后端租户；
- Kubernetes 清单通过 dry-run；
- 输出容量报告和运维 Runbook。

---

## 25. 需求追踪矩阵

| `issue.md` 要求 | 交付位置 |
|---|---|
| 租户七要素 | 完整设计 §2；本计划 Week 1/3 |
| 节点拓扑与路由、sticky session | 完整设计 §1/§3；Week 1/2 验证 |
| 多租户隔离 | Week 1 tenant-bound facade；Week 3 安全验收 |
| 多后端统一抽象 | Week 1 §5 |
| Session 并发与更新顺序 | Week 1 §6 |
| Memory 可见性 | Week 1 §6.4 |
| Redis→SQL、向量迁移 | Week 1 §7 |
| IM 幂等 | Week 1 §6.5、Week 2 |
| 后端一致性取舍 | Week 1 验收报告 |
| 最小数据模型 | Week 1 验收 |
| 至少两类 IM | Week 2 企业微信 + Telegram |
| 消息转换、绑定、验签、群聊、平台限制 | Week 2 |
| Filter 治理 | Week 3 §15 |
| 指标、Tracing、审计、密钥 | Week 3 §16-17 |
| 故障降级 | Week 4 §20 |
| 灰度与配置回滚 | Week 4 §21 |
| 容量评估 | Week 4 §22 |
| 最小与生产部署 | Week 4 §23 |

## 26. 四周之外不追加的范围

以下内容不属于 `issue.md` 的硬性验收，4 周内不主动追加：

- 第三、第四种 IM Adapter；
- 每种向量数据库和对象存储厂商适配；
- 多 Region 数据驻留；
- Kubernetes Operator；
- 完整租户计费产品；
- Admin Web UI；
- 自动化 GDPR 删除证明平台；
- 全量 WORM 审计基础设施；
- 全平台富媒体卡片模板库。

如果任一周无法通过验收，下一周首先关闭阻断项，并删除同等工作量的非硬性增强；不得删除 `issue.md` 追踪矩阵中的硬性要求。
