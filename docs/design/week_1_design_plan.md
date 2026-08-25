# Week 1 Design Plan：数据同步与多后端支持

> 依据：
> - [`multi_tenant_node_deployment.md`](./multi_tenant_node_deployment.md)
> - [`multi_tenant_node_deployment_4_week_mvp.md`](./multi_tenant_node_deployment_4_week_mvp.md)
> - [`issue.md`](./issue.md)
>
> 本文是 Week 1 的可执行设计与实现计划。Week 1 的目标不是一次性完成所有后端和迁移平台，而是冻结多租户数据面的语义，交付可运行的关键骨架，并用最小跨节点场景验证设计成立。

---

## 1. Week 1 目标

### 1.1 目标

在一周内完成以下闭环：

1. 租户可以独立选择 Session、Memory、Summary、Artifact、Knowledge 和 Audit 后端。
2. 通过统一的 `BackendFactory` 装配租户运行时，并通过 tenant-bound facade 阻止跨租户访问。
3. 冻结多节点 Session 的一致性协议：`inbound_seq`、Session lock、fencing epoch、revision CAS 和确定性 ID。
4. 明确并实现 `event → state → commit → post-turn` 的更新顺序。
5. 保证 Memory 从不可变原始 event range 写入，避免 Summary 压缩导致记忆遗漏。
6. 建立 IM 入向消息的持久幂等边界，避免“去重成功但入队失败”导致消息静默丢失。
7. 设计并演练 Redis → SQL Session、本地向量库 → 远端向量库两类迁移。
8. 建立最小数据模型、迁移机制和验收测试，为 Week 2 的 IM 接入提供稳定基础。

### 1.2 Week 1 成功标准

Week 1 结束时，必须能够在测试环境运行两个 Worker：

- Tenant A 使用 Redis Session/Memory；
- Tenant B 使用 SQL Session/Memory；
- 两个租户共用 Worker 进程，但数据、配置和 service facade 不可互相访问；
- 同一 Session 的并发消息不会丢 Event、覆盖 State 或产生重复 USER Event；
- 重放同一 `MessageEnvelope` 可以收敛，不得笼统宣称 exactly-once。

系统语义统一定义为：

| 数据/动作 | Week 1 保证 |
|---|---|
| 入向消息 | at-least-once；通过持久状态和确定性 ID 幂等收敛 |
| Session Event | 确定性 ID、幂等追加、顺序可验证 |
| Session State | revision/epoch CAS；拒绝 stale writer |
| 工具副作用 | 仅在支持幂等键时尽力收敛，不保证外部系统 exactly-once |
| Memory | 普通写入最终一致；支持 durable job 重试 |
| Summary | 异步、幂等、`covers_up_to_seq` 单调发布 |
| Outbound | 由 `(envelope_id, chunk_index)` 去重；平台不支持幂等时保留极小重复窗口 |

---

## 2. 范围与优先级

### 2.1 P0：本周必须完成

- ADR：一致性、幂等、Turn 状态机、租户隔离边界；
- `TenantConfig`、`BackendSpec`、`TenantScope` 和 `SecretRef` 的最小模型；
- `BackendFactory` 接口与 Redis/SQL/InMemory Session、Memory 装配；
- tenant-bound facade；
- Session 的 turn 语义和 `Runner` persisted-turn API 合同；
- Session revision、seq、fencing epoch 的数据结构与 CAS 规则；
- immutable event-range post-turn job；
- Redis Lua 入向 dedup + `XADD` 原子方案设计/原型；
- Redis Session 读改写缺陷的并发测试与修复方案；
- Alembic 或等价版本化迁移的接入；
- 最小表结构在真实 PostgreSQL 上创建和升级；
- Week 1 验收测试。

### 2.2 P1：本周完成原型或接口，不要求生产化

- Redis Session V2 的追加语义：Event 使用 append，State 使用字段级更新；
- SQL Session revision 乐观锁；
- SQL Inbox/Outbox 兜底方案；
- durable post-turn reaper 的任务模型和 lease；
- Redis → SQL 单租户迁移 CLI；
- 本地向量 → 一个远端向量库的 adapter 验证；
- `QueueABC`，首期实现 Redis Streams；
- `SCAn` 替换 `KEYS` 的 Redis 查询实现。

### 2.3 明确不在 Week 1 完成

- 企业微信、Telegram 的完整 Adapter；
- Gateway/Channel Adapter 的完整 HTTP 服务；
- 完整 Filter 治理、预算、脱敏和全链路 Trace；
- S3/COS/MinIO 全量 Artifact 生产实现；
- Kafka 实现；
- 通用五阶段迁移平台和多 Region 迁移；
- Admin Web UI、Kubernetes 生产清单；
- 所有向量数据库和外部 Memory Provider 的适配。

Week 1 只保留这些能力的接口扩展点，Week 2～4 按 MVP 计划继续实现。

---

## 3. 设计决策冻结

### 3.1 租户命名空间

SDK 现有 Session API 使用 `(app_name, user_id, session_id)` 寻址。Week 1 采用：

```text
app_name = tenant_id + "|" + app_id
```

约束：

- `tenant_id` 和 `app_id` 禁止包含 `|`；
- `TenantScope.parse()` 必须严格校验格式；
- `app_name` 只是命名空间，不是安全边界；
- 业务代码只能拿到固定 `TenantScope` 的 facade，不允许任意传入 `app_name`；
- Worker 消费 Envelope 后必须重新校验租户、应用和通道绑定。

### 3.2 后端选择

每个租户独立配置：

```python
class BackendSpec(BaseModel):
    type: Literal[
        "inmemory", "redis", "redis_cluster", "sql",
        "s3", "vector_local", "vector_remote",
        "mem0", "mempalace", "file", "otel_log",
    ]
    dsn_ref: SecretRef | None = None
    options: dict[str, Any] = Field(default_factory=dict)

class DataBackends(BaseModel):
    session: BackendSpec
    memory: BackendSpec
    summary: BackendSpec | None = None  # None 表示跟随 Session
    artifact: BackendSpec
    knowledge: BackendSpec | None = None
    audit: BackendSpec
```

Week 1 最小可运行矩阵：

| 租户 | Session | Memory | Summary | Artifact | 目的 |
|---|---|---|---|---|---|
| Tenant A | Redis | Redis | 跟随 Session | InMemory（测试） | 验证共享后端、多 Worker |
| Tenant B | SQL | SQL | SQL | InMemory（测试） | 验证不同租户后端绑定 |
| 开发模式 | InMemory | InMemory | InMemory | InMemory | 单进程单测 |

生产校验：

- 多 Worker 配置下禁止 InMemory Session/Memory/Artifact；
- Secret 只能通过 `SecretRef` 解析，不得放进 `options` 明文；
- `summary=None` 时必须明确使用 Session 对应的 Summary store；
- 外部 Memory 与向量库失败不得阻塞基本 Session 对话。

### 3.3 BackendFactory 与连接池

```python
class BackendFactory:
    def session_service(self, tenant: TenantConfig) -> SessionServiceABC: ...
    def memory_service(self, tenant: TenantConfig) -> MemoryServiceABC: ...
    def summary_store(self, tenant: TenantConfig) -> SummaryStoreABC: ...
    def artifact_service(self, tenant: TenantConfig) -> ArtifactServiceABC: ...
    def knowledge(self, tenant: TenantConfig) -> KnowledgeBase | None: ...
    def audit_sink(self, tenant: TenantConfig) -> AuditSinkABC: ...
```

实现规则：

1. 连接池按 `(backend_type, resolved_dsn_fingerprint, options_fingerprint)` 复用；
2. Service 外层包装 tenant-bound facade；
3. Facade 内部固定 `TenantScope`，对输入和输出的 Session/Artifact ID 做归属校验；
4. `Runner` 使用共享 service 时，必须设置 `close_*_service_on_close=False`；
5. 租户配置或 Secret 变化时，旧 Runtime 按 revision 失效，不原地修改共享模型实例；
6. 连接失败时默认 fail closed，并返回可分类的后端错误，不自动切换到 InMemory。

### 3.4 Session 一致性协议

Session 正确性依赖以下顺序，分区本身不提供串行保证：

```text
inbound_seq
  → session distributed lock
  → fencing epoch
  → next_expected_seq 校验
  → Session revision CAS
  → deterministic envelope/turn/event ID
```

单轮执行协议：

```text
acquire lock(epoch)
  → verify inbound_seq == next_expected_seq
  → begin_turn(envelope_id, revision, epoch)
  → append USER event
  → run persisted turn
  → append agent/tool events
  → apply state delta
  → commit revision/max_seq/next_expected_seq
  → create immutable post-turn jobs
  → release lock
  → memory ingest raw event range
  → summary generate and CAS publish
```

必须满足：

- Event 是事实，State 是投影；先 Event 后 State；
- stale epoch 不得提交 Session、执行不可逆工具或发布 Outbound；
- Session commit 必须同时校验 `revision` 和 `fence_epoch`；
- 重放使用稳定的 UUID5：
  - `turn_id = UUID5(namespace, envelope_id)`；
  - `invocation_id = UUID5(namespace, envelope_id)`；
  - `event_id = UUID5(namespace, envelope_id + logical_event_index)`；
- 工具副作用必须接收 `idempotency_key`，否则只能提供 at-least-once 语义。

### 3.5 Runner 接口选择

现有 `Runner.run_async()` 会自行读取/创建 Session、写入 USER Event，因此不能在 Worker 预写 USER Event 后直接调用，否则会重复写入。

Week 1 采用完整语义优先的方案 A，先冻结接口合同：

```python
turn = await session_service.begin_turn(
    envelope_id=envelope_id,
    expected_revision=expected_revision,
    epoch=epoch,
)

async for event in runner.run_persisted_turn(
    turn=turn,
    new_message=message,
    invocation_id=turn.invocation_id,
):
    ...

await session_service.commit_turn(turn)
```

实现策略：

- Week 1 先完成 `SessionTurn`、ABC 方法和 InMemory/SQL 原型；
- 对现有 `run_async()` 保持兼容，不改变默认单进程行为；
- Redis V2 的追加语义可先由 feature flag 开启；
- 若评审决定采用方案 B，必须在 ADR 中记录原因、兼容行为和 USER Event 所有权。

### 3.6 Post-turn 语义

Post-turn job 不保存可变 Session 快照，而保存不可变事件区间：

```python
PostTurnJob(
    tenant_id=tenant_id,
    app_name=app_name,
    user_id=user_id,
    session_id=session_id,
    from_seq=from_seq,
    to_seq=to_seq,
    session_revision=session_revision,
    kind="memory" or "summary",
)
```

顺序：

1. Memory 从原始 `[from_seq, to_seq]` 读取并写入；
2. Memory 成功后推进 `ingested_up_to_seq` / `memory_watermark`；
3. Summary 使用持久 lease，生成后以 `covers_up_to_seq` CAS 发布；
4. Summary 不得回退，也不得依赖被压缩后的 Event 窗口；
5. Worker 崩溃后由 reaper 接管未完成任务。

### 3.7 入向幂等边界

Week 1 首选 Redis Streams + Redis Lua：在同一 Redis slot 内原子完成：

```text
check dedup identity
  → allocate inbound_seq
  → mark RECEIVED/ENQUEUED
  → XADD envelope
```

SQL 兜底设计为：

```text
INSERT inbound_dedup + INSERT inbox/outbox
```

必须避免以下故障窗口：

```text
SET dedup=PROCESSING 成功
→ Gateway 崩溃
→ 尚未入队
→ 重试被误判为重复
→ 消息永久丢失
```

持久状态至少包括：

```text
RECEIVED → ENQUEUED → CLAIMED → SESSION_COMMITTED
         → OUTBOUND_COMMITTED → DONE
```

`PROCESSING` 不得只依靠 TTL，还必须带 owner、lease、heartbeat 和接管规则。

---

## 4. 目标模块与文件变更

### 4.1 新增租户与后端模块

```text
trpc_agent_sdk/tenancy/
├── __init__.py
├── _tenant_config.py       # TenantConfig、DataBackends、BackendSpec
├── _scope.py               # TenantScope
├── _secret_ref.py          # SecretRef、Resolver 接口
├── _registry.py            # TenantRegistry ABC，File 实现优先
├── _runtime.py             # TenantRuntime、RuntimeCache
└── backends/
    ├── __init__.py
    ├── _spec.py
    └── _factory.py
```

Week 1 只实现能支撑两个示例租户的最小字段；完整 Channel、Filter 和 Admin 字段在后续周补齐。

### 4.2 Session、Runner 与 post-turn

```text
trpc_agent_sdk/abc/_session_service.py   # begin_turn/commit_turn 合同
trpc_agent_sdk/sessions/_turn.py         # SessionTurn、TurnState
trpc_agent_sdk/sessions/_sql_session_service.py
trpc_agent_sdk/sessions/_redis_session_service_v2.py
trpc_agent_sdk/runners.py                # run_persisted_turn
trpc_agent_sdk/server/multitenant/worker/_post_turn_reaper.py
```

兼容要求：

- 现有 Session Service 实现和历史测试继续通过；
- 新字段采用兼容迁移，默认值不能破坏旧 Session；
- `append_mode`、`optimistic` 等新语义通过配置开启；
- 不在 Week 1 删除现有 InMemory/Redis/SQL 实现。

### 4.3 消息、幂等和并发控制

```text
trpc_agent_sdk/server/multitenant/
├── _envelope.py       # MessageEnvelope、确定性 ID
├── _queue.py          # QueueABC、Redis Streams 原型
├── _dedup.py          # dedup 状态与 Lua/SQL 接口
├── _lock.py           # lease、owner、fencing epoch
└── _turn.py           # Worker turn orchestration
```

### 4.4 数据库迁移

```text
migrations/
├── env.py
├── versions/
│   ├── xxxx_add_tenant_control_plane.py
│   ├── xxxx_add_session_turn_columns.py
│   └── xxxx_add_inbox_post_turn_tables.py
```

Week 1 最小新增对象：

- `tenants`；
- `agent_apps`；
- `channel_bindings`；
- `channel_inbound_dedup` 或 Inbox；
- `session_summaries`；
- `post_turn_pending`；
- 必要的 Session `revision`、`max_seq`、`fence_epoch`、`next_expected_seq` 和 Event `seq` 字段。

`audit_logs`、`usage_daily`、出向投递完整状态机可先完成模型和迁移接口，详细实现分别放到 Week 2/3。

---

## 5. 五个工作日计划

### Day 1：语义冻结与代码基线

**任务**

- 阅读并确认现有 `Runner`、Session、Memory、Redis Storage 和 Summary 实现；
- 编写 ADR，冻结：
  - at-least-once 边界；
  - Turn 状态机；
  - deterministic ID；
  - fencing epoch 与 revision；
  - event/state/summary 顺序；
  - post-turn 原始 event range；
  - tenant-bound facade；
- 梳理现有 498 个测试文件的兼容约束，建立 Week 1 测试清单；
- 确认 SQL 方言和迁移工具方案，优先选择 Alembic；
- 定义两个示例租户配置。

**输出**

- `docs/design/adr/week1-turn-and-consistency.md`；
- `docs/design/adr/week1-tenant-data-boundary.md`；
- ADR 评审结论；
- 两租户配置样例；
- 风险清单和测试矩阵。

**退出条件**

- 明确 `Runner` 谁负责 USER Event、`seq`、Turn 状态转换和恢复；
- 不存在“先预写 Event 再调用旧 `run_async()`”的未解决路径。

### Day 2：租户模型与 BackendFactory

**任务**

- 实现 `TenantScope`、最小 `TenantConfig`、`BackendSpec`、`SecretRef`；
- 实现 File TenantRegistry，供本地测试使用；
- 实现 BackendFactory 的接口、缓存 key 和生命周期管理；
- 接入 InMemory、Redis、SQL Session/Memory service；
- 为 Service 增加 tenant-bound facade；
- 增加多 Worker 使用 InMemory 时的配置校验；
- 验证租户 A/B 的后端装配结果和 service 连接池复用。

**输出**

- `trpc_agent_sdk/tenancy/` 最小实现；
- `tests/tenancy/test_scope.py`；
- `tests/tenancy/test_backend_factory.py`；
- Tenant A/B 集成 fixture。

**退出条件**

- facade 不接受任意 `app_name`；
- A/B 租户取得的 Session/Memory service 类型正确；
- 不记录 Secret 值、DSN 密码或完整 SecretRef 路径。

### Day 3：Turn、Session 并发与 Redis/SQL 原型

**任务**

- 增加 `SessionTurn` 和 Turn 状态机；
- 扩展 `SessionServiceABC` 的 `begin_turn`/`commit_turn` 合同；
- 实现 InMemory 参考版本；
- 为 SQL 增加 revision/seq/epoch 的 optimistic CAS 原型；
- 实现 Redis Session V2 的最小追加/CAS 路径，或完成 append-mode 设计并用锁保护旧路径；
- 修复/替换生产路径中的 `KEYS` 查询为 `SCAN`；
- 增加 `Runner.run_persisted_turn()` 骨架和事件所有权测试。

**输出**

- `tests/sessions/test_turn.py`；
- `tests/sessions/test_concurrent_commit.py`；
- Redis Session V2 原型；
- SQL migration upgrade test；
- Runner compatibility test。

**退出条件**

- 同一 Session 的并发提交至少能检测冲突，不能静默覆盖；
- stale epoch/revision 提交失败；
- 同一 Envelope 重试不会产生第二个 USER Event。

### Day 4：Post-turn、幂等与迁移原型

**任务**

- 实现 immutable event-range `PostTurnJob`；
- 实现 Memory-first、Summary-second 的 worker/reaper 接口；
- 增加 Memory watermark/ingestion ledger；
- 实现 Redis Lua dedup + `XADD` 原子原型；
- 完成 SQL Inbox/Outbox 的数据模型和恢复流程设计；
- 实现 Redis → SQL Session 的单租户迁移 CLI 最小流程：导出、导入、校验、回滚；
- 实现本地向量 → 一个远端向量库的 adapter 级迁移验证。

**输出**

- `tests/multitenant/test_dedup_atomicity.py`；
- `tests/multitenant/test_post_turn_recovery.py`；
- `tests/migrations/test_redis_to_sql.py`；
- `tests/migrations/test_vector_migration.py`；
- 迁移演练报告。

**退出条件**

- 在 dedup 后、入队前模拟崩溃，消息可恢复；
- Summary 压缩前后，Memory 都能拿到完整原始事件区间；
- 迁移保留 Event ID、顺序、State 和租户过滤条件。

### Day 5：端到端验证、审查与交付

**任务**

- 启动 Redis、PostgreSQL 和两个 Worker；
- 用 Tenant A/B 完成跨节点 Session/Memory 对话；
- 执行 100 条同 Session 并发消息测试；
- 执行 stale lock owner、Envelope replay、post-turn kill 测试；
- 执行租户隔离对抗测试；
- 执行真实 PostgreSQL 空库创建、升级、回滚检查；
- 汇总一致性、延迟、成本和运维复杂度对比；
- 评审未完成项，将非阻断项转入 Week 2 backlog。

**输出**

- Week 1 验收报告；
- 后端对比表；
- API/数据模型文档；
- 可运行的两租户示例；
- Week 2 的 IM Adapter 输入契约。

---

## 6. 最小接口设计

### 6.1 Envelope

```python
class MessageEnvelope(BaseModel):
    envelope_id: UUID
    tenant_id: str
    app_id: str
    channel_id: str
    external_msg_id: str
    user_id: str
    session_id: str
    inbound_seq: int
    payload: Content
    traceparent: str | None = None
    config_revision: int
```

约束：

- `envelope_id` 由 `(tenant_id, channel_id, external_msg_id)` 确定性生成；
- Worker 不信任 Envelope 中的租户字段，必须重新查绑定；
- 原始外部用户 ID 不直接作为 Session 主键；
- Week 2 才接入 tenant-scoped HMAC 的 IM 身份映射，但接口现在预留。

### 6.2 Lock/Fencing

```python
class SessionLease(Protocol):
    epoch: int
    owner: str
    expires_at: float

    async def acquire(self, session_key: str) -> SessionLease: ...
    async def renew(self, lease: SessionLease) -> bool: ...
    async def assert_valid(self, lease: SessionLease) -> None: ...
    async def release(self, lease: SessionLease) -> None: ...
```

规则：

- 使用单调递增 epoch，不使用 UUID 作为 fencing token；
- 续租失败立即取消当前 Invocation；
- Session commit、不可逆工具调用和 outbound commit 前重新校验；
- 拿不到锁时 park/延迟，不得简单重投队尾破坏顺序。

### 6.3 Post-turn Job

```python
class PostTurnJob(BaseModel):
    tenant_id: str
    app_name: str
    user_id: str
    session_id: str
    from_seq: int
    to_seq: int
    session_revision: int
    kind: Literal["memory", "summary"]
    attempts: int = 0
    next_try_at: datetime
```

Job 必须可重试、可接管、可观测；同一事件范围重复写入由 Event ID 或 Memory ledger 幂等收敛。

---

## 7. 测试与验收矩阵

### 7.1 租户与后端

- [ ] Tenant A 使用 Redis，Tenant B 使用 SQL；两者在两个 Worker 上均可读写；
- [ ] 租户 A 不能通过伪造 `app_name` 读取租户 B；
- [ ] 伪造 Artifact/Session 标识会被 facade 拒绝；
- [ ] Worker 配置多节点时，InMemory Session/Memory/Artifact 启动失败；
- [ ] BackendFactory 按 DSN 复用连接池，但不复用跨租户可变 Runtime、Filter 或 Model 实例；
- [ ] Secret 不出现在日志、异常、配置 dump 和 Trace 属性中。

### 7.2 Session 并发与顺序

- [ ] 同一 Session 并发 100 条消息，Event 数量正确；
- [ ] `seq` 单调且无重复；
- [ ] State 不被整对象旧快照覆盖；
- [ ] revision 冲突可检测并重试/park；
- [ ] stale epoch owner 无法提交 State、Event 或 Outbound；
- [ ] 同一 Envelope replay 不产生重复 USER Event；
- [ ] 现有 Session、Runner 测试全部通过。

### 7.3 Post-turn 与 Memory

- [ ] Memory 始终从原始 event range 读取；
- [ ] Summary 压缩 Session 后，本轮事件仍能进入 Memory；
- [ ] Memory 写入失败会进入 durable retry job；
- [ ] Worker 在 Memory/Summary 前后被 kill，重启后可补偿；
- [ ] Summary `covers_up_to_seq` 不回退、不重复发布旧结果；
- [ ] watermark 最终追平。

### 7.4 IM 幂等边界

- [ ] 同一外部消息重复 20 次只生成一个有效 Envelope；
- [ ] dedup 后、入队前崩溃不会丢消息；
- [ ] 入队后、ACK 前崩溃不会造成重复模型执行；
- [ ] Queue 重投可由 Turn 状态机恢复；
- [ ] Outbound 幂等键格式固定为 `envelope_id:chunk_index`。

### 7.5 迁移

- [ ] Redis → SQL 保留 Event ID、seq、State、更新时间；
- [ ] Redis TTL 过期语义得到处理；
- [ ] 迁移失败可从断点继续；
- [ ] shadow compare 能报告 mismatch；
- [ ] 本地向量 → 远端向量完成 count、tenant filter、抽样 recall@k 校验；
- [ ] 未通过校验时不允许 cutover。

---

## 8. 后端一致性对比输出

Week 1 验收报告必须包含以下表格，并用测试数据补充 P50/P95/P99：

| 后端 | 一致性 | 读写延迟 | 多节点能力 | 成本 | Week 1 结论 |
|---|---|---:|---|---|---|
| InMemory | 单进程强一致 | 最低 | 不支持 | 最低 | 仅开发/单测 |
| Redis | 单 Key 强一致，故障转移可能丢最近写入 | 低 | 支持 | 中 | 推荐热 Session/Memory |
| SQL | 事务强一致、唯一约束、CAS | 中 | 支持 | 中 | 推荐控制面和严格数据 |
| 本地向量 | 本地可控，跨节点依赖部署 | 低～中 | 受限 | 低 | 开发/小规模 |
| 远端向量 | 通常最终一致 | 中～高 | 支持 | 高 | 生产 Knowledge |
| 外部 Memory | 最终一致、延迟不可控 | 高 | 支持 | 按调用 | 异步增强，不能阻塞主链路 |

必须记录：

- Session/Memory 单次读写延迟；
- Redis QPS、内存和 Stream lag；
- SQL 连接数、QPS 和锁等待；
- 迁移吞吐、失败重试和校验耗时；
- 连接池在租户数增加时的容量影响。

---

## 9. 风险与应对

| 风险 | 等级 | 应对 |
|---|---|---|
| 修改 Runner 破坏现有单进程行为 | 高 | 新增 persisted-turn API，保留旧 `run_async()`，默认关闭新路径 |
| Redis 读改写丢更新 | 高 | Session lock + fencing；V2 采用追加/CAS；并发测试作为门禁 |
| 分区被误认为严格串行 | 高 | 文档和代码明确：分区只降低争抢，顺序由 seq/lock/epoch 保证 |
| 去重和入队非原子造成丢消息 | 极高 | Redis Lua 或 SQL Inbox/Outbox；禁止两步 SET + publish |
| stale Worker 继续写入 | 极高 | 单调 fencing epoch，所有可变提交带 epoch |
| Summary 压缩导致 Memory 漏写 | 高 | PostTurnJob 固定 event range，Memory 先于 Summary |
| 租户 service/model 实例共享造成串租户 | 极高 | facade 固定 scope；Runtime/Model/Filter 按租户实例化 |
| Alembic 引入影响现有 SQL schema | 中 | 先在临时 PG 执行空库创建、旧库升级和回滚，再合并迁移 |
| 一周内范围过宽 | 高 | P0 先冻结语义和骨架；P1 只交付关键原型；其余明确移入后续周 |

---

## 10. Week 1 Definition of Done

只有同时满足以下条件，Week 1 才算完成：

1. ADR 通过评审，并明确现有 `Runner` 与 persisted-turn 语义的冲突处理；
2. 两个租户可分别装配 Redis 和 SQL Session/Memory；
3. tenant-bound facade 和 Worker 侧绑定重校验通过跨租户对抗测试；
4. Session 并发、revision、fencing、deterministic ID 测试通过；
5. Event → State → Commit → Post-turn 顺序测试通过；
6. Memory 从原始事件区间写入，Summary 不会造成遗漏；
7. dedup 与入队具有原子或持久恢复边界；
8. Redis → SQL 和本地向量 → 远端向量至少各完成一次测试演练；
9. PostgreSQL 迁移在空库、旧库升级和回滚场景可执行；
10. 现有测试集无回归，未完成项已登记到 Week 2 backlog。

---

## 11. Week 2 输入

Week 1 必须向 Week 2 输出以下稳定契约：

- `TenantScope`、`TenantRuntime` 和 BackendFactory 的创建接口；
- `MessageEnvelope` 的字段和确定性 ID 规则；
- Queue、Dedup、SessionLease、Turn 的接口；
- 单聊/群聊 Session 生成所需的 `channel_id`、外部用户标识和 metadata 字段；
- Outbound 幂等键和 post-turn job 的生命周期；
- Redis/SQL 故障时的错误分类和 fail-closed 语义。

Week 2 在此基础上实现企业微信和 Telegram Adapter，不再改变 Week 1 已冻结的 Session、幂等和租户隔离语义。
