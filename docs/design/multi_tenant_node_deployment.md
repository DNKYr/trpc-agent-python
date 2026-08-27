# 多租户与节点部署 — 设计与实施计划

> 目标：在 `trpc_agent_sdk` 现有能力（Runner / SessionService / MemoryService / Filter /
> Telemetry / openclaw channels）之上，增加一层**多租户运行时**，支持多节点水平扩展、
> 多数据后端、IM 通道接入、租户级治理与可观测性。
>
> 设计原则：**尽量不破坏现有单进程用法**。新增能力主体放在 `trpc_agent_sdk/tenancy/` 与
> `trpc_agent_sdk/server/multitenant/`；但经评审确认，**核心（`Runner` / `SessionServiceABC`）
> 必须改**，无法纯靠组合完成。

---

## 修订说明（v2，经对抗性评审）

本文初版经 [对抗性评审](./multi_tenant_node_deployment_adversarial_review.md) 审查，
结论为 **Request Changes**。我逐条回到代码验证后确认：**评审的 10 项阻断问题全部成立**，
其中 6 项是我初版的**事实错误**（而非取舍差异）：

| # | 初版错误结论 | 代码依据 | 修正位置 |
|---|---|---|---|
| 4 | "唯一必须动核心的是 Redis session + Artifact" | `runners.py` L419-466：`run_async` 自己 `get_session`/`create_session`、`conversation_count += 1`、`_append_new_message_to_session` | §0、§5.6 |
| 6 | "`per_group` 实现群内共享上下文" | 主键是 `(app_name, user_id, session_id)`，成员 `user_id` 不同 ⇒ N 个独立 session | §3.1、§7.4 |
| 9 | "post-turn 先 summary 后 memory 无影响" | `_run_post_turn_processing` 先调 `create_session_summary`，而它会**压缩 `session.events`** ⇒ Memory 漏写原始事件 | §5.3 |
| 10.3 | "注册表只放类/工厂" | `filter/_registry.py` 装饰时 `create_and_save` 实例化，`get_filter()` 返回共享单例 | §4.4 |
| 2 | "分区保证同 session 严格串行" | Redis Streams 同 Stream 可并发派发；派发顺序 ≠ 完成顺序 | §5.1 |
| 3 | "UUID token 是 fencing token" | 比对 UUID 只能防误删锁，防不住陈旧持有者继续写入 | §5.1 |

另外评审发现了两个我完全遗漏的进程内状态：`cancel/_cancel.py` 是进程内单例
（跨节点 `/stop` 无效）、`LLMModel._api_key` 可变且有 setter（跨租户共享会串密钥）；
以及 `storage/_redis.py::query` 用 `KEYS` 而非 `SCAN`（生产会阻塞 Redis）。

**已拒绝/保留分歧**：无。本次评审我未发现需反驳的条目。

### 关于"是否过度设计"

**分层回答：正确性机制不是过度设计，但交付范围确实过宽。**

不可删（删了就丢数据/串租户）：§5.1 顺序与 fencing、§5.5 持久 Inbox、
§5.6 Turn 状态机、§4.4 facade 隔离、§5.3 event-range job。
这些是"多节点 + 多租户"这个需求本身的成本，不是额外装饰。

**可以大幅削减的（建议删除或降为 Phase 2）**：
- 四类 IM 通道 → **先做 1 类**（需求只要求两类，但第一个跑通后第二个是机械工作）
- 6 个治理 Filter → **先做 2 个**（工具白名单 + 预算），其余按真实客户要求加
- 三档隔离 + RLS + per-tenant CMK → **先做逻辑隔离一档**，有强监管客户再做
- `MigrationPlan` 五阶段 → 第一版可以只支持**停机迁移**（写清楚运维步骤）
- 向量库迁移、Mem0/Mempalace、`per_thread`、卡片消息、撤回 → 均可延后
- 20+ 个新 metrics → 先上 **6 个**（投递成功率、队列 lag、轮次耗时、错误率、token、成本）

**若需求其实是"内部 5-10 个团队共用一个 Agent 平台"（非对外 SaaS）**，
那么更合理的方案是：每团队一个独立部署（独立 Redis DB + 独立配置），
用 K8s namespace 隔离，**完全不需要本文 80% 的机制**。多租户共享运行时的复杂度
只在"租户数多、单租户载荷小、要求成本摊薄"时才划得来。**这是开工前应先确认的事。**

---

## 0. 现状盘点（Gap Analysis）

先明确"已有什么"，避免重复造轮子。以下结论来自代码阅读。

| 能力 | 现状 | 结论 |
|---|---|---|
| 执行入口 | `trpc_agent_sdk/runners.py::Runner`，字段 `app_name / agent / session_service / artifact_service / memory_service`，支持 `defer_post_turn_processing` 后台线程做 summary + memory 落库 | **可直接复用**。Runner 已经是"每租户一实例"的天然粒度 |
| Session 抽象 | `abc/_session_service.py::SessionServiceABC`，全部方法以 `(app_name, user_id, session_id)` 三元组寻址 | **键空间已具备隔离基础**，`app_name` 可承载 tenant 维度 |
| Session 实现 | `sessions/`：InMemory / Redis / RedisCluster / SQL，SQL 表为 `sessions` / `events` / `app_states` / `user_states`（`_sql_session_service.py`） | 多后端已存在，缺"按租户选后端"的工厂 |
| Session 并发 | `sessions/_redis_session_service.py::append_event`（L191-236）为**读-改-写整个 session 对象**；`_set_session` 整体覆盖 | ⚠️ **多节点并发写同一 session 会丢更新**，必须修 |
| Memory | `abc/_memory_service.py`：`store_session` / `search_memory`；实现有 InMemory / Redis(+Cluster) / SQL(`mem_events` 表) / Mem0 / Mempalace | 多后端已存在；`store_session` 在 post-turn 异步执行 → **跨节点可见性有延迟** |
| Summary | `sessions/_summarizer_manager.py` + `_summarizer_checker.py`（阈值函数式组合） | 复用，但需增加"摘要覆盖水位"防并发重复摘要 |
| Artifact | 只有 `artifacts/_in_memory_artifact_service.py`；`ArtifactId(app_name, user_id, session_id, filename)` | ❌ **生产不可用**，需新增 SQL / S3(对象存储) 实现 |
| Knowledge | `knowledge/_knowledge.py::KnowledgeBase.search(ctx, req)` + `server/knowledge/langchain_knowledge.py` | 复用；需按租户注入不同 vectorstore + `KnowledgeFilterExpr` 强制租户过滤 |
| 治理钩子 | `abc/_filter.py::FilterABC` + `FilterType.{TOOL,MODEL,AGENT}`，注册器 `filter/_registry.py::register_{tool,model,agent}_filter`，链式执行 `filter/_run_filter.py` | **治理策略的主要落点**。注意：无 "channel/ingress" 类型 filter，IM 入口治理需独立中间件 |
| 工具白名单 | `abc/_toolset.py::ToolPredicate` + `_is_tool_selected`；`tools/_registry.py` 单例注册表 | 双层防御：Toolset 层裁剪 + TOOL Filter 层硬拦截 |
| 预算/限流 | `configs/_run_config.py::RunConfig.max_llm_calls / max_tool_calls / agent_limits`，`configs/_agent_run_limits.py` | 复用为"单次调用上限"；租户级累计预算需新增 MODEL Filter |
| 危险工具确认 | `tools/_long_running_tool.py`、`plan_mode/_long_running_tools.py`、`examples/langgraph_agent_with_HITL` | 复用 long-running / HITL 机制做二次确认 |
| 可观测性 | `telemetry/_trace.py`：`trace_runner` / `trace_agent` / `trace_tool_call` / `trace_call_llm`；`telemetry/_metrics.py`：OTel `gen_ai.*` 指标；`server/langfuse/tracing/opentelemetry.py` | **已是 OTel 原生**。缺：IM callback span、Session/Memory IO span、tenant 维度 attribute |
| IM 通道 | `server/openclaw/channels/`：`_wecom.py`（继承 nanobot `WecomChannel`，已实现 `reply_stream` 增量流式）、`_telegram.py`、`_command_handler.py`（`/new` `/stop` `/help`）；总线为 nanobot `MessageBus`（`InboundMessage` / `OutboundMessage`，含 `session_key` / `chat_id` / `metadata`） | **单租户单进程**模型。需抽出 tenant 解析、验签、去重、多租户路由 |
| 配置 | `server/openclaw/config/_config.py::ClawConfig`（YAML），`AgentConfig.api_key` **明文** | ❌ 需 SecretRef 间接化 |
| 审计日志 | 无 | ❌ 全新建设 |
| 租户模型 | 无（全仓 grep `tenant` 仅命中文档与 `skills/hub/_github.py`） | ❌ 全新建设 |
| DB 迁移 | `storage/_sql.py` 由 SQLAlchemy metadata 建表，无 Alembic | ❌ 需引入版本化迁移 |
| CLI | `_cli.py::register_cli` 支持模块自动发现挂载子命令（openclaw 即如此） | 复用，新增 `trpc_agent_cmd mt {gateway,worker,admin}` |

**核心判断（已修正）**：SDK 的领域抽象足够好，但多租户**不是纯编排层问题**。

> ⚠️ 本文初版声称"唯一必须动核心的是 Redis session 并发写语义和 Artifact 实现"，
> 经对抗性评审核实，**该判断错误**。`Runner.run_async()`（`runners.py` L419-466）自己
> 承担了 `get_session`/`create_session`、`session.conversation_count += 1`、构造 USER Event
> 并 `append_event` 落库。因此 §5.2 描述的"Worker 先分配 seq、先写 USER event、再调
> `run_async`"在现有接口下**无法实现**（会导致用户输入重复写入）。

实际必须修改的核心清单：
1. **Redis session 并发写语义**（读-改-写丢更新，§5.1）
2. **Artifact 生产实现**（仅有 InMemory）
3. **`Runner` 需新增 persisted-turn 入口**（确定性 ID + 外部已持久化 USER event 语义，§5.2）
4. **`SessionServiceABC` 需 Turn/revision 语义**（`begin_turn`/`commit_turn`）
5. **post-turn 顺序 bug**：`_run_post_turn_processing` 先 `create_session_summary`（会**压缩**
   `session.events`）再 `store_session` ⇒ **Memory 收到的是已压缩事件窗口，长期记忆漏写**（§5.3）
6. **`FilterRegistry` 缓存的是实例不是类**（`_registry.py` 装饰时 `create_and_save`，
   `get_filter()` 返回共享单例）⇒ 有状态租户 Filter 会跨租户污染
7. **`cancel/_cancel.py` 是进程内单例**（`Dict[SessionKey, asyncio.Event]`）⇒ 跨节点 `/stop` 无效
8. **`storage/_redis.py::query` 用 `KEYS`**（非 `SCAN`）⇒ `list_sessions` 在生产会阻塞 Redis

---

## 1. 总体架构与组件拓扑

```
                        ┌──────────────────────────────────────────┐
   IM 平台 (WeCom /      │            Agent Gateway (无状态)         │
   微信客服 / 公众号 /    │  1 验签 (signature/secret/timestamp)      │
   Telegram)  ──webhook─▶│  2 TenantResolver: 路由键→tenant_id       │
                        │  3 Dedup: (tenant,channel,msg_id) 幂等    │
                        │  4 Normalize: IM msg → MessageEnvelope    │
                        │  5 5s 内 ACK / 空回包                     │
                        └───────────────┬──────────────────────────┘
                                        │ enqueue，按 session_id 哈希分区
                                        ▼
                        ┌──────────────────────────────────────────┐
                        │  Message Queue (Redis Streams / Kafka)    │
                        │  partition = hash(session_id) % N         │
                        └───────────────┬──────────────────────────┘
                                        │ consumer group
        ┌───────────────────────────────┼───────────────────────────────┐
        ▼                               ▼                               ▼
┌────────────────┐            ┌────────────────┐            ┌────────────────┐
│ Agent Worker 1 │            │ Agent Worker 2 │            │ Agent Worker N │  ← 无状态，HPA/KEDA
│ TenantRuntime  │            │ TenantRuntime  │            │ TenantRuntime  │
│  Cache(LRU)    │            │  Cache(LRU)    │            │  Cache(LRU)    │
│ Runner+Agent   │            │ Runner+Agent   │            │ Runner+Agent   │
│ +TenantFilters │            │ +TenantFilters │            │ +TenantFilters │
└───┬────────┬───┘            └───┬────────┬───┘            └───┬────────┬───┘
    │        │                    │        │                    │        │
    │        └────────────┬───────┴────────┴───────┬────────────┘        │
    │                     ▼                        ▼                     │
    │        ┌─────────────────────────┐  ┌──────────────────────┐        │
    │        │  Storage Adapter (工厂)  │  │ Telemetry Collector  │◀───────┘
    │        │ Session/Memory/Summary  │  │ (OTel Collector)     │
    │        │ Artifact/Knowledge/Audit│  └──────┬───────────────┘
    │        └───┬──────┬──────┬───────┘         │
    │            ▼      ▼      ▼                 ▼
    │        Redis   SQL   S3/VectorDB     Jaeger/Prometheus/Langfuse
    │
    ▼ outbound 事件（Redis Pub/Sub 或 Stream，key = tenant:channel:chat）
┌──────────────────────────────────────────┐   ┌───────────────────────┐
│ Channel Adapter (可与 Gateway 同进程)      │   │ Admin API (独立)       │
│  AgentEvent → IM 文本/流式/卡片            │   │ 租户 CRUD、配置版本     │
│  限频、分片、重试、撤回                     │   │ 灰度、回滚、密钥引用     │
└──────────────────────────────────────────┘   └───────────────────────┘
```

### 组件职责与协作

| 组件 | 职责 | 状态 | 扩展方式 |
|---|---|---|---|
| **Agent Gateway** | IM webhook 接入、验签、租户解析、幂等去重、消息归一化、快速 ACK、入队 | 无状态 | HPA on QPS/CPU；前置 L7 LB |
| **Agent Worker** | 消费队列、加载 `TenantRuntime`、构造 `Runner` 执行 `run_async`、发布 outbound 事件 | 无状态（会话状态全在共享后端） | KEDA on 队列深度/消费延迟 |
| **Channel Adapter** | 出向投递：`Event` → IM 回复；处理长度分片、限频、流式 edit、失败重试、卡片渲染 | 无状态 | 与 Gateway 合并部署或独立 |
| **Storage Adapter** | 按 tenant 的后端绑定，惰性创建 + 池化 SessionService/MemoryService/ArtifactService/Knowledge/AuditSink | 持有连接池（进程内缓存） | 随 Worker 扩展；连接池上限需算容量 |
| **Admin API** | 租户/应用/通道绑定/配置版本的 CRUD；灰度开关；配置变更广播（Pub/Sub 使 Worker 缓存失效） | 无状态 | 2 副本足够；写 SQL |
| **Telemetry Collector** | OTel Collector：接收 trace/metrics/logs，脱敏处理器，扇出到 Jaeger/Prom/Langfuse | DaemonSet/Deployment | 按节点 DaemonSet + 中心 Gateway 模式 |

**关键协作时序（一条 IM 消息）**

1. Gateway 收到 webhook → 验签 → `TenantResolver` 由路由键定位 `tenant_id`
2. `Dedup.check_and_mark(tenant, channel, external_msg_id)` → 命中则直接 ACK 返回
3. 构造 `MessageEnvelope`（含 `traceparent`，OTel span `im.callback` 在此开启）
4. 入队（分区键 = `session_id`）→ 立即 ACK（满足企业微信/公众号 5s 限制）
5. Worker 消费 → `link` 到上游 trace → 取 `TenantRuntime`（缓存未命中则从 Admin 配置加载）
6. 获取 session 级串行化（分区 + 分布式锁双保险）→ `Runner.run_async(...)`
7. 逐事件流式发布 outbound（progress 事件用于流式回复）
8. post-turn：summary + memory 落库（`Runner` 已有 deferred 线程）
9. 写审计日志、上报指标；`finally` 释放锁、ACK 队列消息

---

## 2. 租户模型

新模块 `trpc_agent_sdk/tenancy/`（遵循仓库约定：私有 `_x.py` + `__init__.py` 导出，Apache 头，yapf 120 列）。

```
trpc_agent_sdk/tenancy/
├── __init__.py
├── _tenant_config.py      # TenantConfig 及各子配置（pydantic BaseModel）
├── _scope.py              # TenantScope：租户 → SDK 键空间映射
├── _secret_ref.py         # SecretRef + SecretResolver（env/vault/k8s/kms）
├── _registry.py           # TenantRegistry ABC + SQL/File 实现（配置源）
├── _resolver.py           # TenantResolver：路由键 → tenant_id
├── _runtime.py            # TenantRuntime + TenantRuntimeCache（Runner/服务装配）
├── _revision.py           # 配置版本、灰度、回滚
└── backends/
    ├── __init__.py
    ├── _spec.py           # BackendSpec（type + dsn_ref + options）
    └── _factory.py        # BackendFactory：spec → 具体 Service 实例（带池化）
```

### 2.1 数据结构

```python
# trpc_agent_sdk/tenancy/_tenant_config.py（示意，实际加 Field(description=...) 与 docstring）

class SecretRef(BaseModel):
    """密钥引用，绝不存明文。"""
    provider: Literal["env", "vault", "k8s", "kms", "inline_encrypted"]
    key: str                     # 例：vault://kv/tenants/acme/openai#api_key
    version: Optional[str] = None

    def __repr__(self) -> str:   # 防止日志/traceback 泄露
        return f"SecretRef({self.provider}:{self.key})"
    __str__ = __repr__


class ModelBinding(BaseModel):
    alias: str                       # 租户内逻辑名，如 "default" / "cheap"
    provider: Literal["openai", "anthropic", "litellm"]
    model_name: str
    base_url: Optional[str] = None
    api_key_ref: SecretRef
    extra_headers: dict[str, str] = Field(default_factory=dict)
    retry: ModelRetryConfig = Field(default_factory=ModelRetryConfig)   # 复用 configs/
    prompt_cache: PromptCacheConfig = Field(default_factory=PromptCacheConfig)
    max_tokens_per_request: int = 0
    cost_per_1k_input: float = 0.0   # 用于成本核算
    cost_per_1k_output: float = 0.0


class ToolPolicy(BaseModel):
    """工具权限：默认拒绝。"""
    default_action: Literal["deny", "allow"] = "deny"
    allowed_tools: list[str] = Field(default_factory=list)      # 支持 glob: "file_*"
    denied_tools: list[str] = Field(default_factory=list)       # 优先级高于 allow
    require_confirmation: list[str] = Field(default_factory=list)  # 危险工具二次确认
    mcp_servers: list[str] = Field(default_factory=list)        # 允许的 MCP server id
    skill_allowlist: list[str] = Field(default_factory=list)
    code_executor: Literal["disabled", "local", "container"] = "disabled"
    per_tool_timeout_s: dict[str, float] = Field(default_factory=dict)
    egress_allowlist: list[str] = Field(default_factory=list)   # webfetch/websearch 域名白名单


class ChannelBinding(BaseModel):
    channel_id: str                                            # 租户内唯一
    kind: Literal["wecom", "wecom_kf", "wechat_mp", "telegram", "http"]
    enabled: bool = True
    route_key: str                                             # 路由键，见 §7.2，全局唯一
    webhook_path: str                                          # /im/{kind}/{route_key}
    token_ref: Optional[SecretRef] = None                       # 校验 token
    secret_ref: Optional[SecretRef] = None                      # 加解密/签名 secret
    app_credential_refs: dict[str, SecretRef] = Field(default_factory=dict)  # corp_id/agent_id/bot_token
    group_session_policy: Literal["per_group", "per_group_user", "per_thread"] = "per_group"
    reply_mode: Literal["stream", "chunked", "single", "card"] = "chunked"
    rate_limit: ChannelRateLimit = Field(default_factory=ChannelRateLimit)
    allowed_external_users: list[str] = Field(default_factory=list)  # 空=不限制
    require_user_binding: bool = False                          # 是否强制先绑定内部账号


class BackendSpec(BaseModel):
    type: Literal["inmemory", "redis", "redis_cluster", "sql", "s3", "vector", "mem0", "mempalace"]
    dsn_ref: Optional[SecretRef] = None
    options: dict[str, Any] = Field(default_factory=dict)       # ttl、pool_size、table_prefix、bucket...


class DataBackends(BaseModel):
    session: BackendSpec
    memory: BackendSpec
    summary: Optional[BackendSpec] = None                       # None → 跟随 session
    artifact: BackendSpec
    knowledge: Optional[BackendSpec] = None
    audit: BackendSpec
    migration: Optional[MigrationPlan] = None                   # 见 §5.4


class AuditPolicy(BaseModel):
    enabled: bool = True
    sink: Literal["sql", "kafka", "file", "otel_log"] = "sql"
    retention_days: int = 180
    record_payload: bool = False                                # 是否落消息内容（默认否）
    payload_max_chars: int = 2000
    redaction_rules: list[str] = Field(default_factory=list)     # 命名规则集，见 §8.5
    log_level: Literal["minimal", "standard", "verbose"] = "standard"


class BudgetPolicy(BaseModel):
    daily_token_limit: int = 0                                  # 0=不限
    daily_cost_limit_usd: float = 0.0
    monthly_cost_limit_usd: float = 0.0
    max_concurrent_sessions: int = 0
    qps_limit: float = 0.0
    on_exceed: Literal["reject", "degrade_model", "queue"] = "reject"
    degrade_model_alias: Optional[str] = None


class AgentAppConfig(BaseModel):
    app_id: str                                                 # 租户内唯一
    agent_factory: str                                          # "my_pkg.agents:build_agent" 或内置模板名
    model_alias: str = "default"
    instruction_template: str = ""
    run_config: RunConfig = Field(default_factory=RunConfig)     # 复用 configs/_run_config.py
    tool_policy_override: Optional[ToolPolicy] = None
    filters: list[str] = Field(default_factory=list)             # 注册表中的 filter 名
    plan_mode: bool = False


class TenantConfig(BaseModel):
    tenant_id: str                                              # 稳定不可变；小写 [a-z0-9-]{3,32}
    display_name: str
    status: Literal["active", "suspended", "readonly", "deleting"] = "active"
    revision: int = 1                                           # 配置版本，见 §9.2
    apps: dict[str, AgentAppConfig] = Field(default_factory=dict)
    models: dict[str, ModelBinding] = Field(default_factory=dict)
    tool_policy: ToolPolicy = Field(default_factory=ToolPolicy)
    channels: dict[str, ChannelBinding] = Field(default_factory=dict)
    backends: DataBackends
    audit: AuditPolicy = Field(default_factory=AuditPolicy)
    budget: BudgetPolicy = Field(default_factory=BudgetPolicy)
    release_channel: Literal["stable", "canary"] = "stable"      # 灰度
    labels: dict[str, str] = Field(default_factory=dict)
```

必含七要素对照：`tenant_id` ✅、应用配置 `apps` ✅、模型配置 `models` ✅、
工具权限 `tool_policy` ✅、IM 通道 `channels` ✅、数据后端 `backends` ✅、审计策略 `audit` ✅。

### 2.2 TenantScope：租户 → SDK 键空间

SDK 一切以 `(app_name, user_id, session_id)` 寻址。**不改 SDK 签名**，而是让 tenant 进入 `app_name`：

```python
# trpc_agent_sdk/tenancy/_scope.py
SCOPE_SEP = "|"   # 选择在 tenant_id / app_id 字符集中被禁用的分隔符

@dataclass(frozen=True)
class TenantScope:
    tenant_id: str
    app_id: str

    @property
    def app_name(self) -> str:
        """SDK app_name = '<tenant_id>|<app_id>'，天然隔离 Redis key 前缀与 SQL 主键。"""
        return f"{self.tenant_id}{SCOPE_SEP}{self.app_id}"

    @classmethod
    def parse(cls, app_name: str) -> "TenantScope":
        tenant_id, _, app_id = app_name.partition(SCOPE_SEP)
        ...
```

副作用（正向）：
- Redis：`sessions/_redis_session_service.py::_session_key_prefix` → `session:{tenant}|{app}:{user}:*`，前缀天然隔离，`list_sessions` 不会跨租户
- SQL：`sessions` / `events` / `app_states` / `user_states` 主键首列即 `app_name`，索引前缀命中

> ⚠️ **修正：`app_name = tenant|app` 是命名空间约定，不是安全边界。**
> 本文初版称"跨租户查询不可能手滑"，这个说法过强。任何拿到共享 service 句柄的
> 代码都能主动传入另一个租户的 `app_name`；共享 DB 凭据能读到所有行。
> 必须补的防线见 §4.4（tenant-bound facade / RLS / 三档隔离）。
- Memory：`utils/_hash_key.py::user_key(app_name, user_id)` → `save_key` 已含 tenant
- Artifact：`ArtifactId.app_name` 已含 tenant

同时在 `AgentContext.metadata` 注入租户上下文（`AgentContext` 是 `extra="forbid"`，但有
`with_metadata` / `get_metadata` 私有 dict，正是为此设计）：

```python
ctx = new_agent_context(metadata={
    "tenant_id": scope.tenant_id, "app_id": scope.app_id,
    "channel_id": ..., "external_user_id": ...,
    "trace_id": ..., "request_id": ..., "revision": cfg.revision,
})
```
所有 Filter 的 `_before/_after(ctx, req, rsp)` 都能拿到它 → 治理策略无需额外传参。

---

## 3. 节点部署拓扑与消息路由

### 3.1 路由：消息 → 正确租户 + 正确 session

**三段式解析，全部在 Gateway 完成，Worker 不再做租户判断。**

**第一段：定位 tenant（路由键）**

优先级从高到低，命中即止：

1. **URL 路径**：`POST /im/{kind}/{route_key}` — `route_key` 是随机 22 字符（不可枚举），
   `channel_bindings` 表唯一索引 → 一次查表得 `(tenant_id, channel_id)`。**推荐**：不依赖平台字段。
2. **平台标识回退**：`wecom` 的 `corp_id + agent_id`、`telegram` 的 `bot_id`、
   `wechat_mp` 的 `ToUserName`(公众号原始 ID) → 建 `(kind, platform_key)` 唯一索引。
   用于平台不支持自定义回调路径的场景。
3. **HTTP/内部调用**：`X-Tenant-Id` header + mTLS/JWT（`aud=tenant_id`），仅内网可用。

> 拒绝"从消息体里读 tenant_id"这类可伪造方案。Gateway 必须**先验签再解析**，
> 验签所用 token/secret 由 `route_key` 查出的 binding 决定。

**第二段：定位 session_id（确定性生成，非随机）**

> ⚠️ **修正（两处）**：
> ① **无密钥 SHA-1 不够**：`sha1(x)[:16]` 仅 64 bit，且外部 ID 低熵、格式固定，可字典柚举；
> 跨环境相同 hash 产生可关联标识，且无法轮换。改用 **tenant-scoped HMAC-SHA-256**。
> ② **`per_group` 原方案根本不会共享群上下文**：SDK 主键是
> `(app_name, user_id, session_id)`，只让群成员共用 `session_id` 但 `user_id` 各不相同，
> 得到的是 **N 个不同 session**。共享群会话必须用**合成存储主体**。

```python
# 统一身份标识：tenant-scoped HMAC，保留 ≥128 bit，带 key 版本
uid = hmac_sha256(tenant_identity_key_v(n), f"{channel_kind}|{channel_id}|{external_id}")[:32]
gid = hmac_sha256(tenant_identity_key_v(n), f"{channel_kind}|{channel_id}|{group_id}")[:32]

# 单聊
user_id    = mapped_user_id                    # channel_user_bindings 映射
session_id = f"c:{channel_id}/u:{uid}"

# 群聊 - per_group（群内共享上下文，默认）—— 必须用合成 group 主体
user_id    = f"group:{gid}"                    # ← 关键：存储主体是群，不是人
session_id = f"c:{channel_id}/g:{gid}"
# 真实发言人只能通过可信 event metadata 传入，绝不进存储主键：
#   speaker_user_id / speaker_external_hmac / speaker_role

# 群聊 - per_group_user（群内每人独立上下文）
user_id    = mapped_user_id
session_id = f"c:{channel_id}/g:{gid}/u:{uid}"

# 群聊 - per_thread
session_id = f"c:{channel_id}/g:{gid}/t:{thread_id}"
```

`per_group` 的额外约束（否则就是隐私事故）：
- **绝不能以合成 `group:` 主体加载任何个人长期记忆**
- 群消息中的用户私密查询不得写入其他成员可见的共享历史
- **授权必须基于当前发言者**，而不是合成 `user_id`（否则任何群成员都能冒充整个群）
- 成员退群后需处理其历史身份数据与后续访问权

其余要点：
- 确定性 ⇒ 同一会话的消息落到同一分区（降低争抢，不提供顺序保证，见 §5.1）
- HMAC key 需设版本号与轮换映射表；原始 external ID 仅在确有业务必要时加密保存

**第三段：分区**

```
partition = crc32(f"{tenant_id}|{app_id}|{session_id}") % PARTITION_COUNT
```
- Redis Streams：`stream:agent:p{n}`，consumer group `workers`
- Kafka：key = 上述字符串，由默认分区器保证同 key 同分区
- 效果：**同一 session 的消息严格串行**，这是一致性的第一道也是最主要的保障
- `PARTITION_COUNT` 取远大于 Worker 数（如 256），便于扩容时重平衡

### 3.2 是否需要 Sticky Session？

**结论：正确性上不需要；性能上建议"软亲和"；只有一类场景需要真亲和。**

| 场景 | 是否需要 sticky | 说明 |
|---|---|---|
| IM webhook（企业微信/公众号/Telegram） | **不需要** | 请求-响应解耦：Gateway 立即 ACK，回复由 Channel Adapter 异步发出。任意 Worker 处理任意 session |
| Admin API / 批量任务 | 不需要 | 无状态读写共享后端 |
| HTTP SSE / WebSocket 流式（AG-UI、`server/ag_ui/`） | **连接生命周期内需要** | TCP 连接绑定在某个进程上。解法二选一：(a) L7 LB 按 `session_id` 一致性哈希做 sticky；(b) **推荐**：Worker 把事件发到 Redis Stream `outbound:{session_id}`，持有连接的节点订阅转发 → 回归无状态 |
| A2A 长任务（`server/a2a/executor/`） | 不需要 | task 状态落共享后端，`task_id` 可被任意节点续查 |

**无状态 Worker 如何做到？**（这是设计的核心）

1. **会话状态零本地化**：Worker 不缓存 `Session` 对象跨请求。每次处理消息都
   `session_service.get_session(...)` 从共享后端读全量，处理完 `append_event` 写回。
   现有 `Runner.run_async` 已是这个模型 —— `InvocationContext.session` 生命周期仅一次调用。
2. **Runner/Agent 是纯计算体**：`Runner` 只持有 agent 定义 + service 句柄（连接池），
   无会话状态 ⇒ 可安全缓存复用（`TenantRuntimeCache`），与 sticky 无关。
3. **串行化靠"顺序协议 + 锁 + fencing"**（已修正）：
   - ⚠️ 分区**不**保证串行（见 §5.1），只降低争抢概率与提高缓存命中率
   - 真正的串行化 = `ingress_seq`/`next_expected_seq` 顺序协议
     + 会话级分布式锁 + 单调 epoch fencing
   - 因此分布式锁是**主要正确性机制，不是"兜底"**
4. **进程内可变状态白名单化**（必须逐个消灭或外置）：
   - `Runner._post_turn_thread`：后台 summary/memory 线程。节点被 kill 时队列内任务丢失
     → **对策**：`defer_post_turn_processing=False`（在锁内同步完成，代价是延迟）
     或写入 `post_turn_pending` 表由巡检补偿。**推荐后者**。
   - ⚠️ **修正**：`filter/_registry.py` 当前**并非只放类** —— 装饰时即 `create_and_save`
     实例化，`get_filter()` 返回共享单例（`_filter_runner.py::_init_filters` 直接
     append 该单例）。需**改造为 factory 注册**或强制 Filter 无状态（见 §4.4）。
     `tools/_registry.py` / `models/_registry.py` 同样不得持有租户实例；
     尤其 `LLMModel` 有可变 `_api_key` + `set_api_key()` setter，跨租户共享会串密钥。
   - `cancel/_cancel.py`：进程内 `Dict[SessionKey, asyncio.Event]` → 需分布式 control
     channel（§5.7），否则跨节点 `/stop` 完全无效。
   - `skills/_hot_reload.py`、`code_executors/local`：本地文件系统状态
     → 按 `tenant_id` 分目录 + 容器执行器优先 + 不假设跨请求存活。
   - `plan_mode/_lock.py`：进程内 `asyncio.Lock` dict。已核实计划态本身落 `ctx.state`
     （跨节点可见），由 §5.1 会话级锁覆盖串行性 ⇒ 低优先。

**软亲和（可选优化）**：一致性哈希把 session 倾向路由到固定 Worker，可提高
`TenantRuntime` 缓存命中率、减少锁争抢与 prompt cache miss。**但系统不能依赖它成立** —— 
这是"优化"而非"约束"，故障转移时随意漂移不影响正确性。

### 3.3 水平扩展

- **Gateway**：无状态，HPA on CPU/RPS。webhook 有 5s 硬超时 ⇒ 必须保证 P99 ACK < 1s

> ⚠️ **修正：Gateway 的职责清单与"快速 ACK"自相矛盾。**
> 初版又要求 Gateway 下载 IM 附件、上传 S3、构造完整 Content（§7.2），这与 5s 硬超时直接冲突。
>
> **快路径只做**：① 解析路由候选 ② 从本地/Redis 缓存读 binding ③ 验签
> ④ 持久化原始消息或**平台媒体引用**（不下载）⑤ 原子入 Inbox/队列 ⑥ ACK。
> 附件下载、病毒扫描、S3 上传、Content 转换全部移到**异步 media ingestion 阶段**。
>
> 另，§3.1 的"先验签再解析 tenant"表述不准确（验签本身需先找到 binding secret）。
> 准确流程：`解析不可信路由候选 → 查候选 binding/secret → 验签 → 验签成功后才信任 tenant 归属`。
- **Worker**：KEDA on `XPENDING` 深度或消费滞后。扩容后 consumer group 自动重平衡；
  缩容时先 `SIGTERM` → 停止拉新消息 → 排空在途（`terminationGracePeriodSeconds` ≥ 单轮最长耗时）
- **分区数固定**为 256/512，扩缩容只改 consumer 数，不改分区键 ⇒ 无需重建历史

---

## 4. 统一数据访问抽象与多后端

### 4.1 抽象分层

SDK 已有 `SessionServiceABC` / `MemoryServiceABC` / `ArtifactServiceABC` / `KnowledgeBase`，
**不新增第五套接口**，只补两块：`AuditSinkABC`（新）和 `BackendFactory`（装配）。

```python
# trpc_agent_sdk/tenancy/backends/_factory.py

class BackendFactory:
    """spec → service 实例，按 (tenant, kind, spec_fingerprint) 池化复用连接。"""

    def session_service(self, t: TenantConfig) -> SessionServiceABC: ...
    def memory_service(self, t: TenantConfig) -> MemoryServiceABC: ...
    def artifact_service(self, t: TenantConfig) -> ArtifactServiceABC: ...
    def knowledge(self, t: TenantConfig) -> Optional[KnowledgeBase]: ...
    def audit_sink(self, t: TenantConfig) -> AuditSinkABC: ...
```

关键实现要点：
- **连接池按 DSN 复用，不按租户复用**：100 个租户共用一个 PG 实例时只需一个池。
  池 key = `(type, resolved_dsn_hash, options_hash)`。租户隔离靠 `app_name` 前缀 + 行级过滤。
- **`Runner` 的 close 语义**：构造时传
  `close_session_service_on_close=False, close_memory_service_on_close=False`
  （`runners.py` 已支持，docstring 明确为"应用级共享连接池"场景）—— 否则短生命周期
  Runner 会关掉共享池。**这是必须踩对的点**。
- **失败快速降级**：spec 解析/连接失败 → 依 `status` 决定拒绝服务还是降级到 InMemory
  并标记 `degraded=true`（见 §9.1）。

### 4.2 各类数据的存储方案

| 数据 | 抽象 | 后端选项 | 键/表 | 隔离方式 |
|---|---|---|---|---|
| **Session**（events + state） | `SessionServiceABC` | InMemory / Redis / RedisCluster / SQL | Redis `session:{tenant\|app}:{user}:{sid}`；SQL `sessions` + `events` | `app_name` 首列/前缀 |
| **Session State** 分桶 | 同上 | 同上 | SQL `app_states` / `user_states`；`sessions.state` | `sessions/_utils.py::extract_state_delta` 已按 `app:` / `user:` / `temp:` 前缀分流，直接复用 |
| **Summary** | `SummarizerSessionManager` | 默认随 session 后端；可独立 SQL | 新表 `session_summaries`（含 `covers_up_to_seq`） | `app_name` + `session_id` |
| **Memory**（长期记忆） | `MemoryServiceABC` | InMemory / Redis / SQL(`mem_events`) / Mem0 / Mempalace | `memory:{save_key}:{sid}`；`mem_events.save_key` | `save_key = user_key(app_name, user_id)` 已含 tenant |
| **Artifact** | `ArtifactServiceABC` | ❌ 需新增 SQL / S3 | S3 `s3://{bucket}/{tenant}/{app}/{user}/{sid}/{filename}/v{n}`；元数据表 `artifacts` | 路径前缀 + bucket policy；**建议高敏租户独立 bucket** |
| **Knowledge**（向量） | `KnowledgeBase` | 本地 FAISS/Chroma / 远端 Milvus/Qdrant/PGVector | collection `kb_{tenant}_{kb_id}` 或单 collection + `tenant_id` 元数据过滤 | **强烈推荐 collection-per-tenant**；共享 collection 必须在 `build_search_extra_params` 强制注入 `tenant_id` 过滤，且过滤条件由服务端拼接、不接受客户端覆盖 |
| **Audit Log** | `AuditSinkABC`（新） | SQL(分区表) / Kafka / OTel Log / 文件 | `audit_logs` 按天分区 | `tenant_id` 列 + 行级授权；Admin 查询强制注入 |
| **Dedup / 幂等** | `DedupStore`（新） | Redis（主）+ SQL（兜底） | `dedup:{tenant}:{channel}:{msg_id}` TTL 24h | key 前缀 |
| **Quota / 用量** | `UsageCounter`（新） | Redis（热计数）+ SQL（对账） | `usage:{tenant}:{yyyymmdd}` HINCRBY | key 前缀 |
| **Tenant Config** | `TenantRegistry` | SQL（主）+ 文件（本地开发） | `tenants` / `agent_apps` / `channel_bindings` / `config_revisions` | Admin API 授权 |

### 4.4 租户隔离档位（安全边界）

§2.2 的 `app_name` 前缀只是**命名空间**。真正的隔离需下列防线，分三档提供：

| 隔离档位 | Session/SQL | Artifact | Knowledge | Worker | 适用 |
|---|---|---|---|---|---|
| **逻辑隔离** | tenant-bound facade + `tenant_id` 列 | 共享 bucket 前缀 | 共享 collection，服务端强制 filter | 共享 | 普通租户 |
| **数据库隔离** | PostgreSQL RLS + 独立 DB role | 独立 prefix/KMS | collection per tenant | 共享 | 企业租户 |
| **物理隔离** | 独立 DB/Redis 实例 | 独立 bucket/CMK | 独立实例 | 独立 Worker pool | 强监管租户 |

**必需的代码级防线**（不分档位都要做）：

1. **tenant-bound service facade**：对业务代码暴露的接口**不接受任意 `app_name`**，
   `TenantScope` 在 facade 内部固定；对传入/返回的 `Session`、`ArtifactId` 做归属校验
   （这是防"代码手滑"与"代码被攸略"的主防线）
2. **Worker 端重新校验绑定**：队列 Envelope 的 tenant/app/channel **不能完全信任 Gateway**，
   Worker 必须重查一次绑定关系
3. **Knowledge 过滤强制注入**：在服务端 adapter 内拼，**不接受客户端覆盖**
4. **`agent_factory` 必须走管理员 allowlist**：现设计的 `"my_pkg.agents:build_agent"`
   若由租户配置直控，**等价于远程代码执行**。租户只能选已批准的模板 ID
5. **实例不得跨租户共享**（两个已核实的真实隐患）：
   - `FilterRegistry` 装饰时就 `create_and_save` 实例化，`get_filter()` 返回**共享单例**
     ⇒ 租户 Filter 若持有预算/规则/缓存等状态，会跨租户污染与数据竞争。
     **对策**：注册表只存 factory/class，`TenantRuntime` 每租户构造自己的 Filter 实例；
     或强制所有注册 Filter 不可变且无状态（需 CI 检查）
   - `LLMModel` 持有可变 `_api_key` / `_base_url` 且有 `set_api_key()` / `set_base_url()` setter
     ⇒ 误用共享实例可能**把 A 租户密钥用于 B 租户请求**。
     **对策**：模型实例绝不跨租户共享；密钥只作请求级不可记录 Secret 注入，
     不得通过共享对象 setter 切换
6. **强隔离档用 `tenant_id` 显式列 + PostgreSQL RLS**，而非仅靠字符串前缀

### 4.3 后端一致性取舍

| 后端 | 一致性 | 读/写延迟 | 成本 | 运维复杂度 | 适用 |
|---|---|---|---|---|---|
| InMemory | 进程内强一致，**跨节点无一致性** | µs | 最低 | 最低 | 单机开发、测试、CI（`tests/sessions/test_in_memory_session_service.py`） |
| Redis 单实例 | 单 key 线性一致（单线程模型）；主从异步复制 ⇒ **failover 可能丢最后几笔写** | 0.2–1 ms | 中（内存贵） | 中 | 会话热数据、锁、去重、限流。**默认推荐** |
| Redis Cluster | 同上；**跨 slot 无原子性、无跨 key 事务** | 0.3–2 ms | 中高 | 高 | 超大规模；要求同 session 的 key 用 hashtag `{...}` 落同 slot |
| SQL (PG/MySQL) | 强一致 + 事务 + 唯一约束（幂等的最可靠靠山） | 2–15 ms | 中 | 中 | 审计、配置、对账、需要严格一致的会话 |
| 对象存储 (S3) | **最终一致的元数据 + 强一致读己写**（现代 S3 已 read-after-write）；无事务 | 20–200 ms | 最低/GB | 低 | Artifact、大文件、冷备 |
| 向量库（远端） | 最终一致（索引构建异步）⇒ **写入后立刻检索可能查不到** | 5–100 ms | 高 | 中高 | Knowledge。需在 UI 上明确"索引中"状态 |
| 外部 Memory 服务（Mem0） | 最终一致，且**延迟不可控**（含 LLM 抽取） | 100 ms–数 s | 按调用计费 | 低（托管） | 长期记忆增强，不可放在关键路径同步等待 |

**推荐组合**

| 档位 | Session | Memory | Summary | Artifact | Knowledge | Audit |
|---|---|---|---|---|---|---|
| 开发 | InMemory | InMemory | InMemory | InMemory | 本地 Chroma | 文件 |
| 标准生产 | Redis | Redis | SQL | S3 | 远端向量库(collection/租户) | SQL 分区表 |
| 强合规 | SQL | SQL | SQL | S3(独立 bucket+KMS) | 独立向量实例 | SQL + Kafka 双写 |

---

## 5. 数据同步策略

### 5.1 多节点并发写同一 session 的一致性

**这是本设计最需要严肃处理的问题，且现有实现有真实缺陷。**

现状问题（`sessions/_redis_session_service.py` L191–236）：
```python
storage_session = await self._get_session(...)      # 读整个 session
storage_session.events = session.events             # 用本节点内存快照整体覆盖
await self._set_session(redis_session, storage_session)   # 整体写回
```
若节点 A、B 同时处理同一 session：后写者用自己的快照覆盖，**A 的 events 与 state_delta 静默丢失**。

四层防御（自上而下，越上层越省事）：

**L1 分区（降低争抢，但 ⚠️ 不提供串行化保证）**

> ⚠️ **修正**：本文初版称"分区使同 session 消息严格串行"，**对 Redis Streams / Kafka 均不成立**：
> 同一 Stream 的不同消息可并发派发给多个 consumer；派发顺序 ≠ 完成顺序；
> `XAUTOCLAIM` 会在慢 consumer 未完成时把消息交给另一节点；锁竞争失败后"重投队尾"
> 会让后续消息越过旧消息；Kafka 下应用内并发处理同 partition 也会破坏完成顺序。
> 分区只能**降低**锁争抢概率与提高缓存命中率，不能作为正确性机制。

**顺序协议（真正的串行化机制，必选）**
- 每条消息在 ingress 处分配单调 `ingress_seq`（per session）
- Session 维护 `next_expected_seq`；**只有 `ingress_seq == next_expected_seq` 的消息可执行**
- 不符合的消息不重投队尾，而是**保留原顺序暂停该 session**（park/延迟重试同位置）
- 或：Worker 内维护按 session 排序的调度器，同一 session 同时只有一个在途轮次
- ACK / offset commit 必须在对应顺序位置完成后才推进

**L2 会话级分布式锁 + 真 fencing epoch（主要正确性机制，非"兜底"）**

> ⚠️ **修正**：初版的 `token = uuid4().hex` 只是**锁所有权 token**，不是 fencing token。
> 场景：A 拿锁 → A 长时间 stop-the-world 暂停（GC / 调度 / 网络）→ 租约过期 →
> B 拿新锁并写入 → **A 恢复后继续提交陈旧状态**。比对 UUID 只能防止 A 删 B 的锁，
> 防不住 A 写 session / 调外部工具 / 发 IM 回复。

```python
# 单调递增 epoch 作为 fencing token
epoch = await redis.incr(f"epoch:sess:{key}")          # 单调，永不回退
ok = await redis.set(f"lock:sess:{key}", epoch, nx=True, px=LEASE_MS)
# 所有可变存储写入携带 epoch；Redis Lua / SQL UPDATE 必须拒绝小于当前 epoch 的写入：
#   UPDATE sessions SET ..., fence_epoch = :epoch
#    WHERE app_name=... AND fence_epoch <= :epoch
```
- 租约 30s + 每 10s 续租；**续租失败必须立即取消当前 Invocation**（不能让它继续跑）
- 在**不可逆工具调用、session commit、outbound commit 前重新校验 owner/epoch**
- 拿不到锁：按顺序协议 park（不重投队尾，否则乘上中文顺序丢失）

**L3 存储层原子化（修掉根因，即使锁失效也不丢数据）**
- Redis：把"整对象覆盖"改为**追加语义**
  - events → `RPUSH session:{...}:events`（或 Stream `XADD`，天然带序号）
  - state → `HSET session:{...}:state field value`（按字段增量，不覆盖整 dict）
  - session meta → Lua 脚本 CAS：`if last_update_time == expected then update end`
  - Cluster 下用 hashtag：`session:{tenant|app:user:sid}:events` 保证同 slot 可用 Lua/MULTI
- SQL：`events` 表主键已含 `event.id` ⇒ `INSERT ... ON CONFLICT DO NOTHING` 天然幂等；
  `sessions` 增加 `revision` 列，更新用
  `UPDATE ... WHERE revision = :expected` + 影响行数为 0 则重读重试（乐观锁）
- **兼容策略**：新增 `RedisSessionServiceV2`（追加语义）与 `SqlSessionService(optimistic=True)`，
  由 `BackendSpec.options.append_mode` 开关，老实现保留，避免破坏现有用户与 498 个测试文件

**L4 冲突可检测**：`events` 唯一约束 + `seq` 单调；写入冲突记 metric
`tenant_session_write_conflict_total`，非零即报警。

### 5.2 Session event / state / summary 的更新顺序

> ⚠️ **本小节描述的是目标语义，它在现有 `Runner` 接口下无法实现。**
> `Runner.run_async()` 自己做 `get_session`/`create_session`、`conversation_count += 1`、
> 构造 USER Event 并 `append_event`。若 Worker 预先写 USER event 再调 `run_async`，
> **用户输入会重复**；若不预写，则无法完成 Turn 事务与 seq 分配。
> **前置条件：先完成 §5.6 的 persisted-turn API。**

单轮次内**严格顺序**（violate 会导致"state 领先于 event"的历史不可重放）：

```
1. acquire session lock → 得到 fencing epoch（§5.1 L2）
2. 校验 ingress_seq == next_expected_seq，否则 park（§5.1 顺序协议）
3. begin_turn(envelope_id, expected_revision, epoch)   # §5.6
4. 确定性 ID：turn_id = invocation_id = UUID5(ns, envelope_id)
5. append USER event (seq=n, id=UUID5(ns, envelope_id+0))   ← 幂等，重放不重复
6. runner.run_persisted_turn(...) → 逐个 event：
     a. append event (seq=n+i, id=UUID5(ns, envelope_id+i))  # 先 event
     b. apply event.actions.state_delta                      # 后 state
        - extract_state_delta 分流 app:/user:/session   (复用 sessions/_utils.py)
        - app_state / user_state 跨 session 共享 → 必须单字段 HSET/UPSERT
     c. 不可逆工具调用前重新校验 epoch（防陈旧持有者）
7. commit_turn: 写入 revision/max_seq/next_expected_seq（带 epoch 校验的 CAS）
8. 写 POST_TURN_OUTBOX + OUTBOUND_OUTBOX（同事务）
9. release lock
10. post-turn（异步，幂等，携带不可变 event range）：
     memory:  从 [from_seq, to_seq] 原始事件写入   ← 必须先于 summary（§5.3）
     summary: 持久 lease 防重复计算 → CAS 发布 WHERE covers_up_to_seq < to_seq
```

**为什么 event 先于 state**：event 是事实（append-only、可重放），state 是投影。
先写 state 后崩溃 → state 领先于事件流，无法从 events 重建，且 `_summarizer_checker`
的阈值判断会错乱。反之只是丢一次投影，可由 events 重放修复。

**摘要的并发**：两个节点可能同时判定"该摘要了"（`should_summarize_session`）。
用 `covers_up_to_seq` 做 CAS：后到的若 `covers_up_to_seq >= n_max` 则丢弃自己的结果。
摘要**不进关键路径**（`Runner` 已有 `defer_post_turn_processing`），但需
`post_turn_pending` 表 + 巡检补偿，防节点被 kill 时丢摘要。

**Redis Streams 作为 event log（进阶选项）**：`XADD` 返回的 ID 天然单调且全局有序，
可直接作为 `seq`，并支持 `XRANGE` 增量读 ⇒ 比 List 更适合多节点。列为 Phase 3。

### 5.3 Memory 写入后的跨节点可见性

Memory 由 post-turn 异步写（可能还在另一个线程/另一个节点），必然存在窗口期。

> ⚠️ **先修一个真 bug**：`Runner._run_post_turn_processing()` 的顺序是
> `create_session_summary()` → `store_session()`。而 `SummarizerSessionManager.create_session_summary`
> 会**压缩 `session.events`**（只留 summary + 近期事件）并 `update_session()`。
> ⇒ **Memory 拿到的是已压缩的事件窗口，本轮原始事件会漏写长期记忆**。
> 修法：post-turn job 必须携带**不可变的事件区间**，而非可变 Session 快照：
>
> ```python
> PostTurnJob(tenant_id, app_name, user_id, session_id,
>             from_seq, to_seq, session_revision, kinds=["memory", "summary"])
> ```
> - Memory 从**原始 event range** 读取写入，不依赖压缩后快照；且先 memory 后 
summary
> - Summary 用**持久 lease** 避免两节点重复跑昂贵的 LLM 摘要（`covers_up_to_seq` 的 CAS
>   只能防旧摘要发布，**防不住重复计算与重复计费**）
> - 摘要发布与 Session active window 重写必须属于**同一个版本协议**（同一 revision CAS）
> - 当前 Summary metadata 只存在 `_summarizer_cache` **进程内**⇒ 必须落
>   `session_summaries` 并定义 `get_session()` 如何组合 summary anchor + 近期事件

| 需求 | 方案 |
|---|---|
| 同租户同用户"读己所写" | 在 session state 写 `user:memory_watermark = seq`；读取时若后端最大 seq < watermark 则**读修复**。⚠️ 当前 `MemoryServiceABC.search_memory()` **返回值不包含最大 seq**，无法直接对比 ⇒ 需扩展 Memory 接口或增加独立 ingestion ledger |
| 关键记忆需立即可见 | `MemoryPolicy.sync_write_on_flag`：事件带 `important` 标记时**同步**写 memory（在锁内），代价是延迟 |
| 一般记忆 | 接受最终一致（秒级）。文档明示语义，避免用户误解 |
| 外部服务（Mem0/Mempalace） | 延迟不可控 ⇒ 一律异步 + 本地 outbox 表；失败重试 3 次后进死信并告警。**绝不阻塞用户回复** |
| 幂等 | `store_session` 以 `event.id` 为主键 upsert（`mem_events.id` 已是 PK）⇒ 重复投递自动收敛 |

### 5.4 后端迁移方案

统一走**五阶段影子迁移**，由 `MigrationPlan` 驱动，**按租户逐个切换**：

```python
class MigrationPlan(BaseModel):
    kind: Literal["session", "memory", "artifact", "knowledge"]
    source: BackendSpec
    target: BackendSpec
    phase: Literal["idle", "dual_write", "backfill", "shadow_read", "cutover", "done"]
    started_at: float
    backfill_cursor: str = ""     # 断点续传游标
    mismatch_tolerance: float = 0.001
```

阶段：
1. **dual_write**：写 source + target（target 失败只记 metric 不影响主链路）；读仍走 source
2. **backfill**：按 `(app_name, user_id)` 分片 + 游标批量回填历史；限速避免打满源库；
   已存在则跳过（幂等）。可断点续传
3. **shadow_read**：读 source 返回给用户，同时异步读 target 比对，采样上报
   `migration_mismatch_ratio`。低于阈值持续 N 小时才放行
4. **cutover**：`phase=cutover` → 读写切 target，source 降为只写备份

> ⚠️ **修正："Gateway 冻结几秒"不构成屏障。**队列里还有旧消息；Worker 有在途 Invocation；
> Cron/Admin/A2A 等入口可绕过 Gateway；post-turn job 仍可能写旧后端；
> 配置缓存失效不是同时发生的。
>
> **必须引入真正的 write barrier 协议**：
> tenant migration epoch → 所有入口统一检查 barrier → 入队 watermark →
> Worker drain watermark → **在途 Turn 数归零确认** → post-turn/outbox drain →
> source/target 双写序号对账 → 原子切换生效 epoch → **旧 epoch Worker 写入被拒绝**
5. **done**：停止双写，保留 source 只读 T 天后清理

**Redis → SQL 特化**：
- Redis 的 TTL 语义 SQL 没有 ⇒ 回填时按 `update_time + ttl_seconds` 过滤掉已过期数据
  （`_sql_session_service.py::_expire_before` 已有等价逻辑，复用）
- Redis pickle/JSON 序列化差异：`storage/_redis.py::_serialize_value` 与
  `_sql.py` 的 `DynamicJSON` / `DynamicPickleType` 不同 ⇒ 必须经
  `Session`/`Event` pydantic 模型往返，**不做二进制搬运**
- Redis 无 schema ⇒ 回填时会撞 SQL 的字段长度限制（`DEFAULT_MAX_KEY_LENGTH`）⇒ 预扫描报告超长键

**本地向量库 → 远端向量库特化**：
- 若 embedding 模型不变：导出 `(id, vector, payload)` 直接批量 upsert，最快
- 若 embedding 模型变了：**必须重新 embed**，成本 = 文档数 × embedding 单价，需预算审批
- 索引构建异步 ⇒ cutover 前必须校验 `count(target) == count(source)` 且抽样 recall@k 达标
- `KnowledgeFilterExpr` 的过滤语法在不同 vectorstore 间不等价 ⇒ 在
  `knowledge/_filter_expr.py` 层做能力探测，不支持则拒绝迁移而非静默降级

**回滚**：任何阶段（除 done）都可把 `phase` 退回，因为 source 一直是完整的。
cutover 后回滚窗口 = source 保留期。

### 5.5 IM 消息重复投递的幂等处理

IM 平台在超时/5xx 时会重投（企业微信、公众号通常 3 次，间隔递增），必须幂等。

**三层幂等**：

> ⚠️ **阻断级修正：去重与入队之间有消息永久丢失窗口。**
> 初版流程是 `SET NX dedup=PROCESSING` → 发布队列 → ACK，两步之间**没有原子边界**：
> Gateway 在标记 `PROCESSING` 后、入队前崩溃 ⇒ 平台重试时看到 `PROCESSING` → 返回空 ACK
> → **而队列里从未出现过这条消息** ⇒ 静默丢失（且 TTL 过期后平台未必再重投）。
> 反之（先入队后标记）则崩溃会造成重复入队。
>
> **必须建立持久且可恢复的边界**，三选一：
> 1. **SQL Inbox + 事务 Outbox**（推荐）：同一事务内写入向消息 + 待发布事件，
>    由 Relay 投递队列；崩溃后 Relay 自动重试
> 2. **Redis Lua 原子操作**：去重 key 与 Stream 在同一 Redis/slot，单个 Lua 内完成
>    去重标记 + `XADD`
> 3. 支持事务生产者的 MQ + 持久 Inbox + 幂等消费者
>
> 且 `PROCESSING` 不能只靠 24h TTL，必须有 **租约 / owner / 最后心跳 / 可接管规则**：
> ```text
> RECEIVED → ENQUEUED → CLAIMED → SESSION_COMMITTED → OUTBOUND_COMMITTED → DONE
> ```

**第 1 层 — Gateway 入口去重（挡住 99%，但必须配合上述原子边界）**
```python
key = f"dedup:{tenant_id}:{channel_id}:{external_msg_id}"
# SET NX 原子占位，返回状态机
state = await redis.set(key, PROCESSING, nx=True, ex=86400)
if not state:                       # 已存在
    prev = await redis.get(key)
    if prev == DONE:      return cached_reply_or_empty_ack()   # 直接返回，不重跑
    if prev == PROCESSING: return empty_ack()                  # 在途，让平台别等
    if prev == FAILED:     ... # 允许重试：CAS 改回 PROCESSING
```
- `external_msg_id` 取值优先级：平台 msg_id > `(from_user, create_time, md5(content))`。
  部分平台（如公众号某些消息类型）无稳定 msg_id ⇒ 用后者兜底
- Redis 不可用时**降级到 SQL 唯一约束**（`channel_inbound_dedup` 表
  `UNIQUE(tenant_id, channel_id, external_msg_id)`，`INSERT` 冲突即重复）。
  绝不"Redis 挂了就放行"（那会导致重复对话 + 重复计费）

**第 2 层 — 队列消息幂等**
`MessageEnvelope.envelope_id = uuid5(NAMESPACE, f"{tenant}:{channel}:{msg_id}")`（确定性）。
Worker 侧再查一次 `processed:{envelope_id}`；消费失败重投时同一 envelope_id 不会重复落 event。

**第 3 层 — 存储层幂等（需确定性 ID 才成立）**

> ⚠️ **修正：存储层主键无法让 Runner 重放变幂等。**
> 初版称"`events.id` 主键能挡住重复执行"。但 `Event.new_id()` 是 `str(uuid.uuid4())`，
> `Runner` 重跑时会生成**全新的 invocation_id 与 event_id** ⇒ 同一 `envelope_id` 重放
> 被当作全新事件，唯一约束形同虚设。即便 event 去重成功，也不能消除副作用：
> 重复模型调用与计费、重复执行外部工具、重复 Artifact 版本、重复发 IM。
>
> **必须为一次外部消息建立稳定身份**：
> ```text
> turn_id       = UUID5(ns, envelope_id)
> invocation_id = UUID5(ns, envelope_id)
> event_id      = UUID5(ns, envelope_id + logical_event_index)
> tool_call_id  = 持久化后生成，或由稳定输入确定
> ```
> - 副作用工具需支持 `idempotency_key`，并记 **tool execution ledger**
> - Artifact 用 `(artifact_id, content_hash, operation_id)` 去重
> - `processed:{envelope_id}` 存典型问题：执行前标记→崩溃可能永不执行；
>   执行后标记→崩溃可能重复副作用。因此必须配合 Turn 状态机（§5.6）而非单独使用
> - **语义定位：本系统只能提供 at-least-once + 幂等收敛，不得笼统宣称 exactly-once。**
>   对不支持幂等键的 IM 平台，"平台发送成功但本地状态更新失败"仍可能重复发送

- `events` 表 PK 含 `event.id`；**配合上述 UUID5 确定性 ID 后**重复插入被唯一约束挡住
- `mem_events.id` 同理
- Artifact 用 `(artifact_id, content_hash, operation_id)` 判重

**出向幂等**（同样重要，否则用户看到重复回复）：
`outbound_deliveries` 表 `UNIQUE(tenant_id, channel_id, chat_id, idempotency_key)`，
`idempotency_key = f"{envelope_id}:{chunk_index}"`。Channel Adapter 投递前占位，
投递成功标 DONE；重试时命中 DONE 直接跳过。

---

### 5.6 Turn 状态机与 Runner persisted-turn API（新增，先于一切实现）

§5.2 描述的顺序在现有 `Runner` 接口下**无法实现**（见 §0 修正）。先定义语义，再写代码。

**系统保证（必须写在文档开头，避免过度承诺）**

| 层 | 保证 |
|---|---|
| 入向队列 | at-least-once |
| Session event | 确定性 ID + 幂等追加 |
| Session state | revision / epoch CAS |
| 工具副作用 | 尽力通过 idempotency key 收敛，**不保证外部系统 exactly-once** |
| outbound | at-least-once；平台不支持幂等时存在极小重复窗口 |
| Memory / Knowledge | 最终一致 |
| Summary | 异步、可重建、单调发布 |

**Turn 状态机**

```text
INBOX_RECEIVED
  → QUEUED
  → TURN_CLAIMED(epoch, owner, lease)
  → USER_EVENT_COMMITTED(turn_id, seq)
  → RUNNING  ──┐
  │                ├→ TOOL_LEDGER_PREPARED → TOOL_EXECUTED
  → SESSION_COMMITTED(revision, max_seq)
  ──┐
  │ └→ POST_TURN_OUTBOX
  → OUTBOUND_OUTBOX
  → DONE
```
每个转换需：expected previous state、确定性 turn/envelope ID、fencing epoch、
持久 timestamp、crash recovery owner、retry policy。

**所需的 SDK 接口变更**

```python
# 方案 A（推荐）：显式 Turn API
turn = await session_service.begin_turn(envelope_id=..., expected_revision=..., epoch=...)
async for event in runner.run_persisted_turn(turn=turn, new_message=msg):
    ...
await session_service.commit_turn(turn)

# 方案 B（改动更小）：给 Runner 增加“USER event 已持久化”入口
runner.run_async(..., user_event_already_persisted=True, invocation_id=deterministic_id)
```

必须在 ADR 中明确职责归属：谁创建 invocation_id、谁分配 event seq、
谁判定 Turn 已开始/已提交、Worker 崩溃后谁恢复、`Runner` 是否允许同一 Envelope 重执行。

### 5.7 跨节点取消（新增）

`cancel/_cancel.py::_RunCancellationManager` 是 `SingletonBase` + `Dict[SessionKey, asyncio.Event]`，
**进程内**。后果：
- `/stop` 若由另一 Worker 处理，**无法取消正在执行的节点**
- 更糟：若 `/stop` 与普通消息进同一个严格串行的 session 队列，
  它会被阻塞到当前长任务结束，**完全丧失取消意义**

**对策**：新增分布式 control channel（Redis Pub/Sub `control:{tenant}:{session}`）；
取消消息走**高优先旁路（不进有序队列）**，由正在执行的节点订阅并触发本地 cancel。
同理，openclaw WeCom 的 `_active_stream_ids` / frame 映射也是进程内，
需持久化 stream/edit correlation，或明确单次 outbound stream 的适配器亲和性与故障降级策略。

## 6. 最小数据模型 / 表结构

> ⚠️ **定位修正：以下是数据模型草图，不是"可部署的最小表结构"。**
> 对抗性评审指出初版 DDL 有多处在 PostgreSQL 上**无法执行或无法提供预期约束**，
> 已在下方逐项修正（内联 `INDEX`、表达式主键、分区表主键未含分区键、
> 缺子分区/default 分区、`chat_id` 规范不一致、部分新表缺 `tenant_id`、
> 长度限制与注释不一致、`events.seq` 可 NULL 导致唯一索引形同虚设、
> `UNIQUE(kind, platform_key)` 的 NULL 语义、缺复合外键）。
> **落地时必须转写为真实 Alembic migration 并在真实 PostgreSQL 上跑通**，
> 测试空库创建/旧库升级/回滚/并发索引/分区轮转，并区分 PG/MySQL/SQLite 支持范围。

以 PostgreSQL 方言给出。**新增表全部带 `tenant_id` 列**；SDK 自有表（`sessions`/`events`/
`app_states`/`user_states`/`mem_events`）**不改结构**（除 §5.1/§5.6 所需的 revision/seq/epoch），
靠 `app_name = '<tenant>|<app>'` 做命名空间 + §4.4 facade/RLS 做安全边界。

```sql
-- ============ 1. 租户 ============
CREATE TABLE tenants (
    tenant_id        VARCHAR(64)  PRIMARY KEY,
    display_name     VARCHAR(255) NOT NULL,
    status           VARCHAR(16)  NOT NULL DEFAULT 'active',   -- active/suspended/readonly/deleting
    revision         INT          NOT NULL DEFAULT 1,
    release_channel  VARCHAR(16)  NOT NULL DEFAULT 'stable',   -- stable/canary
    config           JSONB        NOT NULL,                    -- TenantConfig 快照（仅含 SecretRef，无明文）
    labels           JSONB        NOT NULL DEFAULT '{}',
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT tenants_id_fmt CHECK (tenant_id ~ '^[a-z0-9][a-z0-9-]{2,63}$')  -- 禁用 '|'
);

-- 配置版本（灰度与回滚的依据）
CREATE TABLE config_revisions (
    tenant_id    VARCHAR(64) NOT NULL REFERENCES tenants(tenant_id),
    revision     INT         NOT NULL,
    config       JSONB       NOT NULL,
    diff_summary TEXT,
    author       VARCHAR(128) NOT NULL,
    reason       TEXT,
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    rolled_back_from INT,
    PRIMARY KEY (tenant_id, revision)
);

-- 密钥引用（只存引用与元数据，绝不存值）
CREATE TABLE tenant_secrets (
    tenant_id  VARCHAR(64)  NOT NULL REFERENCES tenants(tenant_id),
    name       VARCHAR(128) NOT NULL,          -- 'model.default.api_key'
    provider   VARCHAR(32)  NOT NULL,          -- env/vault/k8s/kms
    ref_key    VARCHAR(512) NOT NULL,
    ref_version VARCHAR(64),
    rotated_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, name)
);

-- ============ 2. Agent 应用 ============
CREATE TABLE agent_apps (
    tenant_id     VARCHAR(64)  NOT NULL REFERENCES tenants(tenant_id),
    app_id        VARCHAR(64)  NOT NULL,
    app_name      VARCHAR(191) NOT NULL,       -- 物化 '<tenant_id>|<app_id>'，与 SDK 表 JOIN 用
    agent_factory VARCHAR(255) NOT NULL,
    model_alias   VARCHAR(64)  NOT NULL DEFAULT 'default',
    run_config    JSONB        NOT NULL DEFAULT '{}',
    tool_policy   JSONB        NOT NULL DEFAULT '{}',
    enabled       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, app_id),
    UNIQUE (app_name),
    CONSTRAINT agent_apps_id_fmt CHECK (app_id ~ '^[a-z0-9][a-z0-9_-]{1,63}$')
);

-- ============ 3. IM 通道绑定 ============
CREATE TABLE channel_bindings (
    tenant_id      VARCHAR(64)  NOT NULL REFERENCES tenants(tenant_id),
    channel_id     VARCHAR(64)  NOT NULL,
    app_id         VARCHAR(64)  NOT NULL,
    kind           VARCHAR(32)  NOT NULL,      -- wecom/wecom_kf/wechat_mp/telegram/http
    route_key      VARCHAR(64)  NOT NULL,      -- webhook URL 中的不可枚举随机串
    platform_key   VARCHAR(191),               -- corp_id:agent_id / bot_id / mp_original_id（回退路由）
    enabled        BOOLEAN      NOT NULL DEFAULT TRUE,
    token_ref      VARCHAR(256),               -- → tenant_secrets.name
    secret_ref     VARCHAR(256),
    credential_refs JSONB       NOT NULL DEFAULT '{}',
    group_session_policy VARCHAR(24) NOT NULL DEFAULT 'per_group',
    reply_mode     VARCHAR(16)  NOT NULL DEFAULT 'chunked',
    rate_limit     JSONB        NOT NULL DEFAULT '{}',
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, channel_id),
    UNIQUE (route_key),                        -- 第一路由：URL → 唯一租户
    UNIQUE (kind, platform_key),               -- 第二路由：平台标识 → 唯一租户
    FOREIGN KEY (tenant_id, app_id) REFERENCES agent_apps(tenant_id, app_id)
);

-- IM 外部身份 → 内部 user_id 映射（跨租户绝不复用）
CREATE TABLE channel_user_bindings (
    tenant_id         VARCHAR(64)  NOT NULL,
    channel_id        VARCHAR(64)  NOT NULL,
    external_user_hmac CHAR(64)    NOT NULL,   -- 修正：tenant-scoped HMAC-SHA256，非 SHA-1
    identity_key_version INT       NOT NULL DEFAULT 1,   -- 支持密钥轮换
    user_id           VARCHAR(191) NOT NULL,   -- SDK user_id
    display_name_enc  BYTEA,                   -- 昵称加密存储（可选）
    role              VARCHAR(32)  NOT NULL DEFAULT 'member',  -- IM 侧权限
    bound_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, channel_id, external_user_hmac),
    -- 修正：PG 不支持表定义内联 INDEX，拆为独立 CREATE INDEX
    FOREIGN KEY (tenant_id, channel_id) REFERENCES channel_bindings(tenant_id, channel_id) ON DELETE CASCADE
);
CREATE INDEX idx_cub_user ON channel_user_bindings (tenant_id, user_id);

-- ============ 4. 入向去重（Redis 的 SQL 兜底 / 审计追溯）============
CREATE TABLE channel_inbound_dedup (
    tenant_id        VARCHAR(64)  NOT NULL,
    channel_id       VARCHAR(64)  NOT NULL,
    external_msg_id  VARCHAR(191) NOT NULL,
    envelope_id      UUID         NOT NULL,
    session_id       VARCHAR(191) NOT NULL,
    state            VARCHAR(16)  NOT NULL,    -- received/enqueued/claimed/committed/done/failed
    owner            VARCHAR(128),             -- 租约持有者（§5.5 状态机）
    lease_expires_at TIMESTAMPTZ,
    last_heartbeat   TIMESTAMPTZ,
    received_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    completed_at     TIMESTAMPTZ,
    -- 修正：PG 要求分区表的唯一/主键约束必须包含全部分区键
    PRIMARY KEY (tenant_id, channel_id, external_msg_id, received_at)
) PARTITION BY RANGE (received_at);
-- 修正：必须创建日分区 + default 分区，否则插入直接失败
CREATE TABLE channel_inbound_dedup_default PARTITION OF channel_inbound_dedup DEFAULT;

-- ============ 5. Session（SDK 表，此处仅示意与新增字段）============
-- 现有 sessions(app_name, user_id, id) PK / events(id, app_name, user_id, session_id) PK 不变
-- 新增：并发控制与顺序保证所需的最小扩展
ALTER TABLE sessions ADD COLUMN revision    BIGINT NOT NULL DEFAULT 0;  -- 乐观锁
ALTER TABLE sessions ADD COLUMN max_seq     BIGINT NOT NULL DEFAULT 0;  -- 已分配的最大 event 序号
ALTER TABLE sessions ADD COLUMN fence_epoch BIGINT NOT NULL DEFAULT 0;  -- §5.1 真 fencing
ALTER TABLE sessions ADD COLUMN next_expected_seq BIGINT NOT NULL DEFAULT 1;  -- §5.1 顺序协议
ALTER TABLE events   ADD COLUMN seq         BIGINT;                     -- 会话内单调序号
-- 修正：seq 可 NULL 时唯一索引形同虚设（PG 允许多行 NULL）。
-- 回填存量后置 NOT NULL；过渡期用部分索引并单独监控 seq IS NULL 的行数
CREATE UNIQUE INDEX uq_events_seq ON events (app_name, user_id, session_id, seq)
    WHERE seq IS NOT NULL;
-- 运维查询用（按租户前缀扫）
CREATE INDEX idx_sessions_tenant ON sessions ((split_part(app_name, '|', 1)), update_time);

-- ============ 6. Summary ============
CREATE TABLE session_summaries (
    tenant_id         VARCHAR(64)  NOT NULL,   -- 修正：补上遗漏的 tenant_id
    app_name          VARCHAR(191) NOT NULL,
    user_id           VARCHAR(191) NOT NULL,
    session_id        VARCHAR(191) NOT NULL,
    summary_text      TEXT         NOT NULL,
    covers_up_to_seq  BIGINT       NOT NULL,   -- CAS 依据：只允许单调前进
    token_count       INT          NOT NULL DEFAULT 0,
    model             VARCHAR(128),
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (app_name, user_id, session_id)
);

-- 后台补偿任务（防节点被 kill 丢摘要/记忆）
CREATE TABLE post_turn_pending (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   VARCHAR(64)  NOT NULL,         -- 修正：补上遗漏的 tenant_id
    app_name    VARCHAR(191) NOT NULL,
    user_id     VARCHAR(191) NOT NULL,
    session_id  VARCHAR(191) NOT NULL,
    kind        VARCHAR(16)  NOT NULL,         -- summary/memory
    -- 修正（§5.3）：携带不可变事件区间，而非可变 Session 快照
    from_seq    BIGINT       NOT NULL,
    to_seq      BIGINT       NOT NULL,
    session_revision BIGINT  NOT NULL,
    lease_owner VARCHAR(128),                  -- 防两节点重复跑昂贵的 LLM 摘要
    lease_expires_at TIMESTAMPTZ,
    attempts    INT          NOT NULL DEFAULT 0,
    next_try_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (app_name, user_id, session_id, kind, from_seq, to_seq)
);

-- ============ 7. Memory（SDK mem_events 表已存在，示意其租户归属）============
-- mem_events.save_key = user_key(app_name, user_id) 已含 tenant
CREATE INDEX idx_mem_tenant ON mem_events ((split_part(save_key, '|', 1)), timestamp);

-- ============ 8. Artifact 元数据（载荷在 S3）============
CREATE TABLE artifacts (
    tenant_id    VARCHAR(64)  NOT NULL,        -- 修正：补上遗漏的 tenant_id
    app_name     VARCHAR(191) NOT NULL,
    user_id      VARCHAR(191) NOT NULL,
    -- 修正：PG 主键不能含 COALESCE 表达式；用非空哨兵值 '' 代表 user 级
    session_id   VARCHAR(191) NOT NULL DEFAULT '',
    filename     VARCHAR(512) NOT NULL,
    version      INT          NOT NULL,
    operation_id VARCHAR(191),                 -- 幂等：稳定操作 ID
    canonical_uri TEXT        NOT NULL,        -- s3://bucket/tenant/app/user/sess/file/v1
    mime_type    VARCHAR(128),
    size_bytes   BIGINT       NOT NULL DEFAULT 0,
    content_hash CHAR(64),                     -- sha256，用于幂等去重
    custom_metadata JSONB     NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (app_name, user_id, session_id, filename, version)
);
CREATE UNIQUE INDEX uq_artifact_op ON artifacts (app_name, user_id, session_id, filename, operation_id)
    WHERE operation_id IS NOT NULL;

-- ============ 9. 出向投递（幂等 + 重试）============
CREATE TABLE outbound_deliveries (
    tenant_id       VARCHAR(64)  NOT NULL,
    channel_id      VARCHAR(64)  NOT NULL,
    -- 修正：文本称唯一键含 chat_id，故将其纳入主键（与 §5.5 表述一致）
    chat_id         VARCHAR(191) NOT NULL,
    idempotency_key VARCHAR(191) NOT NULL,     -- '{envelope_id}:{chunk_index}'
    payload_kind    VARCHAR(16)  NOT NULL,     -- text/stream/card/image/file
    state           VARCHAR(16)  NOT NULL,     -- pending/sent/failed/dropped
    attempts        INT          NOT NULL DEFAULT 0,
    last_error      VARCHAR(512),
    platform_msg_id VARCHAR(191),              -- 用于撤回
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    sent_at         TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, channel_id, chat_id, idempotency_key)
);

-- ============ 10. 审计日志 ============
CREATE TABLE audit_logs (
    id            BIGSERIAL,
    ts            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    tenant_id     VARCHAR(64)  NOT NULL,
    channel       VARCHAR(32),
    channel_id    VARCHAR(64),
    user_id       VARCHAR(191),
    external_user_hash CHAR(40),
    session_id    VARCHAR(191),
    app_id        VARCHAR(64),
    agent_name    VARCHAR(128),
    tool_name     VARCHAR(128),
    action        VARCHAR(64)  NOT NULL,       -- im.inbound / model.call / tool.call / im.outbound / admin.update
    decision      VARCHAR(24)  NOT NULL,       -- allow/deny/redacted/confirm_required/degraded/rate_limited
    decision_reason VARCHAR(255),
    latency_ms    INT,
    error_type    VARCHAR(64),
    error_message VARCHAR(512),                -- 已脱敏
    input_tokens  INT          NOT NULL DEFAULT 0,
    output_tokens INT          NOT NULL DEFAULT 0,
    cost_usd      NUMERIC(12,6) NOT NULL DEFAULT 0,
    trace_id      CHAR(32),
    span_id       CHAR(16),
    envelope_id   UUID,
    revision      INT,                          -- 生效的配置版本，便于回溯"当时的策略"
    payload_digest CHAR(64),                    -- sha256(内容)，默认不存原文
    payload_excerpt TEXT,                       -- 仅当 audit.record_payload=true 且已脱敏
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);
CREATE INDEX idx_audit_tenant_ts ON audit_logs (tenant_id, ts DESC);
CREATE INDEX idx_audit_trace     ON audit_logs (trace_id);
CREATE INDEX idx_audit_session   ON audit_logs (tenant_id, session_id, ts DESC);

-- ============ 11. 用量与配额（Redis 热计数 + 此表对账）============
CREATE TABLE usage_daily (
    tenant_id     VARCHAR(64) NOT NULL,
    day           DATE        NOT NULL,
    app_id        VARCHAR(64) NOT NULL DEFAULT '',
    model         VARCHAR(128) NOT NULL DEFAULT '',
    requests      BIGINT      NOT NULL DEFAULT 0,
    input_tokens  BIGINT      NOT NULL DEFAULT 0,
    output_tokens BIGINT      NOT NULL DEFAULT 0,
    tool_calls    BIGINT      NOT NULL DEFAULT 0,
    cost_usd      NUMERIC(14,6) NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, day, app_id, model)
);
```

> **迁移工具**：仓库当前无 Alembic（`storage/_sql.py` 用 metadata 建表）。
> 本特性必须引入版本化迁移（Alembic 或手写编号 DDL + `schema_versions` 表），
> 否则 `ALTER TABLE sessions ADD COLUMN` 这类变更无法安全上线。列为 Phase 1 前置项。

---

## 7. IM 软件接入

### 7.1 Channel Adapter 抽象

选定**至少两类**并优先实现：**企业微信（智能机器人 / 应用消息）** 与 **Telegram**
（理由：`server/openclaw/channels/` 已有 nanobot 基座与流式实现，可直接演进），
其后补 **微信客服** 与 **微信公众号**。

```
trpc_agent_sdk/server/multitenant/channels/
├── __init__.py
├── _abc.py             # ChannelAdapterABC
├── _envelope.py        # MessageEnvelope / OutboundChunk
├── _registry.py        # kind → adapter 类
├── _wecom.py           # 企业微信（复用 openclaw/_wecom.py 的 reply_stream 思路）
├── _wecom_kf.py        # 微信客服
├── _wechat_mp.py       # 微信公众号（被动回复 + 客服消息）
├── _telegram.py        # Telegram
└── _limits.py          # 限频、分片、重试、退避
```

```python
class ChannelAdapterABC(ABC):
    kind: ClassVar[str]

    @abstractmethod
    async def verify(self, req: HttpRequest, binding: ResolvedBinding) -> None:
        """验签；失败抛 ChannelAuthError（返回 401，且不泄露原因细节）。"""

    @abstractmethod
    async def parse(self, req: HttpRequest, binding: ResolvedBinding) -> list[MessageEnvelope]:
        """IM 原始报文 → 归一化 envelope（可能一个请求含多条）。"""

    @abstractmethod
    def ack_response(self, envelopes: list[MessageEnvelope]) -> HttpResponse:
        """平台要求的同步应答（空串 / challenge 回显 / 加密包）。"""

    @abstractmethod
    async def render(self, event: Event, ctx: RenderContext) -> list[OutboundChunk]:
        """Agent Event → IM 载荷（文本/流式增量/卡片/图片/文件）。"""

    @abstractmethod
    async def deliver(self, chunk: OutboundChunk, binding: ResolvedBinding) -> DeliveryResult:
        """实际投递；返回 platform_msg_id 供撤回/编辑。"""
```

### 7.2 消息转换

**入向：IM → tRPC-Agent 用户输入**

```python
class MessageEnvelope(BaseModel):
    envelope_id: UUID                 # uuid5 确定性，见 §5.5
    tenant_id: str
    app_id: str
    channel_id: str
    channel_kind: str
    external_msg_id: str
    external_user_id_hash: str        # 不带明文
    user_id: str                      # 映射后的内部 ID
    session_id: str                   # 见 §7.4
    chat_id: str                      # 平台回复所需（群/单聊 ID）
    is_group: bool
    content: Content                  # SDK 的 Content（text/image/file parts）
    attachments: list[AttachmentRef]  # 大文件先落 Artifact，只传引用
    traceparent: str                  # W3C trace 上下文，串起全链路
    received_at: float
    platform_meta: dict[str, Any]     # 回复所需的平台原始字段（如 wecom frame 信息）
```

转换规则：
- 文本 → `Content(parts=[Part(text=...)])`
- 图片/文件 → **先下载到 Artifact**（S3），再以 `Part` + `canonical_uri` 引用传入。
  绝不把二进制塞进队列（队列体积应 < 64KB）
- 语音 → 若配置 ASR 则转文本，否则回复"暂不支持"
- 富文本/卡片点击回调 → 映射为结构化 `Part`（`function_response` 语义），用于按钮式二次确认
- `@机器人` 前缀、`/命令` → 复用 `openclaw/channels/_command_handler.py` 的
  `/new` `/stop` `/help` 语义，在 Worker 侧优先拦截（不进 LLM）

**出向：Agent Event → IM 回复**

| Event 特征 | IM 表现 |
|---|---|
| `partial=True` 文本增量 | 企业微信：`reply_stream(frame, stream_id, content, finish=False)`（`_wecom.py` 已实现）；Telegram：**节流后** `editMessageText`（≥1s 间隔，避免 429） |
| `turn_complete=True` | 关闭流（`finish=True`）/ 最后一次 edit；标记 delivery=sent |
| 工具调用中间态 | 可选"正在查询…"progress 消息，`metadata["_progress"]=True`（`_wecom.py` 已按此约定过滤） |
| 结构化输出 / 长结果 | 卡片消息（企业微信模板卡片、Telegram InlineKeyboard） |
| 生成的文件/图片（Artifact） | 上传平台媒体接口 → 图片/文件消息；超限则给预签名下载链接 |
| `error_code` 非空 | 用户友好文案（**不暴露内部错误**）+ 完整信息只入审计与 trace |
| 需二次确认的危险工具 | 卡片 + 确认/取消按钮 → 回调映射为 `function_response`，恢复 long-running 工具 |

### 7.3 账号绑定与安全

| 项 | 设计 |
|---|---|
| **Webhook URL** | `https://gw.example.com/im/{kind}/{route_key}`。`route_key` = 22 字符随机（≈128bit），不可枚举、可轮换（轮换期双 key 并存 7 天） |
| **Token / Secret** | 仅存 `SecretRef`；`tenant_secrets` 记引用。轮换支持"新旧双活"窗口，避免配置下发瞬间验签失败 |
| **回调验签** | 企业微信：`msg_signature = sha1(sort(token, timestamp, nonce, encrypt))` 校验 + AES-CBC 解密（`echostr` 首次校验）；公众号：同 sha1 三元组；Telegram：校验 `X-Telegram-Bot-Api-Secret-Token` header（webhook 时必设）。统一要求：**timestamp 偏移 > 5 分钟直接拒绝**（防重放）；比较用 `hmac.compare_digest`（防时序侧信道） |
| **消息去重** | §5.5 三层 |
| **用户身份映射** | `channel_user_bindings`：`(tenant, channel, sha1(external_user))` → 内部 `user_id`。首次可自动建（`require_user_binding=false`）或要求绑定码（true）。**跨租户永不复用 user_id** |
| **IM 用户权限** | `allowed_external_users` 白名单 + `role`；在 IM Ingress Filter 中校验（§8.1） |
| **回调 IP 白名单** | 可选：平台出口 IP 段 + WAF 限速，作为验签之外的一层 |

### 7.4 群聊 / 单聊 session_id 与隔离

规则见 §3.1。策略含义与选择建议：

| 策略 | session_id | 语义 | 适用 |
|---|---|---|---|
| `per_group`（默认） | `c:{ch}/g:{ghash}` | 群内所有人共享上下文，机器人像"群成员" | 团队协作助手 |
| `per_group_user` | `c:{ch}/g:{ghash}/u:{uhash}` | 群内每人独立上下文，互不可见 | 群里做私密查询（HR、财务） |
| `per_thread` | `c:{ch}/g:{ghash}/t:{tid}` | 按话题/回复串隔离 | 支持 thread 的平台 |
| 单聊 | `c:{ch}/u:{uhash}` | — | — |

**隔离要点（关键）**：
- `session_id` 里**必须**含 `channel_id`。同一个人在 A 通道和 B 通道是两个 session，
  防止"跨通道串上下文"（一个人可能同时是甲租户的员工和乙租户的客户）
- 完整键是 `(app_name = tenant|app, user_id, session_id)`，**tenant 在最外层**，
  所以跨租户碰撞在数学上不可能，即便 `session_id` 相同
- `per_group` 下群成员共享上下文 ⇒ 必须在 system prompt 里注明"当前为群聊，
  发言人是 X"，并**禁止**在群聊里返回用户级私密记忆（Memory 检索时按
  `is_group` 降级为仅 session 记忆）。这是易被忽略的隐私泄露路径
- 用户被移出群 / 通道解绑 ⇒ 触发 session 归档任务

### 7.5 平台限制与应对

| 限制 | 应对 |
|---|---|
| **同步响应 5s**（企业微信、公众号） | Gateway 只做验签+去重+入队，立即 ACK。回复走异步：公众号用客服消息接口（48h 内有交互才可）；企业微信用应用消息/流式回复 |
| **消息长度**（Telegram 4096 字符；企业微信 markdown 有上限） | `_limits.py` 按语义边界（段落 > 句子 > 硬切）分片，编号 `(1/3)`；代码块不跨片切断 |
| **频率限制**（Telegram 单聊 ~1 msg/s、全局 ~30 msg/s；企业微信各接口配额） | 令牌桶（Redis）**双层**：per-chat + per-tenant；流式 edit 强制 ≥1s 节流并合并增量；触发 429 则读 `retry_after` 退避 |
| **流式回复** | 企业微信 `reply_stream` 增量（已有）；Telegram `editMessageText` 合并；不支持的平台降级为 `reply_mode="chunked"`（攒够一段发一次） |
| **图片/文件** | 入向：下载→S3 Artifact→引用。出向：走平台媒体上传（有大小/格式/有效期限制，media_id 通常 3 天）；超限给预签名链接 |
| **撤回** | 保存 `platform_msg_id`；违规内容检出后调撤回接口（平台通常限 24h 内），失败则发更正消息 |
| **失败重试** | `outbound_deliveries` 状态机 + 指数退避（1s/4s/16s，最多 4 次）；4xx（除 429）不重试直接标 failed；重试受幂等键保护 |
| **平台侧重投** | §5.5 幂等 |
| **access_token 管理** | 企业微信/公众号 token 有效期 7200s 且**有并发刷新竞争** ⇒ Redis 分布式锁 + 提前 5min 刷新 + 共享缓存，避免每节点各刷把配额打满 |

---

## 8. 治理、监控与安全

### 8.1 Filter 治理策略

用现有 `FilterType.{TOOL, MODEL, AGENT}` 三类，配合两个 SDK 外的入口中间件。
所有策略从 `ctx.get_metadata("tenant_id")` 拿租户上下文（§2.2）。

| 策略 | 落点 | 实现要点 |
|---|---|---|
| **工具白名单** | ① `ToolPredicate`（`abc/_toolset.py::_is_tool_selected`）在装配期裁剪，LLM 根本看不到禁用工具；② `TenantToolGuardFilter`（TOOL）执行期硬拦截 | 双层防御。②必需：防止 prompt 注入伪造工具名、防 Agent-as-Tool 绕过。`default_action="deny"` |
| **敏感信息脱敏** | `RedactionFilter`（MODEL：出入两向；TOOL：工具入参/返回） | 规则集：手机号/身份证/银行卡/邮箱/密钥模式（`sk-`、`AKID`、JWT）。命中记 `decision=redacted` 审计。⚠️ **流式脱敏必须跨 chunk 缓冲**：密钥被拆成两个 chunk 时逐 chunk 正则会漏检（保留尾部 overlap 窗口） |
| **预算限制** | `BudgetFilter`（MODEL） | ⚠️ **修正：不能用"先读后写"**（并发下多个请求同时通过检查 ⇒ 穿透预算）。改为：调用前 **原子预留**（Lua）最大估算 token/cost → 完成后按实际 usage 结算并释放差额 → **失败/超时必须回收 reservation**。月预算不能只依赖日 Redis counter，需持久账本 + 对账。**货币不用 `float`**，用 Decimal 或整数微美元 |
| **单次调用上限** | `RunConfig.max_llm_calls` / `max_tool_calls` / `agent_limits`（已存在） | 直接由 `AgentAppConfig.run_config` 下发，无需新代码 |
| **危险工具二次确认** | `ConfirmationFilter`（TOOL） | 命中 `require_confirmation` 时不执行，转为 long-running 工具挂起（复用 `tools/_long_running_tool.py` / HITL），向 IM 推确认卡片；回调携带 `confirm_token`（短 TTL、绑定 session + tool_call_id，防重放） |
| **IM 用户权限校验** | **IM Ingress Middleware**（Gateway 侧，非 Filter） | Filter 类型不含 "channel"，且此校验应在入队**之前**完成以免浪费资源。校验 `allowed_external_users`、`role`、`require_user_binding`、租户 `status` |
| **租户状态门禁** | Ingress + `TenantGuardFilter`（AGENT） | `suspended` → 拒绝并回友好文案；`readonly` → 只读工具；`deleting` → 拒绝 |
| **出向内容审核** | Channel Adapter 前置钩子 | 调外部审核服务；违规则拦截并（若已发）撤回 |

组合方式（**顺序即优先级**，`filter/_run_filter.py` 按列表顺序嵌套）：
```python
# 装配 TenantRuntime 时
model_filters = ["tenant_guard", "budget", "redaction"]        # 先门禁→再预算→再脱敏
tool_filters  = ["tenant_tool_guard", "confirmation", "redaction", "audit"]
agent_filters = ["tenant_guard", "audit"]
```

### 8.2 监控指标

复用 `telemetry/_metrics.py` 已有的 OTel `gen_ai.*` 仪表（`gen_ai.request_cnt`、
`gen_ai.client.operation.duration`、`gen_ai.server.time_to_first_token`、
`gen_ai.usage.{input,output,cache_read,cache_creation}_tokens`），**只加维度**：
`tenant_id`、`app_id`、`channel_kind`、`release_channel`、`revision`。

> ⚠️ 基数控制：`tenant_id` 会显著放大时序基数。约束：
> ①绝不把 `user_id`/`session_id` 放进 metric label（只进 trace/log）；
> ②租户数 > 1000 时对长尾租户聚合为 `tenant_id="_other"`，明细走 trace/日志聚合。

新增仪表（`trpc_agent_sdk/server/multitenant/_metrics.py`）：

| 指标 | 类型 | 标签 | 用途 |
|---|---|---|---|
| `mt.im.inbound_total` | Counter | tenant, channel_kind, result(accepted/dup/rejected/authfail) | 请求量、去重率、攻击面 |
| `mt.im.callback_duration` | Histogram | tenant, channel_kind | 保障 ACK < 5s（P99 告警） |
| `mt.im.outbound_total` | Counter | tenant, channel_kind, state(sent/failed/dropped) | **IM 投递成功率** = sent/(sent+failed) |
| `mt.im.outbound_latency` | Histogram | tenant, channel_kind | 用户感知端到端延迟 |
| `mt.im.rate_limited_total` | Counter | tenant, channel_kind, scope(chat/tenant) | 限频命中 |
| `mt.queue.depth` / `mt.queue.lag_seconds` | Gauge | partition | 扩容依据（KEDA） |
| `mt.worker.turn_duration` | Histogram | tenant, app | 单轮总耗时 |
| `mt.session.backend_latency` | Histogram | tenant, backend_type, op(get/append/update) | **Session 后端延迟** |
| `mt.memory.backend_latency` | Histogram | tenant, backend_type, op | Memory 后端延迟 |
| `mt.session.lock_wait` / `mt.session.write_conflict_total` | Histogram/Counter | tenant | 并发争抢与丢更新检测（应为 0） |
| `mt.tool.duration` | Histogram | tenant, tool_name, outcome | **工具调用耗时**（`report_execute_tool` 已有，补 tenant 维度） |
| `mt.governance.decision_total` | Counter | tenant, policy, decision | 治理命中（deny/redact/confirm） |
| `mt.tenant.cost_usd` | Counter | tenant, model | **每租户成本** |
| `mt.tenant.budget_utilization` | Gauge | tenant, kind | 预算使用率，>80% 预警 |
| `mt.config.revision` | Gauge | tenant | 各节点生效版本（检测配置漂移） |
| `mt.migration.mismatch_ratio` | Gauge | tenant, kind | 迁移校验 |
| `mt.degraded_mode` | Gauge | tenant, component | 是否处于降级 |

**错误率**：`mt.errors_total{tenant, stage(ingress/worker/model/tool/storage/outbound), error_type}`。
SLO：可用性（非 5xx 比例）、IM 投递成功率 ≥ 99.5%、首字延迟 P95。

### 8.3 OpenTelemetry 全链路追踪

现状：`telemetry/_trace.py` 已有 `trace_runner` / `trace_agent` / `trace_tool_call` /
`trace_call_llm`；`server/langfuse/tracing/opentelemetry.py` 可导出到 Langfuse。
**缺口**：IM callback 无 span、Session/Memory IO 无 span、跨队列无上下文传播。

目标 trace 形态（一个 trace_id 串起全链路）：

```
span: im.callback                     [Gateway]  attrs: tenant_id, channel_kind, dedup_result
 ├─ span: im.verify_signature
 ├─ span: tenant.resolve
 ├─ span: im.dedup_check
 └─ span: mq.publish                             ← 注入 traceparent 到 MessageEnvelope
 ......................（进程 / 时间边界）..........................
span: mq.consume                      [Worker]   parent = mq.publish（跨进程 remote context）
 ├─ span: tenant.runtime_load                    attrs: cache_hit, revision
 ├─ span: session.get                            attrs: backend=redis, tenant_id  ← 新增
 ├─ span: session.lock_acquire                   attrs: wait_ms
 ├─ span: invoke_agent  ← trace_runner/trace_agent（已有，补 tenant attrs）
 │   ├─ span: call_llm  ← trace_call_llm（已有）
 │   ├─ span: execute_tool  ← trace_tool_call（已有）
 │   │   └─ span: governance.decision            attrs: policy, decision  ← 新增
 │   └─ span: session.append_event               ← 新增
 ├─ span: memory.store / memory.search           ← 新增
 ├─ span: session.summarize
 └─ span: im.deliver                  [Adapter]  attrs: chunk_index, attempts, platform_msg_id
     └─ span: im.platform_api_call               attrs: http.status_code, retry_after
```

实现要点：
1. **跨队列传播**：`MessageEnvelope.traceparent`（W3C）。Gateway 用
   `TraceContextTextMapPropagator().inject(carrier)`；Worker `extract` 后
   以 `links=[Link(remote_ctx)]` 或直接作 parent 开 span。
   **必须显式做** —— OTel 自动埋点不覆盖自定义队列
2. **统一 attribute 集**（新建 `_span_attrs.py` 常量，避免各处写错 key）：
   `tenant.id`、`tenant.revision`、`agent.app_id`、`im.channel_kind`、`im.channel_id`、
   `session.id`、`session.backend`、`envelope.id`。
   **`user_id` 与 `external_user_id` 一律 hash 后写入**；消息内容默认不写（受
   `AuditPolicy.record_payload` 控制）
3. **采样**：`ParentBased(TraceIdRatioBased)` 基础采样 + **tail sampling**（在 Collector）
   保证 100% 采样错误/超时/治理拦截/高成本 trace。低价值高频 trace 降采样控成本
4. **Collector 侧脱敏处理器**：`attributes` processor 删除/哈希敏感 key，作为
   代码之外的第二道防线（防新代码不小心加了敏感 attribute）
5. **trace ↔ audit ↔ log 互通**：审计表存 `trace_id`/`span_id`；日志格式加
   `trace_id`（`log/_default_logger.py` 的 `log_format` 扩展）⇒ 三者可互跳
6. **Langfuse**：`server/langfuse/tracing/opentelemetry.py` 已支持自定义 attribute
   上报（见 commit `1bf8510`），把 tenant 维度带过去即可做租户级 LLM 分析

### 8.4 审计日志字段

必含字段全部落在 `audit_logs`（§6 第 10 表）：
`tenant_id` ✅ `channel` ✅ `user_id` ✅ `session_id` ✅ `agent_name` ✅ `tool_name` ✅
`decision` ✅ `latency`(latency_ms) ✅ `error_type` ✅ `cost`(cost_usd) ✅ `trace_id` ✅

补充字段与理由：
- `action`：区分 `im.inbound` / `model.call` / `tool.call` / `im.outbound` / `admin.update`，
  一张表覆盖全部审计场景
- `revision`：回溯"当时生效的是哪版策略"（事故复盘刚需）
- `decision_reason`：`deny` 的原因（哪条规则），否则无法向租户解释
- `payload_digest`：默认只存内容 hash，可证明"消息未被篡改"又不留原文
- `external_user_hash`：可关联同一外部用户的行为，又不存明文
- `envelope_id`：与幂等/去重链路对齐

写入策略（已修正）：

> ⚠️ 初版的"异步内存 ring buffer"与强合规诉求矛盾：节点崩溃会丢审计记录，
> 无法满足不可抵赖。且 `sha256(payload)` **不能证明未被篡改**（攻击者改内容后可重算
> digest），对低熵内容无密钥 digest 还可被柚举。

- **标准生产**：在业务事务内写 **durable audit outbox**，再异步投递审计库
- **强合规**：Kafka / WORM 对象存储 + 不可变保留策略 + **签名 hash chain**
- `payload_digest` 改用 **HMAC 或数字签名**，并记录 key version
- 必须明确审计后端不可用时是 **fail-open 还是 fail-closed**，按租户合规档位配置
- 分区表按天 + `retention_days` 自动 detach/归档到对象存储

### 8.5 密钥管理与脱敏

**硬性要求：IM token、模型 API key、数据库密码不得以明文出现在日志、trace、错误报告中。**

现状风险（必须修）：
- `server/openclaw/config/_config.py::AgentConfig.api_key: str = ""` —— YAML 明文
- pydantic 模型默认 `repr` 会打印全部字段 ⇒ `logger.error("config=%s", cfg)` 即泄露
- 异常 traceback 里 DSN（含密码）常出现在 SQLAlchemy/redis-py 的错误信息中

分层防护：

1. **配置层：只存引用**
   - `SecretRef`（provider + key + version），`__repr__`/`__str__` 只输出引用
   - `SecretResolver` 支持 `env` / `vault` / `k8s secret` / `kms`；内存缓存 + TTL，
     不落盘、不进 `TenantConfig` 的 JSONB 快照
   - 解析后的值用 `SecretStr`（pydantic）或自定义 `Secret` 包装类型持有；
     `Secret.__repr__` → `"***"`，取值必须显式 `.reveal()`（**审计点**：grep `.reveal()`
     即可枚举所有明文使用处）
2. **注入层：不落地**
   - 模型 key 通过 `RunConfig.custom_data` 在请求期注入
     （`configs/_run_config.py` 的 docstring 明确此用途：
     "such as dynamic API key retrieval or per-request model customization"）⇒ 天然契合
   - 容器/K8s：Vault Agent / External Secrets 挂载为文件或 env，进程只读一次
3. **日志层：强制脱敏**
   - 新增 `log/_redaction.py`：`RedactingFilter`（logging.Filter）+ 正则集
     （`sk-[A-Za-z0-9]{20,}`、`AKID\w+`、`Bearer \S+`、`postgres://user:pass@`、
     JWT 三段式、企业微信 corpsecret 形态），命中替换为 `***REDACTED***`
   - 挂到 `log/_default_logger.py` 的 handler 上，**默认开启**（安全默认值）
   - DSN 一律经 `mask_dsn()` 后再记录
4. **Trace/Metrics 层**
   - `_span_attrs.py` 白名单：只允许列举的 attribute key 写入 span
   - Collector `attributes` processor 二次删除/哈希（代码之外的兜底）
   - 绝不把请求 header、config dump 整体作为 attribute
5. **错误报告层**
   - 全局异常包装 `sanitize_exception(exc)`：脱敏 message + 截断 traceback 中的字面量
   - 返回给 IM 用户的永远是通用文案 + `trace_id`，细节只进审计
   - `pyproject.toml` 的 lint 增加自定义检查：禁止 `logger.*` 直接传入
     `TenantConfig` / `BackendSpec` / `ModelBinding` 整体对象
6. **轮换与最小权限**
   - `tenant_secrets.rotated_at` + 到期提醒；轮换支持新旧双活窗口
   - 每租户独立 DB 用户/schema（强合规档）；S3 按前缀最小权限 policy
   - 静态加密：SQL TDE + S3 SSE-KMS（每租户独立 CMK 可选）

**测试保障**（不写测试的安全设计等于没有）：
`tests/multitenant/test_redaction.py` 注入一批已知密钥形态，断言日志/span/异常输出中
一个都不出现；作为 CI 必过项。

---

## 9. 故障恢复与运维

### 9.1 降级策略

| 故障 | 检测 | 降级 | 用户感知 |
|---|---|---|---|
| **Worker 节点故障** | 消费者心跳丢失 / K8s liveness | Redis Streams `XAUTOCLAIM`（或 Kafka rebalance）把 pending 消息交给其他消费者；由 §5.5 幂等保证重放安全。session 锁靠租约自动过期释放 | 延迟增加，不丢消息 |
| **节点被 kill 时的 post-turn 丢失** | `post_turn_pending` 表有超时未完成记录 | 巡检任务重跑 summary/memory（幂等） | 摘要延迟 |
| **IM 平台重投** | 幂等键命中 | 直接返回缓存结果/空 ACK，不重跑 LLM | 无（也不重复计费） |
| **IM 投递失败** | 平台 4xx/5xx | 指数退避重试（1/4/16s×4）；429 按 `retry_after`；终态失败入死信 + 告警；可选降级为"简短文本"重发 | 可能延迟或收到简版 |
| **Redis 短暂不可用** | 连接异常 + 熔断器（连续 N 次失败） | **fail closed + 队列等待**（见下方修正）：不接受新的状态变更执行，消息保留在 durable queue，向用户发“暂时不可用”提示；去重降级到 SQL 唯一约束 | 延迟升高或明确报不可用，**不丢不分叉** |
| **SQL 短暂不可用** | 同上 | 审计写入转本地文件 buffer（后续回灌）；配置读走本地缓存快照（`TenantRuntimeCache` 保留最后一次成功配置，允许过期使用 + 打 `stale` 标记）；会话若主后端是 SQL 则同 Redis 降级路径 | 基本无感（配置类） |
| **模型超时/限流** | `models/_retry.py` + `ModelRetryConfig` | 已有指数退避重试；仍失败则按 `models` 列表 fallback 到备用 alias（不同 provider 更佳）；全失败返回兜底文案 | 变慢或换模型 |
| **工具执行失败** | 异常/超时 | 返回**结构化错误**给 LLM 让其自行恢复（而非直接失败）；同一工具同轮最多重试 2 次；`per_tool_timeout_s` 强制超时；MCP server 不可用则本轮摘除该工具 | Agent 自然绕行 |
| **代码执行器故障** | 容器启动失败 | container → local（若策略允许）→ disabled；策略不允许则明确报错 | 功能受限 |
| **向量库不可用** | 检索异常 | 跳过 RAG，system prompt 注明"知识库暂不可用"，继续对话 | 答案质量下降 |
| **配额耗尽** | `BudgetFilter` | `reject` / `degrade_model` / `queue`（依策略） | 明确提示 |
| **单租户打爆共享资源（吵闹邻居）** | per-tenant QPS/并发指标 | 租户级令牌桶 + 并发信号量；持续超限则临时 `readonly`；重要租户可专属 Worker 池（`nodeSelector`/独立 deployment） | 该租户被限流，其他不受影响 |

熔断统一用半开探测（`closed → open(冷却30s) → half_open(单探) → closed`），
每个后端独立熔断器，状态经 `mt.degraded_mode` 暴露。

> ⚠️ **重要修正：禁止把 InMemory 作为生产 Session 的自动 fallback。**
> 本文初版建议"Redis 挂了就用进程内 session + 本地 outbox 回放"，这会造成 **split-brain**：
> 同一 session 在不同节点形成不同历史，恢复后无法可靠合并 events 与 state；锁后端同时不可用
> 时更无法判定写入顺序；用户可能基于空历史执行高风险工具；且本地 outbox 与
> 只读根文件系统的部署要求（§9.4）相矛盾。
>
> **生产默认：fail closed**。只有无状态、只读、且显式配置允许的请求可降级。

### 9.2 灰度发布与租户级配置回滚

**两个正交维度：代码灰度、配置灰度。**

**代码灰度**
- Gateway/Worker 各部署 `stable` 与 `canary` 两组（不同 Deployment、同一 consumer group 或
  独立 group）
- 路由：`TenantConfig.release_channel == "canary"` 的租户，其消息进 `stream:agent:canary:*`，
  只由 canary Worker 消费 ⇒ **爆炸半径限定在被选中的租户**
- 推进节奏：内部租户 → 1% 小租户 → 10% → 50% → 100%，每档观察窗口看
  错误率 / P95 延迟 / 投递成功率 / 治理 deny 率突变
- 自动回滚：canary 指标越界（如错误率 > stable 的 2 倍持续 5 min）→ 流水线自动把
  `release_channel` 改回 `stable` 并回滚镜像
- DB 变更遵循 **expand-contract**：先加可空列/新表（兼容旧代码）→ 双写 → 切读 → 再删旧。
  绝不在一次发布里同时加列和依赖它

**配置灰度与回滚**
- 一切配置变更写 `config_revisions`（append-only，含 author/reason/diff）；
  `tenants.revision` 是当前生效指针
- 变更流程：`validate`（schema + 引用可达性 + 密钥可解析 + 后端连通性 dry-run）→
  `plan`（diff 预览）→ `apply`（写 revision + 移动指针 + Pub/Sub 广播失效）
- Worker 侧 `TenantRuntimeCache` 收到失效通知即重建（也有 TTL 兜底，默认 60s）；
  **在途轮次继续用旧版本**（`ctx.metadata["revision"]` 固定），避免同一轮次前后策略不一致
- 回滚：`POST /admin/tenants/{id}/rollback?to_revision=N` → 写一条新 revision
  （`rolled_back_from=当前`）+ 移指针。**回滚也是前进**，历史不可变，可完整审计
- 每个节点上报 `mt.config.revision` gauge ⇒ 能看到"配置是否已在全集群生效"（检测漂移）
- 危险变更（换 session 后端、改 tool_policy 为 allow-all、改 audit 为 disabled）要求
  二人复核（Admin API 加 approval 流）

### 9.3 容量评估

**单 Worker 并发能力**

Agent 轮次是 IO 密集（LLM 等待占 80–95%），asyncio 下的约束是内存与 CPU 而非线程：

```
单轮内存 ≈ session 历史(events 数 × 平均 event 大小) + prompt/response buffer
        ≈ 30 events × 8 KB + 2 MB ≈ 2.3 MB   （取 3 MB 计安全余量）
单 Worker 并发轮次 C ≈ (可用内存 × 0.7) / 3 MB
  4 GiB Pod → C ≈ 950；但 CPU（JSON 序列化、tokenize）通常先到瓶颈
经验取值：2 vCPU / 4 GiB → C = 150~250 并发轮次（留 50% 余量取 150）
```

**吞吐**
```
单 Worker QPS = C / 平均轮次耗时
  平均轮次 8s（含 LLM 2 次调用）→ 150/8 ≈ 18 轮次/s
需要的 Worker 数 = 峰值轮次QPS / 18 × (1 + 冗余30%)
```

**Token 与成本**
```
单轮 token ≈ system(500) + 历史(摘要后 ~2000) + 工具 schema(1500) + 用户(200)
           + 输出(400) ≈ 4600 tokens（有 prompt cache 时输入部分可降 50%+）
日 token = 日轮次 × 4600
日成本 ≈ 日轮次 × (input×单价_in + output×单价_out)
→ 直接用于 BudgetPolicy 定档与租户报价
```

**Redis QPS**
```
单轮 Redis 操作 ≈ get_session(1) + lock(2: set+del) + 续租(轮次/10s)
                + append_event(events 数 × 1~2) + state(1~3) + dedup(1)
                + 限流(2) + 用量(2) ≈ 25~40 ops
Redis QPS = 轮次QPS × 35
单实例安全上限 ~80k ops/s（pipeline 后更高）→ 约 2200 轮次/s，通常不是瓶颈
瓶颈更可能是**内存**：活跃 session 数 × session 大小(≈250 KB) → 10 万活跃 session ≈ 25 GiB
必须设 TTL（`types/_ttl.py` 已有 Ttl 配置）+ maxmemory-policy=noeviction（会话不可被随意淘汰，
应主动归档到 SQL/S3）
```

**SQL QPS**
```
若 session 主存 SQL：单轮 ≈ 1 SELECT(join events) + N INSERT + 1~3 UPSERT ≈ 10~20 ops
审计：单轮 3~6 行（批量写摊薄为 ~1 次 INSERT）
用量对账：分钟级批量
PG 单实例(8vCPU) 写入 ~5k TPS → 约 300~500 轮次/s
→ 高吞吐场景 session 走 Redis，SQL 只承担审计/配置/摘要
```

**IM 回调峰值**
```
峰值 = 群数 × 群活跃人数 × 人均消息频率，且呈强突发（早高峰、活动推送后）
容量按 P99 峰值的 1.5 倍配 Gateway；Gateway 单实例可轻松 2000+ RPS（只做验签+入队）
真正的削峰点是队列：允许队列堆积（延迟↑）而不是拒绝（消息丢失）
需监控 mt.queue.lag_seconds，SLO 内用 KEDA 扩 Worker
企业微信/公众号 5s 超时是硬约束 → Gateway ACK 路径必须无 DB 重操作
```

**压测计划**：`pipeline_test/` 下加多租户场景（100 租户 × 混合后端）。
至少必须实测：P50/P95/P99 session JSON 大小、append bytes/sec、JSON 编解码 CPU、
Redis command latency、**queue 未投递 + pending + processing 总 lag**（非仅 `XPENDING`）、
provider 并发连接限制、工具与代码执行的峰值内存（后三项初版完全未计入）。
校准后写入运维手册（不要照抄经验值，要用自己环境的实测值）。

### 9.4 部署方案

**A. 最小可运行（Docker Compose，本地/单机验证）**

```yaml
# deploy/compose/docker-compose.min.yml
services:
  gateway:        # Agent Gateway + Channel Adapter 同进程
    build: .
    command: trpc_agent_cmd mt gateway --config /etc/mt/config.yaml
    environment:
      MT_QUEUE_URL: redis://redis:6379/1
      MT_CONFIG_DB:  postgresql+asyncpg://mt:mt@postgres/mt
      OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4318
    ports: ["8080:8080"]
    depends_on: [redis, postgres, otel-collector]

  worker:
    build: .
    command: trpc_agent_cmd mt worker --config /etc/mt/config.yaml --partitions 0-15
    deploy: { replicas: 2 }          # 演示多节点无状态
    environment: *same_env

  admin:
    build: .
    command: trpc_agent_cmd mt admin --config /etc/mt/config.yaml
    ports: ["8081:8081"]

  redis:      { image: redis:7-alpine, command: redis-server --appendonly yes }
  postgres:   { image: postgres:16-alpine, environment: { POSTGRES_DB: mt, ... } }
  minio:      { image: minio/minio, command: server /data }   # Artifact (S3 兼容)
  otel-collector: { image: otel/opentelemetry-collector-contrib, volumes: [./otel.yaml:/etc/otelcol/config.yaml] }
  jaeger:     { image: jaegertracing/all-in-one }
  prometheus: { image: prom/prometheus }
```
- 2 个 worker 副本即可验证"无状态 + 分区路由 + 共享后端"是否真的成立
- 更小的开发模式：`--backend inmemory` 单进程，Session/Memory/Artifact 全 InMemory，
  仅用于跑 examples

**B. 生产推荐（Kubernetes）**

```
Namespace: agent-mt
├── Ingress (nginx/ALB) — TLS 终止、WAF、per-route 限速
│     /im/*      → svc/gateway
│     /admin/*   → svc/admin（内网 + OIDC/mTLS）
├── Deployment gateway         3+ 副本, HPA(cpu 60% / rps), PDB minAvailable=2
│     readiness: /healthz（含 Redis+SQL 探活）; resources 1c/1Gi
├── Deployment worker-stable   N 副本, KEDA(redis stream lag), PDB
│     resources 2c/4Gi, terminationGracePeriodSeconds=120（排空在途轮次）
│     lifecycle.preStop: 停止拉新消息
├── Deployment worker-canary   1~2 副本（灰度，仅消费 canary 分区）
├── Deployment admin           2 副本
├── Deployment channel-adapter 2+ 副本（若与 gateway 分离；订阅 outbound stream）
├── CronJob  post-turn-reaper  补偿 post_turn_pending
├── CronJob  audit-partition   审计分区滚动 + 归档到对象存储
├── CronJob  usage-reconcile   Redis 计数 → usage_daily 对账
├── DaemonSet otel-collector-agent  → Deployment otel-collector-gateway(tail sampling)
├── ExternalSecret (Vault/ASM) → 生成 K8s Secret，Pod 只读挂载
└── NetworkPolicy: worker 出网仅允许 模型域名 + 后端 + IM 平台域名（egress allowlist）

托管依赖（不自建）：
├── Redis：主从 + Sentinel/托管集群，AOF everysec，maxmemory-policy noeviction
├── PostgreSQL：主 + 只读副本 + PITR；session/audit 分库
├── S3/COS：Artifact，SSE-KMS，生命周期规则
├── 向量库：Milvus/Qdrant 托管，collection per tenant
└── Kafka（可选，替代 Redis Streams）：审计归档 + 大规模队列
```
关键运维配置：
- `topologySpreadConstraints` 跨 AZ 打散；`PodAntiAffinity` 避免同节点堆叠
- HPA/KEDA 缩容需 `stabilizationWindowSeconds` 防抖
- 所有 Pod `runAsNonRoot`、只读根文件系统（代码执行器用独立沙箱 Pod/gVisor）
- 备份：SQL 每日全量 + WAL；Redis 快照仅作加速（**权威数据不能只在 Redis**）

---

## 10. 模块与文件清单

```
trpc_agent_sdk/
├── tenancy/                              # 新增：租户模型与装配（§2、§4.1）
│   ├── __init__.py  _tenant_config.py  _scope.py  _secret_ref.py
│   ├── _registry.py  _resolver.py  _runtime.py  _revision.py
│   └── backends/  __init__.py  _spec.py  _factory.py
├── audit/                                # 新增：审计（§8.4）
│   ├── __init__.py  _abc.py  _record.py
│   └── _sql_sink.py  _kafka_sink.py  _file_sink.py  _otel_sink.py
├── artifacts/                            # 补齐生产实现（§0 gap）
│   ├── _sql_artifact_service.py          # 新增
│   └── _s3_artifact_service.py           # 新增
├── sessions/
│   ├── _redis_session_service_v2.py      # 新增：追加语义 + Lua CAS（§5.1 L3）
│   └── _sql_session_service.py           # 改：revision 乐观锁 + seq（向后兼容开关）
├── log/_redaction.py                     # 新增：日志脱敏 Filter（§8.5）
├── telemetry/_span_attrs.py              # 新增：attribute 白名单常量（§8.3）
└── server/multitenant/                   # 新增：运行时组件
    ├── __init__.py  _cli.py              # register_cli → `trpc_agent_cmd mt ...`
    ├── _envelope.py  _queue.py  _dedup.py  _lock.py  _metrics.py  _errors.py
    ├── gateway/  _app.py  _ingress_filters.py  _routes.py
    ├── worker/   _consumer.py  _turn.py  _post_turn_reaper.py
    ├── admin/    _app.py  _schemas.py  _authz.py
    ├── channels/ _abc.py _registry.py _limits.py
    │             _wecom.py _wecom_kf.py _wechat_mp.py _telegram.py
    └── filters/  _tenant_guard.py  _tool_guard.py  _budget.py
                  _redaction.py  _confirmation.py  _audit.py

deploy/
├── compose/docker-compose.min.yml  otel.yaml
└── k8s/  (kustomize base + overlays: dev/staging/prod)

migrations/                               # 新增：Alembic（§6 注）
docs/mkdocs/{en,zh}/multi_tenant.md       # 用户文档（挂 mkdocs.yml nav）
examples/multi_tenant_im/                 # 可运行示例（2 租户 × 2 通道）
tests/multitenant/                        # 镜像结构（仓库约定）
tests/tenancy/
```

**约定遵循**：私有 `_module.py` + `__init__.py` 导出；Apache-2.0 文件头；
yapf `pep8`/120 列/`split_before_logical_operator`；flake8 忽略 E402,W503；
pytest `asyncio_mode=auto`；`tests/` 目录镜像源码结构。
新增可选依赖组 `multitenant = [fastapi, uvicorn, boto3, aiokafka?, hvac?]`（`pyproject.toml`）。

---

## 11. 实施路线图

> ⚠️ **已根据对抗性评审重排。** 初版先做 Alembic/SecretRef/Redis V2，但那些都是
> “实现级”任务；在**语义未冻结**前就扩展 TenantConfig / Filter / Channel Adapter / 部署清单，
> 会放大返工成本。因此新增 **P0 语义冻结（ADR）**为硬前置。

| 阶段 | 目标 | 必要交付 | 验收 |
|---|---|---|---|
| **P0 语义冻结**（1 周） | 先把保证写清楚 | 独立 ADR：at-least-once 语义边界、**Turn 状态机**、持久 Inbox/Outbox、**确定性身份（UUID5）**、**fencing epoch**、Session revision、租户隔离三档位 | ADR 评审通过；§5.2/§5.6 与现有 `Runner` 接口的冲突有明确结论 |
| **P1 持久消息边界**（2 周） | 不丢消息 | durable Inbox/Outbox、原子入队（SQL 事务 outbox 或 Redis Lua）、Relay、恢复器、Alembic 接入 | **崩溃矩阵测试**（§12.1）全绿：每个注入点后消息不静默丢失 |
| **P2 Turn 与 Session 协议**（3 周） | 可安全重放 | `Runner` persisted-turn API、`SessionServiceABC.begin/commit_turn`、UUID5 确定性 ID、`seq`/`revision`/`fence_epoch`、`_redis_session_service_v2`（追加语义+Lua CAS）、顺序协议 | 并发测试 8×同 session×500 轮：events 不丢、seq 连续、state 不被覆盖；**陈旧锁持有者测试**（§12.2）；**Envelope 重放测试**（§12.3） |
| **P3 租户安全边界**（2 周） | 防跨租户访问 | tenant-bound facade（不接受任意 `app_name`）、`tenancy/` 全套、RLS 可选档、Knowledge 强制过滤、**`agent_factory` allowlist**、Filter/Model 每租户实例化、`log/_redaction.py` + `SecretRef` | **跨租户对抗测试**（§12.5）全绿；**密钥泄露测试**（§12.10）全绿 |
| **P4 Post-turn 可靠性**（2 周） | Summary/Memory 可恢复 | **immutable event-range job**（修正 summary 压缩先于 memory 的 bug）、durable outbox、summary lease + CAS、`session_summaries` 落库并接入 `get_session()` | **post-turn kill 测试**（§12.7）：原始事件不丢、Memory 最终补齐、Summary 不回退 |
| **P5 节点化与取消**（2 周） | 多节点运行 | ordered scheduler、**分布式 cancel control channel**（高优先旁路）、stream correlation 持久化、Gateway/Worker/`mt` CLI、compose | 杀任一节点消息不丢不重、上下文连续；跨节点 `/stop` 生效且不被长任务阻塞 |
| **P6 IM 与治理**（3 周） | 接入和策略 | 企业微信 + Telegram Adapter、**异步 media ingestion**（不在 ACK 快路径）、HMAC 身份映射、**合成 group 主体**、**预算原子预留**、跨 chunk 脱敏、`audit/` durable outbox、全链 trace | **群会话测试**（§12.4）；平台重投不重复回复；429 正确退避；单 trace 串起 callback→deliver |
| **P7 运维与迁移**（2 周） | 可发布 | **migration barrier**（epoch + drain watermark）、**canary epoch**、K8s 清单、真实容量压测、灾恢演练、文档与示例 | **屏障测试**（§12.9）：切换时同一 session 不跨 epoch 并发；**PG 迁移测试**（§12.6） |

**风险与缓解**

| 风险 | 影响 | 缓解 |
|---|---|---|
| 改动 Redis/SQL session 语义破坏现有用户 | 高 | 新实现旁路并存（`options.append_mode`），默认不改行为；498 个现存测试文件必须全绿 |
| `app_name` 编码 tenant 的字符集冲突 | 中 | `CHECK` 约束禁止 `tenant_id`/`app_id` 含 `|`；`TenantScope.parse` 严格校验并有单测 |
| metric 基数爆炸 | 中 | 禁止 user/session 进 label；长尾租户聚合 `_other`；上线前用 Prometheus `tsdb` 估算 |
| 跨租户数据泄露 | **极高** | 隔离测试作为 CI 门禁（构造对抗性 app_name/session_id）；Knowledge 强制服务端注入过滤；代码评审 checklist |
| 群聊共享上下文泄露用户私密记忆 | 高 | `is_group` 时 Memory 检索降级为 session 级（§7.4）+ 专项测试 |
| nanobot 上游 API 变动 | 中 | Adapter 层隔离（`_abc.py`），不在业务代码直接依赖 nanobot 类型 |
| 分布式锁误用导致死锁/长阻塞 | 中 | 强制租约 + 续租 + fencing token；锁等待有上限，超时重入队；`mt.session.lock_wait` 告警 |
| 进程内锁在多节点下失效（`plan_mode/_lock.py`） | 低-中 | 已核实：plan 态本身落 `ctx.state` ⇒ 跨节点可见；由 §5.1 L2 会话级锁覆盖串行性。需 audit 是否有绕过 Runner 的 plan 写入路径 |

---

## 12. 进入实现前的强制验收门禁

每道门禁都是 **CI 必过项**，不是“有空再补”。本节清单来自对抗性评审。

### 12.1 Ingress 崩溃矩阵
在以下每个位置注入进程崩溃，证明消息不静默丢失，并记录哪些场景可能重复：
dedup 前、dedup 后入队前、入队后 ACK 前、Session commit 前/后、
outbound 平台调用前/后、queue ACK 前/后。

### 12.2 陈旧锁持有者测试（fencing）
暂停 Worker A 致租约过期 → B 拿锁并提交 → 恢复 A。验证 A **无法**：
修改 Session state、append 非幂等事件、执行外部工具、发布 outbound。

### 12.3 Envelope 重放测试
同一 Envelope 在每个故障点后重放，验证：USER event 不重复、seq 不分叉、
幂等工具不重复产生副作用、Artifact 不重复生成版本、outbound 在平台支持幂等时不重复。

### 12.4 群会话测试
同群两用户：能读共享群上下文；**不能**读彼此私有 Memory；
发言人身份正确进入 prompt 与 audit；移除成员后不能继续访问。

### 12.5 跨租户对抗测试
构造：伪造 `app_name`、伪造 `ArtifactId`、覆盖 Knowledge filter、伪造 queue Envelope tenant、
使用其他租户的 SecretRef、Admin API 缺失 tenant scope、恶意 `agent_factory`。
**任何路径不得读写其他租户资源。**

### 12.6 PostgreSQL 迁移测试
真实 PG 上验证：空库创建、从旧 SDK schema 升级、expand-contract、
分区创建与轮转、并发索引、rollback、大表迁移锁时长。

### 12.7 Post-turn kill 测试
在 Summary 生成前/后、Memory 写入前/后、Session 压缩前/后 `kill -9`，验证：
原始事件不丢、Memory 最终补齐、Summary 不回退、不重复压缩或破坏 event window。

### 12.8 后端故障测试
Redis/SQL 故障时验证**不会自动形成 InMemory split-brain**，
消息保留在 durable queue 并在恢复后按序继续。

### 12.9 Canary 与迁移屏障测试
切换 stable/canary 或 source/target 时，验证同一 Session 的连续消息不会跨 epoch 并发处理，
也不会由旧 Worker 在切换后写入。

### 12.10 Secret 泄露测试
样本必须覆盖：API key、DSN 密码、Authorization header、**`extra_headers`**、
**`BackendSpec.options`**、**SecretRef 路径**、异常链与 traceback、OTel attributes/events、
**流式跨 chunk 密钥**、Admin dry-run 错误。

---

## 13. 其余已修正的设计缺口

来自对抗性评审的 §19-24，均为真实缺口，已纳入：

| # | 缺口 | 修正 |
|---|---|---|
| 19 | `SecretRef.__repr__` 仍泄露租户名/系统结构/密钥路径 | 日志只输出 `provider` + 逻辑名 + **不可逆 ref fingerprint**，不输出完整路径。`extra_headers` 与 `BackendSpec.options` 可直接含 Secret，**不能只靠字段命名约定**，需整体当敏感对象处理 |
| 20 | 任意 `agent_factory` = 远程代码执行 | 必须由管理员维护模板/工厂 **allowlist**，租户只能选已批准 ID（已写入 §4.4） |
| 21 | `readonly` 语义不完整 | 明确区分：`readonly` = **禁止业务副作用工具**，但仍允许写 Session event / audit / usage（欠费冻结场景仍需审计）；若需“数据完全不可写”另设 `frozen` 状态 |
| 22 | 数据删除权流程缺失 | 新增 tenant/user deletion job：各后端删除状态与证明、S3 version/delete marker 清理、向量库删除、外部 Memory 服务删除、audit legal hold 例外、backup 延迟删除策略 |
| 23 | Admin API 安全不足 | 补：RBAC/ABAC、操作者 tenant scope、防 confused deputy、revision 乐观锁、CSRF、SecretRef 可用范围、**后端连通性 dry-run 的 SSRF 风险**、**审批人≠提交人**、break-glass 流程 |
| 24 | Readiness 同时探测 Redis+SQL 会放大故障 | 拆分：liveness（仅进程）/ readiness（能否安全接请求）/ dependency health（独立指标+熔断）/ degraded readiness（单通道或单租户不可用 ≠ 整个 Pod 不可用）。**避免共享依赖抖动就摘除全部实例** |

---

## 14. 需要确认的开放问题

1. **队列选型**：Redis Streams（少一个组件、够用到万级 QPS）vs Kafka（更强持久性与回溯、
   运维更重）。建议先 Redis Streams，`_queue.py` 抽象保留 Kafka 实现位。
2. **租户规模量级**：< 100（可全量常驻缓存 + 每租户 metric）还是 > 10 000
   （需配置分片加载 + metric 聚合）？直接影响 §8.2 基数策略与 `TenantRuntimeCache` 容量。
3. **是否需要单租户专属隔离档**（独立 Worker 池 + 独立 DB + 独立向量实例）？
   若需要，K8s 层面要引入 per-tenant Deployment 生成器（Operator/Helm 模板）。
4. **Plan 模式的多节点语义（已核实，需确认取舍）**：`plan_mode/_store.py` 是纯函数状态机
   （"in-memory; caller persists"），`_controller.py::_save_plan` 把计划编码后写入
   `ctx.state[...]` ⇒ **计划态随 session state 落共享后端，跨节点可见，无需改造**。
   但 `plan_mode/_lock.py` 的 `_locks: Dict[_LockKey, asyncio.Lock]` 是**进程内**锁，
   跨节点不生效。结论：只要 §5.1 的**会话级分布式锁（L2）**覆盖了 plan 工具所在的轮次，
   进程内锁退化为无害的本地优化，不需要额外分布式化；需确认是否存在
   "同一 session 的 plan 更新绕过 Runner 轮次"的调用路径（如 Admin 直接改计划），
   若有则必须补分布式锁。
5. **合规要求**（数据驻留地域、可删除权/GDPR、审计不可篡改）：
   若需要按地域隔离，架构需升级为多 region 部署 + 租户地域绑定，`route_key` 需带 region 提示。
6. **成本核算口径**：按 token 定价直接换算，还是含基础设施摊销？影响
   `ModelBinding.cost_per_1k_*` 与 `usage_daily` 是否需要区分 list price 与 internal cost。
