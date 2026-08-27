# 多租户与节点部署设计——对抗性评审

> 评审对象：[`multi_tenant_node_deployment.md`](./multi_tenant_node_deployment.md)
>
> 评审方式：从进程崩溃、消息重放、并发竞争、节点漂移、恶意租户、后端故障和配置错误等场景出发，验证设计中关于无丢失、有序性、幂等性、多租户隔离和可运维性的结论。

## 结论

**建议在进入实现前进行重大修改（Request Changes）。**

原设计覆盖面完整，也正确识别了当前 Redis Session 读—改—写导致的丢更新问题；但若按当前方案直接实施，以下核心保证仍不成立：

- Gateway 接收的消息可能静默丢失；
- 同一 Session 的消息不一定严格串行；
- 过期锁持有者仍可能写入陈旧数据；
- 重放可能重复调用模型、执行工具和发送 IM 回复；
- `per_group` 实际不会形成共享群会话；
- `app_name = tenant|app` 只是命名空间约定，不是安全边界；
- §5.2 的更新顺序与当前 `Runner` 接口不兼容；
- 给出的 PostgreSQL DDL 有多处无法执行或无法提供预期约束的问题。

在继续设计 TenantRuntime、Channel Adapter 和治理 Filter 前，应先定义一个明确的、可恢复的 **at-least-once Turn 状态机**，并补齐持久 Inbox/Outbox、确定性 ID、真正的 fencing、租户绑定的数据访问接口和跨节点取消机制。

---

## 一、阻断性问题

### 1. Gateway 去重与入队之间存在消息永久丢失窗口

**相关章节：** §1 关键协作时序、§5.5。

当前流程是：

1. `SET NX dedup=PROCESSING`；
2. 发布消息到队列；
3. 向 IM 平台返回 ACK。

步骤 1 和步骤 2 之间没有原子边界。若 Gateway 在写入 `PROCESSING` 后、发布队列前崩溃：

- IM 平台会重试；
- 重试请求发现状态为 `PROCESSING`；
- Gateway 返回空 ACK；
- 实际上队列中从未出现该消息；
- 消息会一直丢失，直至去重键过期，而且过期后也未必再次收到平台重投。

反过来，若先入队后标记去重，则崩溃会造成重复入队。

#### 必须修改

使用以下任一方案建立持久且可恢复的边界：

1. **SQL Inbox + 事务 Outbox**：在同一数据库事务中记录入向消息及待发布事件，由 Relay 投递队列；
2. **Redis Lua 原子操作**：前提是 dedup key 与 Redis Stream 位于可原子执行的同一 Redis/slot，在同一个 Lua 脚本中完成去重记录和 `XADD`；
3. 使用支持事务生产者的消息队列，同时保留持久 Inbox 和幂等消费者。

建议显式定义状态机：

```text
RECEIVED
  -> ENQUEUED
  -> CLAIMED
  -> SESSION_COMMITTED
  -> OUTBOUND_COMMITTED
  -> DONE
```

`PROCESSING` 不能只依赖 24 小时 TTL；它必须有租约、owner、最后心跳和可接管规则。

---

### 2. Redis Streams consumer group 不保证同一 Session 严格串行

**相关章节：** §3.1、§3.2、§5.1 L1。

原设计认为：将 Session 哈希到固定 Stream/分区，再由 consumer group 消费，就能保证同一 Session 严格串行。该结论对 Redis Streams 不成立：

- 同一个 Stream 中的不同消息可以同时分发给多个 consumer；
- 消息分发顺序不等于处理完成顺序；
- 慢 consumer 尚未完成时，`XAUTOCLAIM` 可能把消息交给另一节点；
- 锁竞争失败后把旧消息重新放到队尾，会让后续消息越过旧消息；
- 即使使用 Kafka，如果 consumer 在应用内并发处理同一 partition 的消息，也仍会破坏完成顺序。

#### 必须修改

必须定义真正的顺序协议，而不是只定义哈希函数：

- 一个 partition 同一时刻只能有一个有效 owner；
- 每个 partition 一次只允许一个在途消息，或者由 Worker 内部维护按 Session 排序的调度器；
- Session 消息必须带单调 ingress sequence；
- 只有序号为 `next_expected_seq` 的消息可以进入执行；
- 锁竞争失败时不能简单重投队尾，应保留原顺序或暂停该 Session；
- ACK/offset commit 必须在对应顺序位置完成后推进。

在上述机制建立前，分布式 Session 锁是主要正确性机制，而不是“L2 兜底”。

---

### 3. 文档中的 UUID 锁 token 不是真正的 fencing token

**相关章节：** §5.1 L2。

原方案：

```python
token = uuid4().hex
SET lock_key token NX PX lease_ms
```

这只是“锁所有权 token”，不是 fencing token。以下场景仍会产生陈旧写：

1. Worker A 获得锁；
2. A 长时间暂停，锁租约过期；
3. Worker B 获得新锁并完成写入；
4. A 恢复执行并继续提交旧状态。

释放锁时比较 UUID 只能阻止 A 删除 B 的锁，不能阻止 A 在步骤 4 写 Session、调用外部工具或发出 IM 回复。

#### 必须修改

- 使用单调递增 epoch，例如 `INCR session_lock_epoch`；
- 每次获得锁返回新的 fencing epoch；
- 所有可变存储写入都携带 epoch/revision；
- Redis Lua 和 SQL `UPDATE` 必须拒绝小于当前 epoch 的写入；
- 续租失败后应立即取消当前 Invocation，不能让它继续运行；
- 在不可逆工具调用、Session commit 和 outbound commit 前重新校验 owner/epoch；
- 事件写入同时使用确定性 ID，以便拒绝重复提交。

仅实现“比较 token 后 DEL”不足以声称存在 fencing。

---

### 4. §5.2 的事件更新顺序无法通过当前 `Runner` 实现

**相关章节：** §5.2。

原设计要求 Worker：

1. 获取锁；
2. 读取 Session；
3. 分配 seq；
4. 先持久化 USER event；
5. 再调用 `Runner.run_async(...)`。

但当前 `Runner.run_async()` 会自行：

- 调用 `get_session`/`create_session`；
- 增加 `conversation_count`；
- 创建 USER event；
- 调用 `session_service.append_event()` 保存 USER event。

如果 Worker 预先写入 USER event，再正常调用 `Runner.run_async()`，用户输入会重复；如果不预写，Worker 又无法实现文档描述的 Turn 事务和 seq 分配。

因此，“唯一必须修改核心的是 Redis Session 和 Artifact”这一判断不成立。要实现文档中的语义，至少还需修改：

- `Runner` Turn 入口；
- `SessionServiceABC`；
- SQL Session 写入协议；
- post-turn 调度；
- Invocation/Event ID 和 revision 传递方式。

#### 必须修改

引入显式 Turn API，例如：

```python
turn = await session_service.begin_turn(
    envelope_id=envelope_id,
    expected_revision=expected_revision,
)

async for event in runner.run_persisted_turn(
    turn=turn,
    new_message=message,
):
    ...

await session_service.commit_turn(turn)
```

或者为 `Runner` 增加“用户事件已经持久化”的执行入口，确保它不会再次 append USER event。

设计必须明确以下内容的职责归属：

- 谁创建 Invocation ID；
- 谁分配事件 seq；
- 谁决定 Turn 已开始/已提交；
- Worker 崩溃后谁恢复；
- `Runner` 是否允许同一 Envelope 重新执行。

---

### 5. 存储层事件主键不能让整个 Runner 重放变成幂等

**相关章节：** §5.5。

原设计认为 `events` 表的事件 ID 主键可以阻挡重复执行。但当前 `Runner` 重跑时会生成新的 Invocation ID 和 Event ID，因此同一 `envelope_id` 重放会被视为全新的事件。

即使 Session event 去重成功，也无法自动消除以下副作用：

- 重复模型调用和重复计费；
- 重复执行外部工具；
- 重复创建 Artifact 版本；
- 重复发送 IM 消息；
- 重复写第三方 Memory 服务。

`processed:{envelope_id}` 也存在经典问题：

- 执行前标记：崩溃可能导致消息永远不执行；
- 执行后标记：崩溃可能导致副作用重复执行。

#### 必须修改

为一次外部消息建立稳定身份：

```text
invocation_id = UUID5(namespace, envelope_id)
event_id      = UUID5(namespace, envelope_id + logical_event_index)
turn_id       = UUID5(namespace, envelope_id)
tool_call_id  = 持久化后生成，或由稳定输入确定
```

同时要求：

- 副作用工具支持 `idempotency_key`，并记录 tool execution ledger；
- Artifact 以稳定操作 ID 或 `(artifact_id, content_hash, operation_id)` 去重；
- outbound 使用稳定 chunk ID；
- 明确系统只能提供 **at-least-once + 幂等收敛**，不能笼统宣称 exactly-once；
- 对不支持幂等键的 IM 平台，应明确“平台发送成功但本地状态更新失败”仍可能导致重复发送。

---

### 6. `per_group` 策略实际上不会共享群会话

**相关章节：** §3.1、§7.4。

SDK 的 Session 主键是：

```text
(app_name, user_id, session_id)
```

原设计在 `per_group` 模式下只让群成员使用相同的 `session_id`，但每个发送者仍映射到不同的 `user_id`。因此：

```text
(tenant|app, user-A, group-session)
(tenant|app, user-B, group-session)
```

是两个不同 Session，无法实现“群内所有人共享上下文”。

#### 必须修改

共享群 Session 应使用合成存储主体：

```text
user_id    = group:<group_hmac>
session_id = channel:<channel>/group:<group_hmac>
```

真实发言人信息只能通过可信 metadata/event metadata 传入：

```text
speaker_user_id
speaker_external_user_hmac
speaker_role
```

此外必须保证：

- 不能以合成 group user 加载个人长期记忆；
- 群消息中的用户私密查询不得写入其他成员可见的共享历史；
- 群 Session 授权需要基于当前发送者，而不是合成 `user_id`；
- 成员退出群后应处理其历史身份数据和后续访问权限。

---

### 7. `app_name = tenant|app` 是命名空间，不是安全隔离边界

**相关章节：** §2.2、§4.2。

原设计称 SQL 中以 `app_name` 为主键前缀后，跨租户查询“不可能手滑”。该结论过强。

任何拥有共享 Service 句柄的错误代码或被攻陷代码都可以主动传入另一个租户的 `app_name`。共享数据库凭据也通常能访问所有行。类似绕过包括：

- 构造其他租户的 `ArtifactId.app_name`；
- Admin API 漏掉 tenant predicate；
- Knowledge adapter 接受客户端覆盖过滤条件；
- 工具直接执行原始 SQL；
- 任意 `agent_factory` 导入并执行租户配置指定的 Python 代码；
- 租户之间共享带可变状态的 Filter/Model 实例。

#### 必须修改

至少增加以下防线：

1. 对外暴露 tenant-bound service facade，不允许调用方传任意 `app_name`；
2. facade 内固定 `TenantScope`，并校验传入/返回的 `Session`、`ArtifactId`；
3. 强隔离档使用显式 `tenant_id` 列和 PostgreSQL RLS；
4. 高合规租户使用独立 DB role/schema、bucket、KMS key 和向量 collection；
5. Knowledge tenant filter 必须在服务端 adapter 内强制注入且不可移除；
6. `agent_factory` 必须来自管理员 allowlist，不能允许普通租户指定任意 import path；
7. 队列 Envelope 在 Worker 侧必须重新校验 tenant/app/channel 绑定，不能完全信任 Gateway 输入。

设计应定义“共享逻辑隔离、RLS 隔离、物理隔离”三个档位，而不是把字符串前缀描述为硬安全边界。

---

### 8. §6 的 PostgreSQL DDL 无法按原样执行

**相关章节：** §6。

存在以下具体问题：

1. PostgreSQL 不支持表定义中的内联：

   ```sql
   INDEX idx_cub_user (tenant_id, user_id)
   ```

   应使用独立 `CREATE INDEX`。

2. PostgreSQL 主键不能直接包含表达式：

   ```sql
   PRIMARY KEY (app_name, user_id, COALESCE(session_id,''), filename, version)
   ```

   可改用生成列、`NULLS NOT DISTINCT` 唯一约束，或将 `session_id` 规范化为非空哨兵值。

3. `channel_inbound_dedup` 按 `received_at` 分区，但主键不包含分区键。PostgreSQL 要求分区表的唯一/主键约束包含全部分区键。

4. 只创建父分区表，没有创建日分区或 default partition；插入可能直接失败。

5. 文本称 outbound 唯一键包含 `chat_id`，DDL 的主键却不包含 `chat_id`，规范不一致。

6. 文本称“新增表全部带 `tenant_id`”，但以下表没有：

   - `session_summaries`；
   - `post_turn_pending`；
   - `artifacts`。

7. `TenantConfig` 注释称 tenant ID 最长 32 字符，DDL 却允许 64 字符。

8. `events.seq` 可为 NULL；唯一索引允许多行 NULL，不能对旧数据形成完整顺序约束。

9. `UNIQUE(kind, platform_key)` 对 NULL 不会形成单一空值约束，需明确是否符合预期。

10. `channel_user_bindings` 等表缺少必要的复合外键，容易保留悬挂绑定。

#### 必须修改

- 将 DDL 变为实际 Alembic migration；
- 在真实 PostgreSQL 上执行 migration 测试；
- 测试空库创建、旧库升级、回滚、并发索引、分区轮转；
- 在文档中区分 PostgreSQL、MySQL 和 SQLite 支持范围；
- 不再把伪 SQL 作为“可部署最小表结构”。

---

### 9. Summary 与 Memory 的执行顺序和数据语义存在冲突

**相关章节：** §5.2、§5.3。

当前 `Runner._run_post_turn_processing()` 的顺序是：

1. `create_session_summary(session)`；
2. `memory_service.store_session(session)`。

当前 `SummarizerSessionManager` 会压缩并修改 `session.events`，随后调用 `update_session()`。因此 Memory 可能收到的是已经压缩后的事件窗口，而不是本轮完整原始事件，导致长期记忆漏写。

此外：

- 当前 Summary metadata 只存在 `SummarizerSessionManager._summarizer_cache` 进程内缓存；
- 文档新增的 `session_summaries` 没有说明如何接入 `get_session()`；
- `covers_up_to_seq` CAS 只能防止旧摘要发布，不能阻止两个节点重复进行昂贵的 LLM 摘要调用；
- 摘要过程会重写 Session event window，单独对 summary 表 CAS 不能保护该重写；
- 当前 `MemoryServiceABC.search_memory()` 返回值不包含最大 seq，无法实现文档中的 watermark 比较；
- `store_session()` 接收整个可变 Session 快照，不适合作为可靠 outbox payload。

#### 必须修改

post-turn job 应记录不可变事件范围：

```text
PostTurnJob(
    tenant_id,
    app_name,
    user_id,
    session_id,
    from_seq,
    to_seq,
    session_revision,
    kinds=[memory, summary],
)
```

处理原则：

- Memory 从原始 event range 写入，不能依赖已压缩 Session 快照；
- Summary 使用持久 lease 避免重复生成，再用 CAS 发布；
- Summary 发布和 Session active window 更新必须属于同一个版本协议；
- `get_session()` 必须明确如何组合 summary anchor、近期事件和 historical events；
- Memory watermark 需要扩展 Memory 接口或增加独立 ingestion ledger。

---

### 10. 多个关键运行时状态仍是进程内状态

原设计只部分识别了本地状态问题，仍遗漏以下关键路径：

#### 10.1 Cancel 是进程内的

当前 cancel manager 在进程内维护 active run。若 `/stop` 命令由另一 Worker 处理，它无法取消正在执行的节点。

更严重的是，如果 `/stop` 与普通消息进入同一个严格串行 Session 队列，它可能必须等当前长任务结束后才能执行，失去取消意义。

**要求：** 增加分布式 cancellation/control channel；取消消息应走高优先级旁路，并由执行节点订阅。

#### 10.2 WeCom 流状态是进程内的

当前 OpenClaw WeCom channel 使用 `_active_stream_ids` 和 frame 映射。节点切换后，另一实例未必能继续 edit/finish 原流。

**要求：** 持久化 stream/edit correlation，或明确同一次 outbound stream 的适配器亲和性和故障降级策略。

#### 10.3 Filter 注册表实际缓存的是实例

当前 `FilterRegistry` 注册时会创建并缓存 Filter 实例，`get_filter()` 返回共享实例。原设计称注册表“只放类/工厂”，与当前实现不符。

若租户 Filter 内保存预算、规则、缓存或临时状态，会形成跨租户污染和数据竞争。

**要求：** 注册表保存 factory/class，TenantRuntime 每次构造租户级 Filter 实例；或者强制所有注册 Filter 不可变且无状态。

#### 10.4 Model 对象有可变密钥和 URL

`LLMModel` 包含可变 `_api_key`、`_base_url` 和 setter。若错误地跨租户复用模型实例，可能把 A 租户密钥用于 B 租户请求。

**要求：** 模型实例不得跨租户共享；动态密钥只能作为请求级不可记录 Secret 注入，不能通过共享对象 setter 切换。

---

## 二、高风险问题

### 11. 后端故障时降级到 InMemory 会造成 split-brain

**相关章节：** §4.1、§9.1。

当 Redis/SQL 主 Session 后端故障时切换到每个 Worker 的 InMemory Session，会导致：

- 同一 Session 在不同节点形成不同历史；
- 恢复后无法可靠合并事件和 state；
- 锁后端同时不可用时更无法判断写入顺序；
- 用户可能基于空历史执行高风险工具；
- 本地 outbox 与只读根文件系统部署要求相冲突。

#### 建议

生产默认应 **fail closed + 队列等待**：

- 不接受新的状态变更执行；
- 保留消息在 durable queue；
- 向用户发送明确的暂时不可用提示；
- 只有无状态、只读且明确配置允许的请求才可降级；
- 不把 InMemory 作为生产 Session 自动 fallback。

---

### 12. Gateway 的“快速 ACK”要求与职责冲突

**相关章节：** §3.3、§7.2。

文档要求 Gateway P99 ACK < 1s 且不做 DB 重操作，但 Gateway 又可能需要：

- 查询 SQL 获取 route binding；
- Redis 不可用时走 SQL dedup；
- 查询或创建 user binding；
- 下载 IM 附件；
- 上传 S3 Artifact；
- 构造完整 Content。

尤其“先下载附件到 S3 再入队”与快速 ACK 目标直接冲突。

#### 建议

Gateway 快路径只做：

1. 解析 route candidate；
2. 从本地/Redis 缓存读取 binding；
3. 验签；
4. 持久化原始消息或平台媒体引用；
5. 原子入 Inbox/队列；
6. ACK。

附件下载、病毒扫描、S3 上传和 Content 转换应在异步 media ingestion 阶段完成。

另外，“先验签再解析 tenant”在逻辑上不准确：验签需要先根据 route candidate 找到 binding secret。准确流程应是：

```text
解析不可信路由候选
  -> 查询候选 binding/secret
  -> 验签
  -> 验签成功后信任 tenant/channel 归属
```

---

### 13. SHA-1 和无密钥 hash 不足以保护身份隐私

**相关章节：** §3.1、§7.3。

问题包括：

- `sha1(external_user_id)[:16]` 只有 64 bit，规模增大后存在碰撞风险；
- 外部 ID 往往低熵、格式固定，可被字典枚举；
- 不同环境使用相同 hash，会产生可关联标识；
- 无法安全轮换。

#### 建议

使用 tenant-scoped HMAC-SHA-256：

```text
HMAC(tenant_identity_key, channel_kind || channel_id || external_id)
```

保留至少 128 bit 输出，并设计 key version 和轮换映射。Session key 中只使用 HMAC 标识，原始 external ID 仅在确有业务必要时加密保存。

---

### 14. 审计可靠性与“异步内存 ring buffer”冲突

**相关章节：** §8.4。

如果审计记录先进入进程内 ring buffer，节点崩溃会丢失记录。对“强合规”租户，这不能满足不可抵赖或完整审计要求。

此外，`sha256(payload)` 不能证明内容未被篡改，攻击者修改内容后可以重新计算 digest。对低熵内容，无密钥 digest 还可被枚举。

#### 建议

- 标准生产：业务事务内写 durable audit outbox，再异步投递审计库；
- 强合规：Kafka/WORM 对象存储、不可变保留策略、签名 hash chain；
- payload digest 使用 HMAC 或数字签名；
- 记录 key version；
- 明确审计后端不可用时是 fail-open 还是 fail-closed，并按租户合规档位配置。

---

### 15. BudgetFilter 的“先读后写”会在并发下穿透预算

**相关章节：** §8.1。

流程“调用前读取累计值，调用后 HINCRBY”允许多个并发请求同时通过检查，最终大幅超出预算。

#### 建议

- 模型调用前原子预留最大估算 token/cost；
- 调用完成后按实际 usage 结算并释放差额；
- 失败和超时也必须回收 reservation；
- QPS 和并发 Session 限制采用 Redis Lua/原子计数；
- 月预算不能只依赖日 Redis counter，应有持久账本和对账机制；
- 货币计算不要用 `float`，应使用 Decimal 或整数微美元。

流式脱敏也需要跨 chunk 缓冲；密钥若被拆成两个 chunk，逐 chunk 正则会漏检。

---

### 16. 后端迁移 cutover 缺少真正的屏障协议

**相关章节：** §5.4。

“Gateway 冻结租户写入几秒”不能保证安全切换，因为：

- 队列中已经有旧消息；
- Worker 中有在途 Invocation；
- Cron/Admin/A2A 等入口可能绕过 Gateway；
- post-turn job 仍可能写旧后端；
- 配置缓存失效不是同时发生的。

#### 建议

迁移必须引入：

- tenant migration epoch；
- 所有入口统一检查的 write barrier；
- 入队 watermark；
- Worker drain watermark；
- 在途 Turn 数归零确认；
- post-turn/outbox drain；
- source/target 双写序号对账；
- 原子切换生效 epoch；
- 旧 epoch Worker 写入拒绝。

仅靠 Pub/Sub 缓存失效和短暂停止消费无法证明无交叉写。

---

### 17. stable/canary 切换可能破坏 Session 顺序

**相关章节：** §9.2。

租户从 stable stream 切到 canary stream 时：

- 旧消息可能仍在 stable queue；
- 新消息已经进入 canary queue；
- 两组 Worker 可能同时处理同一 Session。

配置回滚时同样存在反向问题。

#### 建议

代码灰度切换必须与 Session ordering epoch 绑定：

1. 停止该租户新消息进入旧队列；
2. 记录切换 watermark；
3. 等旧队列和在途 Turn 排空；
4. 更新 release epoch；
5. 新队列仅接受新 epoch 消息；
6. Worker 拒绝不属于自身 epoch 的执行。

或者不要按租户切换物理队列，而是在同一有序队列中由兼容 Worker 根据固定 epoch 选择代码版本。

---

### 18. 容量估算忽略当前 Redis Session 的全量 JSON 成本

**相关章节：** §9.3。

当前 Redis Session 实现每次 append 都会：

- `GET` 整个 Session JSON；
- 反序列化；
- 替换整个 events/historical events；
- 重新序列化；
- `SET` 整个 Session JSON。

因此容量不能只按“每轮 25～40 Redis ops”估算，还要考虑：

- Session 全量网络带宽；
- JSON 编解码 CPU；
- 大 Session 的 event loop 阻塞；
- Redis value 重写和内存复制；
- `list_sessions()` 当前使用 `KEYS`，大规模生产会阻塞 Redis；
- `XPENDING` 只代表 pending entries，不代表全部 backlog；
- post-turn、工具容器、模型连接池和附件处理的内存未计入。

#### 建议

压测必须基于真实事件大小和 Session 分布，至少测量：

- P50/P95/P99 Session JSON 大小；
- append bytes/sec；
- 编解码 CPU；
- Redis command latency；
- queue 未投递 + pending + processing 总 lag；
- provider 并发连接限制；
- 工具和代码执行的峰值内存。

---

## 三、其他设计不一致与缺口

### 19. SecretRef 的 `repr` 仍可能泄露敏感路径

原示例返回：

```python
SecretRef(vault:vault://kv/tenants/acme/openai#api_key)
```

虽然不包含 Secret value，但会泄露租户名、系统结构、密钥路径和用途。建议日志中只显示 provider、逻辑名和不可逆 ref fingerprint，不输出完整路径。

`extra_headers` 和 `BackendSpec.options` 也可能直接包含 Secret，不能仅依赖字段命名约定。

### 20. 任意 `agent_factory` 是远程代码执行能力

`"my_pkg.agents:build_agent"` 若由租户配置直接控制，等价于允许加载任意可导入 Python 代码。必须由平台管理员维护模板/工厂 allowlist，租户只能选择已批准 ID。

### 21. `readonly` 状态语义不完整

“readonly → 只读工具”仍可能发生：

- Session event 写入；
- usage/audit 写入；
- Memory 写入；
- Summary 更新；
- outbound 投递。

应明确 readonly 是“禁止业务副作用工具”还是“数据完全不可写”。若是租户欠费/冻结，通常仍需写审计和系统状态，但不能执行外部副作用。

### 22. 数据删除权与保留策略未形成完整流程

开放问题中提到 GDPR，但核心模型缺少：

- tenant/user deletion job；
- 各后端删除状态和证明；
- S3 version/delete marker 清理；
- 向量库删除；
- Memory 外部服务删除；
- audit legal hold 例外；
- backup 中的延迟删除策略。

### 23. Admin API 安全设计不足

文档只简要提到 OIDC/mTLS 和二人复核，还应明确：

- RBAC/ABAC；
- 操作者的 tenant scope；
- 防止 confused deputy；
- revision 乐观锁；
- CSRF（若有浏览器界面）；
- SecretRef 可用范围；
- 后端连通性 dry-run 是否可能 SSRF；
- 审批人不能与提交人相同；
- 紧急 break-glass 流程。

### 24. Readiness 同时探测 Redis 和 SQL 可能放大故障

如果任一共享依赖短暂失败就让所有 Gateway Pod readiness 失败，负载均衡会一次性摘除全部实例。应区分：

- liveness：仅进程是否健康；
- readiness：是否能安全接受请求；
- dependency health：单独指标和熔断；
- degraded readiness：某通道/租户不可用不代表整个 Pod 不可用。

---

## 四、建议重构后的正确性模型

### 1. 明确系统保证

建议在文档开头明确：

- 入向队列：at-least-once；
- Session event：确定性 ID + 幂等追加；
- Session state：revision/epoch CAS；
- 工具副作用：尽力通过 idempotency key 收敛，不保证所有外部系统 exactly-once；
- outbound：at-least-once，平台不支持幂等时存在极小重复窗口；
- Memory/Knowledge：最终一致；
- Summary：异步、可重建、单调发布。

### 2. 建议的 Turn 状态机

```text
INBOX_RECEIVED
    |
    v
QUEUED
    |
    v
TURN_CLAIMED(epoch, owner, lease)
    |
    v
USER_EVENT_COMMITTED(turn_id, seq)
    |
    v
RUNNING
    |
    +--> TOOL_LEDGER_PREPARED --> TOOL_EXECUTED
    |
    v
SESSION_COMMITTED(revision, max_seq)
    |
    +--> POST_TURN_OUTBOX
    |
    v
OUTBOUND_OUTBOX
    |
    v
DONE
```

所有状态转换需要：

- expected previous state；
- turn/envelope deterministic ID；
- fencing epoch；
- durable timestamp；
- crash recovery owner；
- retry policy。

### 3. 建议的租户隔离层次

| 隔离档位 | Session/SQL | Artifact | Knowledge | Worker | 适用场景 |
|---|---|---|---|---|---|
| 逻辑隔离 | tenant-bound facade + tenant 列 | 共享 bucket 前缀 | 共享 collection 强制 filter | 共享 | 普通租户 |
| 数据库隔离 | PostgreSQL RLS + 独立 DB role | 独立 prefix/KMS | collection per tenant | 共享 | 企业租户 |
| 物理隔离 | 独立 DB/Redis | 独立 bucket/CMK | 独立实例 | 独立 Worker pool | 强监管租户 |

---

## 五、进入实现前的强制验收门禁

### 1. Ingress 崩溃矩阵

在以下每个位置注入进程崩溃：

- dedup 前；
- dedup 后、入队前；
- 入队后、ACK 前；
- Session commit 前后；
- outbound 平台调用前后；
- queue ACK 前后。

证明消息不会静默丢失，并记录哪些场景可能重复。

### 2. 陈旧锁持有者测试

暂停 Worker A 直至租约过期，让 Worker B 获锁并提交，再恢复 A。验证 A 无法：

- 修改 Session state；
- append 非幂等事件；
-执行外部工具；
- 发布 outbound。

### 3. Envelope 重放测试

同一 Envelope 在每个故障点后重放，验证：

- USER event 不重复；
- Session seq 不分叉；
- 幂等工具不重复产生副作用；
- Artifact 不重复生成版本；
- outbound 在平台支持幂等时不重复。

### 4. 群会话测试

两个用户在同一群中：

- 能读取共享群上下文；
- 不能读取彼此私有 Memory；
- 发言人身份正确进入 prompt 和 audit；
- 移除成员后不能继续访问。

### 5. 跨租户对抗测试

构造以下攻击输入：

- 伪造 `app_name`；
- 伪造 `ArtifactId`；
- 覆盖 Knowledge filter；
- 伪造 queue Envelope tenant；
- 使用其他租户的 SecretRef；
- Admin API 缺失 tenant scope；
- 恶意 `agent_factory`。

任何路径都不能读写其他租户资源。

### 6. PostgreSQL 迁移测试

在真实 PostgreSQL 上验证：

- 空库创建；
- 从旧 SDK schema 升级；
- expand-contract；
- 分区创建与轮转；
- 并发索引；
- rollback；
- 大表迁移锁时长。

### 7. Post-turn kill 测试

在 Summary 生成前后、Memory 写入前后、Session 压缩前后执行 `kill -9`，验证：

- 原始事件不丢；
- Memory 最终补齐；
- Summary 不回退；
- 不重复压缩或破坏 event window。

### 8. 后端故障测试

Redis/SQL 故障时验证系统不会自动形成 InMemory split-brain，消息应保留在 durable queue 并在恢复后按序继续。

### 9. Canary 与迁移屏障测试

切换 stable/canary 或 source/target 时，验证同一 Session 的连续消息不会跨 epoch 并发处理，也不会由旧 Worker 在切换后写入。

### 10. Secret 泄露测试

测试样本必须覆盖：

- API key；
- DSN 密码；
- Authorization header；
- `extra_headers`；
- `BackendSpec.options`；
- SecretRef 路径；
- 异常链和 traceback；
- OTel attributes/events；
- 流式跨 chunk 密钥；
- Admin dry-run 错误。

---

## 六、建议调整实施顺序

原路线图先做 Alembic、SecretRef 和 Redis Session V2，但更合理的顺序是：

| 阶段 | 目标 | 必要交付 |
|---|---|---|
| **P0 语义冻结** | 明确系统保证 | at-least-once 语义、Turn 状态机、ordering/fencing/idempotency ADR |
| **P1 持久消息边界** | 不丢消息 | durable Inbox/Outbox、原子入队、恢复器、崩溃矩阵测试 |
| **P2 Turn 与 Session 协议** | 可安全重放 | Runner persisted-turn API、确定性 ID、seq、revision、真实 fencing |
| **P3 租户安全边界** | 防跨租户访问 | tenant-bound facade、RLS 可选档、Knowledge 强制过滤、factory allowlist |
| **P4 Post-turn 可靠性** | Summary/Memory 可恢复 | immutable event-range job、durable outbox、summary lease/CAS |
| **P5 节点化与取消** | 多节点运行 | ordered scheduler、distributed cancel、stream correlation |
| **P6 IM 与治理** | 接入和策略 | Adapter、附件异步摄取、预算预留、跨 chunk 脱敏 |
| **P7 运维与迁移** | 可发布 | migration barrier、canary epoch、真实容量压测、灾难恢复演练 |

---

## 最终意见

原设计是一份较完整的需求和组件清单，但目前对以下能力的表述超过了实际机制能够提供的保证：

- 严格顺序；
- 不丢消息；
- fencing；
- 全链路幂等；
- 跨租户不可访问；
- 无状态 Worker；
- 安全的后端降级。

在这些基础语义没有收敛前，继续扩展 TenantConfig、Filter、Channel Adapter 和部署清单会放大返工成本。建议优先补充一份独立 ADR，明确 **Turn 状态机、持久 Inbox/Outbox、确定性身份、fencing epoch、Session revision 和租户隔离档位**，再据此修订主设计文档和实施路线图。
