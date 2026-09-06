# 多租户与节点部署 — 架构设计文档

> 状态：待评审
> 日期：2026-09-06
> 方案基调：编排层方案（不改动 SDK 核心，复用 `app_name` 作为租户隔离键）

## 目录

1. [背景与目标](#1-背景与目标)
2. [核心思路](#2-核心思路)
3. [子项目分解与顺序](#3-子项目分解与顺序)
4. [SP1 租户模型与隔离机制](#4-sp1-租户模型与隔离机制)
5. [SP2 统一数据访问抽象、多后端与数据同步](#5-sp2-统一数据访问抽象多后端与数据同步)
6. [SP3 节点部署拓扑与水平扩展](#6-sp3-节点部署拓扑与水平扩展)
7. [SP4 IM Channel Adapter](#7-sp4-im-channel-adapter)
8. [SP5 治理、监控与安全](#8-sp5-治理监控与安全)
9. [SP6 故障恢复与运维](#9-sp6-故障恢复与运维)
10. [最小数据模型 / 表结构](#10-最小数据模型--表结构)
11. [风险与开放问题](#11-风险与开放问题)

---

## 1. 背景与目标

为 tRPC-Agent-Python SDK 构建多租户能力与可水平扩展的节点部署拓扑，使不同租户可以：

- 拥有独立的配置、模型、工具权限、IM 通道、数据后端与审计策略；
- 选择不同数据后端（InMemory / Redis / SQL / 向量库 / 对象存储 / 外部 Memory 服务）；
- 接入至少两类 IM 通道（企业微信、Telegram 等）；
- 在多节点上无状态水平扩展，消息正确路由到租户与 session；
- 具备治理、监控、安全、审计、故障恢复与灰度发布能力。

### 1.1 现状（代码库调研结论）

- **无 tenant 概念**：全库仅有一处注释提及 "multi-tenant"（`skills/hub/_github.py`）。
- **`app_name` 是事实上的租户键**：`Runner.run_async(user_id, session_id, ...)` 之外，Session/Event/Memory/Storage 均以 `(app_name, user_id, session_id)` 命名空间隔离。
- **已有后端实现**：
  - Session：`InMemory` / `Redis` / `RedisCluster` / `Sql`（`sessions/_base_session_service.py` 及各实现）。
  - Memory：`InMemory` / `Redis` / `RedisCluster` / `Sql` / `mem0` / `mempalace`（`memory/`）。
  - Storage：`BaseStorage` → `RedisStorage` / `RedisClusterStorage` / `SqlStorage`（`storage/`）。
  - Artifact：`ArtifactServiceABC` + `InMemoryArtifactService`。
  - Knowledge：向量库抽象（`knowledge/`）。
- **已有 Filter 体系**：`BaseFilter` / `FilterABC` / `FilterRegistry` / `FilterType.{MODEL,TOOL,AGENT}`（`filter/`）。
- **已有遥测**：OpenTelemetry trace + metrics（`telemetry/`），trace 已覆盖 invocation/runner/llm/tool/session。
- **已有 Server 适配器**：AG-UI（FastAPI endpoint + `AgUiAgent`）、A2A、openclaw、langfuse。
- **已有并发/stale 处理**：`SqlSessionService.append_event` 已有 `update_time` 乐观比较与重读合并逻辑（`_sql_session_service.py:551`）。

---

## 2. 核心思路

**编排层方案**：在 SDK 之上构建一个 `Agent Gateway` 编排层，`tenant_id` 在 Gateway 层解析，落到 SDK 层即 `app_name = tenant_id`（或组合命名空间 `tenant_id:app_name`）。

- SDK 现有 Session/Event/Memory/Storage 已按 `app_name` 命名空间隔离，天然获得租户级数据隔离，**零侵入**。
- 每个租户对应一个 `Runner` 实例（懒加载 + 缓存），其 session/memory/storage 后端由该租户的 `DataBackendConfig` 决定。
- 租户配置、密钥、审计策略由独立的 `TenantRegistry` + `SecretStore` 管理。

### 2.1 顶层组件拓扑

```
IM 平台 ──▶ Channel Adapter ──▶ Agent Gateway ──▶ Agent Worker (Runner)
                                        │
                    ┌───────────────────┼───────────────────────┐
                    │                   │                       │
               TenantRegistry     Storage Adapter         Admin API
              (配置/密钥/审计策略)   (统一数据访问)        (租户 CRUD/灰度)
                    │                   │
               Config DB           Session / Memory / Summary /
                                   Artifact / Knowledge / Audit
                                   (InMemory / Redis / SQL / 向量库 / 对象存储)
```

---

## 3. 子项目分解与顺序

本特性横跨 6 个相对独立的子系统，分解为 6 个子项目，各自独立出 spec → plan → 实现，可独立测试。

| # | 子项目 | 交付物 | 依赖 |
|---|--------|--------|------|
| SP1 | 租户模型 + 隔离机制 | `Tenant` 模型、`TenantRegistry`、`SecretStore`、`Masker` | 无 |
| SP2 | 统一数据访问抽象 + 多后端 + 数据同步 | `StorageAdapter`、`BackendFactory`、迁移工具、一致性策略 | SP1 |
| SP3 | 节点部署拓扑 + 水平扩展 + 路由 | Gateway/Worker/Adapter 组件、路由协议、无状态 Worker | SP1 |
| SP4 | IM Channel Adapter（企微 + Telegram） | 消息归一化、webhook 验签、去重、session 规则 | SP3 |
| SP5 | 治理 / 监控 / 安全（Filter + OTel + 审计） | 租户级 Filter 链、指标、trace、审计日志 | SP1–SP3 |
| SP6 | 故障恢复 + 运维（降级/灰度/容量/部署） | 降级策略、canary、容量评估、Docker Compose/K8s | SP1–SP5 |

---

## 4. SP1 租户模型与隔离机制

### 4.1 `Tenant` 模型（最小字段集）

```python
class Tenant(BaseModel):
    tenant_id: str                          # 全局唯一
    app_config: AppConfig                    # 默认 app_name、agent 描述
    model_config: ModelConfig                # provider、model name、endpoint、timeout
    tool_permissions: ToolPermissions        # 白名单/黑名单、危险工具需二次确认
    im_channels: list[ChannelBinding]        # 企微/Telegram 等，含 webhook 配置
    data_backends: DataBackendConfig         # session/memory/summary/artifact/knowledge/audit 各自后端
    audit_policy: AuditPolicy                # 审计字段开关、采样率、保留期
    budgets: BudgetConfig                    # 每租户 token/成本预算
    rate_limits: RateLimitConfig             # IM 频率、请求 QPS
```

### 4.2 `TenantRegistry`

配置统一读取入口，支持本地文件 / SQL / 远端配置中心；带缓存 + 变更通知（供灰度回滚用）。

- `get(tenant_id) -> Tenant`
- `subscribe(on_change: Callable[[str, int], None])` —— 返回租户 `version`，用于灰度/回滚。
- 密钥字段（IM token、模型 API key、DB 密码）**不存本模型**，只存引用（`secret_ref`），实际值从 `SecretStore` 运行时解析。

### 4.3 隔离机制（四层）

| 维度 | 机制 |
|------|------|
| 配置隔离 | 每租户独立 `Tenant` 配置对象，`Runner` 从注册表按 `tenant_id` 构建，互不可见 |
| 数据隔离 | `app_name = tenant_id` 命名空间；SQL 走 `app_name` 列，Redis 走 key 前缀 `{tenant_id}:`，向量库走 `tenant_id` collection |
| 工具权限隔离 | 租户级 Filter 链 + `ToolPermissions` 白名单在 tool 执行前拦截 |
| 日志脱敏 | 统一 `Masker` 过滤器，正则脱敏手机号/身份证/API key/IM token |

### 4.4 密钥管理 `SecretStore`

抽象接口，实现：`EnvSecretStore` / `KmsSecretStore` / `VaultSecretStore`。

```python
class SecretStore(ABC):
    async def get(self, ref: str) -> str: ...
    async def put(self, ref: str, value: str) -> None: ...
    async def delete(self, ref: str) -> None: ...
```

约束：密钥永不进入日志、trace、错误报告、审计日志；运行时通过 `ModelConfig`/`ChannelBinding` 的 `secret_ref` 注入。

---

## 5. SP2 统一数据访问抽象、多后端与数据同步

### 5.1 抽象现状（可直接复用）

| 数据 | 现有抽象 | 现有实现 | 缺口 |
|------|----------|----------|------|
| Session | `BaseSessionService` | InMemory / Redis / RedisCluster / Sql | 无（加 tenant 命名空间即可） |
| Memory | `BaseMemoryService` | InMemory / Redis / RedisCluster / Sql / mem0 / mempalace | 无 |
| Storage | `BaseStorage` | Redis / RedisCluster / Sql | 无 |
| Summary | summary 锚点事件（`_session_summarizer.py`） | 存于事件流 | 需独立 `summaries` 表 |
| Artifact | `ArtifactServiceABC` | InMemory | 需对象存储实现（S3/COS） |
| Knowledge | `knowledge/` 向量库 | 本地/远端 | 无 |
| Audit Log | **无** | — | 新增 `audit_logs` 表（SP5） |

### 5.2 新增 `StorageAdapter` 门面 + `BackendFactory`

按租户 `DataBackendConfig` 实例化并缓存各服务连接池，提供每类数据的后端选择。

```python
class DataBackendConfig(BaseModel):
    session: BackendSpec      # {type: "redis"|"sql"|"in_memory", dsn: ...}
    memory: BackendSpec
    summary: BackendSpec
    artifact: BackendSpec
    knowledge: BackendSpec
    audit: BackendSpec
```

```python
class StorageAdapter:
    async def session_service(self, tenant_id: str) -> BaseSessionService: ...
    async def memory_service(self, tenant_id: str) -> BaseMemoryService: ...
    async def artifact_service(self, tenant_id: str) -> ArtifactServiceABC: ...
    async def migrate(self, *, tenant_id: str, source: BackendSpec, target: BackendSpec) -> None: ...
```

连接池跨租户共享（同一后端同 DSN），避免每租户新建连接；租户级 `Runner` 持有 `StorageAdapter` 解析出的服务引用。

### 5.3 数据同步策略

- **多节点并发写同一 session**：事件追加以 `event.id` 主键幂等；`update_time` 乐观比较检测 stale（复用现有逻辑），冲突时重读合并（`_sql_session_service.py:551-577`）。
- **event / state / summary 更新顺序**：event → state（`state_delta` 合并）→ summary（post-turn）→ 更新 `last_update_time`。`Runner` 已在 `_schedule_post_turn_processing`（`runners.py:609`）串行化 post-turn，保证 event 先于 summary。
- **Memory 写入后跨节点可见性**：`store_session` 在 post-turn 完成后写共享后端；读走 `search_memory` 读后端，天然可见（最终一致）。
- **迁移方案**（Redis→SQL、本地向量库→远端）：
  1. 双写（新数据同时写 source + target）；
  2. 后台批迁移历史数据；
  3. 读切换开关（灰度）；
  4. 校验一致后停写 source 并清理。
  `StorageAdapter.migrate` 封装此流程。
- **IM 重复投递幂等**：adapter 层以 `(channel, external_msg_id)` 去重（Redis `SETNX` + TTL），并在 session 层以 `event.id` 兜底。

### 5.4 一致性取舍

| 后端 | 一致性 | 读写延迟 | 成本 | 运维复杂度 |
|------|--------|----------|------|-----------|
| InMemory | 强（单节点） | 极低 | 最低 | 最低（不可扩展） |
| Redis | 最终一致（Lua 原子写） | 低 | 中 | 中 |
| SQL | 强一致（事务） | 中 | 中高 | 中 |
| 向量库/对象存储 | 最终一致 | 中高 | 高 | 高 |

**建议**：Session/State/Summary 默认 Redis 或 SQL（需强一致选 SQL）；Memory 可 Redis/mem0；Artifact/Knowledge 走对象存储 + 向量库；Audit 走 SQL（便于查询与合规）。

---

## 6. SP3 节点部署拓扑与水平扩展

### 6.1 组件职责

| 组件 | 职责 |
|------|------|
| **Agent Gateway** | 统一入口：租户解析（webhook URL/token → tenant_id）、鉴权、限流、路由到 Worker、响应回传 |
| **Agent Worker** | 执行 `Runner.run_async`，维护租户 `Runner` 池；无状态，任意横向扩容 |
| **Channel Adapter** | 独立进程/边车：IM 协议 ↔ 内部 `Content`/`Event` 转换，webhook 验签、去重 |
| **Storage Adapter** | 统一数据访问门面，按 `DataBackendConfig` 路由到具体后端；数据迁移入口 |
| **Admin API** | 租户 CRUD、配置下发、灰度发布、回滚、审计查询 |
| **Telemetry Collector** | OTel collector + Prometheus；聚合 trace/metrics，按 `tenant_id` 维度 |

### 6.2 消息路由到正确租户 + session

1. Gateway 从入站请求提取 `tenant_id`（webhook 绑定关系 / 显式 header）。
2. 查 `TenantRegistry` 得到租户配置 → 定位/创建该租户的 `Runner`。
3. `session_id` 由 Channel Adapter 按规则生成（见 SP4），Gateway 将 `(tenant_id, user_id, session_id)` 透传给 `Runner.run_async`。
4. Worker 无状态：任意 Worker 拿到同样三元组都能从共享后端读到同一 session。

### 6.3 是否需要 sticky session

**不需要。** 理由：

- `Session`/`Memory` 全在共享后端（Redis/SQL），Worker 不持有会话本地状态。
- 并发写同一 session 用事件级幂等（`event.id` 主键）+ `update_time` 乐观版本比较（SDK 已有 stale 检测）。
- 例外：选用 `InMemory` 后端的租户需绑定单节点，或升级到 Redis（部署层对 InMemory 租户做节点亲和/告警提示）。

---

## 7. SP4 IM Channel Adapter

### 7.1 抽象与目标（至少支持企微 + Telegram）

```python
class ChannelAdapter(ABC):
    def channel_type(self) -> str: ...                    # "wecom" | "telegram" | ...
    async def inbound(self, raw: RawMessage) -> Content: ...   # 外部消息 → 内部 Content
    async def outbound(self, event: Event) -> OutboundMessage: ...  # Event → IM 回复/卡片/流式
    async def verify_signature(self, request) -> bool: ...  # 回调验签
    async def map_user(self, external_id: str) -> str: ...  # 用户身份映射
```

- **inbound**：外部 IM 消息（文本/图片/文件/卡片）→ `Content(parts=[...])` 作为 tRPC-Agent 用户输入。
- **outbound**：Agent `Event` → 企微文本/卡片消息、Telegram 消息；流式事件（`event.partial=True`）累积后分批发送或转卡片。
- **平台限制处理**：消息长度截断分段、频率限流与背压、异步主动回复（`reply_to`）、图片/文件转 `Part`（对象存储 URL）、撤回/失败重试（指数退避 + 幂等）。

### 7.2 IM 账号与租户绑定

- 每租户独立 webhook URL + `token` + `secret`（存 `ChannelBinding`，密钥引用 `SecretStore`）。
- 回调验签：企微 AES 解密验签、Telegram bot token 校验。
- 消息去重：`(channel, external_msg_id)` → Redis `SETNX` + TTL。
- 用户身份映射：`external_user_id → user_id`（可含企业内用户映射表）。

### 7.3 session_id 生成规则

- 单聊：`chat_{channel}_{peer_id}`。
- 群聊：`group_{channel}_{group_id}`（群内共用 session，按 `author` 区分用户）。
- 跨群/跨租户隔离：靠 `tenant_id + session_id` 命名空间（即 `app_name=tenant_id`）。

---

## 8. SP5 治理、监控与安全

### 8.1 租户级治理 Filter 链

复用 `BaseFilter` + `FilterRegistry`，按租户装配 Filter 链：

| Filter | 作用 |
|--------|------|
| 工具白名单 | `ToolPermissions` 白名单/黑名单在 tool 执行前拦截 |
| 敏感信息脱敏 | `Masker` 正则脱敏手机号/身份证/API key/IM token |
| 预算限制 | token/cost 超限终止运行（配合 `RunConfig` limits） |
| 危险工具二次确认 | HITL，需用户确认后才执行 |
| IM 用户权限校验 | 校验 `user_id` 是否有权访问该租户/agent |

### 8.2 监控指标

扩展 `telemetry/_metrics.py`，统一打 `tenant_id` label：

请求量、模型调用耗时、工具调用耗时、IM 投递成功率、错误率、token 消耗、每租户成本、Session 后端延迟。

### 8.3 OTel tracing

SDK 已用 OpenTelemetry。通过 propagation 传递 `trace_id`，使 trace 串起：

```
IM callback → Runner 执行 → Tool 调用 → Session/Memory 读写 → IM 回复
```

### 8.4 审计日志字段

`tenant_id, channel, user_id, session_id, agent_name, tool_name, decision, latency, error_type, cost, trace_id`

（`audit_log` 表见 §10，索引 `(tenant_id, created_at)`。）

### 8.5 密钥管理与脱敏

- `SecretStore` 统一管理 IM token、模型 API key、数据库密码。
- 日志/trace/错误报告经 `Masker` 过滤，任何密钥不以明文出现。

---

## 9. SP6 故障恢复与运维

### 9.1 降级策略

| 故障 | 降级 |
|------|------|
| 节点故障 | 健康检查摘除 + 重路由到健康 Worker |
| IM 重试 | 指数退避 + 幂等去重 |
| 数据库短暂不可用 | 熔断 + 快速失败 / 降级只读 |
| 模型超时 | `RunConfig` 限次 + 模型重试配置（`_model_retry_config.py`） |
| 工具执行失败 | 返回 error event 让 agent 自愈 |

### 9.2 灰度发布与配置回滚

- `Tenant.version` + canary 百分比切流（Admin API 控制）。
- 配置变更保留 `version` 快照，Admin API 一键回滚；`TenantRegistry.subscribe` 推送变更。

### 9.3 容量评估

给出估算公式与压测基线：
- 每节点并发 session 数 = `节点 CPU 核数 × 并发因子`（受模型 I/O 与事件流带宽约束）。
- 平均 token 消耗 = `并发 session × 每 session 平均 token`（用于成本与预算）。
- Redis/SQL QPS = `并发 session × 每 session 每秒 event 读写次数`。
- IM 回调峰值 = 活动用户数 × 峰值消息速率（用于 adapter 扩容与限流）。

### 9.4 部署方案

- **最小可运行（Docker Compose）**：Gateway + 1 Worker + Channel Adapter + Redis + SQL（单机）。
- **生产推荐（Kubernetes）**：Gateway/Worker/Adapter 独立 Deployment + HPA 水平扩缩；Redis/SQL/向量库托管；OTel collector sidecar 注入。

---

## 10. 最小数据模型 / 表结构

复用现有 `StorageSession` / `SessionStorageEvent` / `MemStorageEvent` 结构，仅增加 `tenant_id` 命名空间与新增 `summary` / `audit_log` / `channel_binding` 表。

```sql
tenant            (tenant_id PK, display_name, app_config JSON, model_config JSON,
                   tool_permissions JSON, audit_policy JSON, budgets JSON,
                   data_backend_config JSON, secret_refs JSON, status, version, updated_at)

agent_app         (app_id PK, tenant_id FK, app_name, agent_config JSON, enabled)

session           (tenant_id PK, app_name PK, user_id PK, id PK, state JSON,
                   conversation_count, create_time, update_time)          -- 复用 StorageSession

event             (id PK, tenant_id, app_name, user_id, session_id,
                   invocation_id, author, branch, content JSON, usage_metadata JSON,
                   timestamp, error_code, ...)                            -- 复用 SessionStorageEvent

memory            (id PK, tenant_id, user_id, session_id, content JSON, embedding, ttl)  -- 复用 MemStorageEvent

summary           (id PK, tenant_id, user_id, session_id, summary_text, anchor_event_id, created_at)

channel_binding   (binding_id PK, tenant_id, channel_type, webhook_url, token_ref,
                   secret_ref, verify_enabled, dedup_ttl, external_account_id)

audit_log         (id PK, tenant_id, channel, user_id, session_id, agent_name, tool_name,
                   decision, latency, error_type, cost, trace_id, created_at)  -- 索引 (tenant_id, created_at)
```

---

## 11. 风险与开放问题

1. **`app_name` 与 `tenant_id` 映射粒度**：单租户多 app 时用组合命名空间 `tenant_id:app_name`，需在 SP1 定死，避免后续迁移成本。
2. **InMemory 后端租户的可扩展性**：部署层需做节点亲和或强制升级 Redis，需在 SP3 明确策略。
3. **并发写冲突的最终一致性边界**：SQL 现有 stale 检测是"best-effort"，是否需要在 SP2 引入显式版本号/锁，需按实际压测决定。
4. **审计日志写入的吞吐与成本**：审计写 SQL 可能成为热点，需在 SP5 评估异步批量写入与采样率。
5. **IM 平台的频率与消息格式差异**：企微与 Telegram 差异较大，SP4 需先做两个具体平台的 PoC。
