# tRPC-Agent 多租户与节点部署 — 设计文档

## 1. 背景与目标

为 tRPC-Agent-Python SDK 构建多租户能力与可水平扩展的节点部署拓扑，使不同租户可以：

- 拥有独立的配置、模型、工具权限、IM 通道、数据后端与审计策略；
- 选择不同数据后端（InMemory / Redis / SQL / 向量库 / 对象存储 / 外部 Memory 服务）；
- 接入企业微信、Telegram 等 IM 通道；
- 在多节点上无状态水平扩展，消息正确路由到租户与 session；
- 具备治理、监控、安全、审计、故障恢复与灰度发布能力。

## 2. 总体架构（编排层方案）

在 SDK 之上构建 **Agent Gateway 编排层**，`tenant_id` 在 Gateway 层解析，落到 SDK 层即 `app_name = tenant_id` 命名空间。核心思路是**零侵入**：SDK 现有 Session/Memory/Storage 已按 `app_name` 隔离，直接复用。

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
```

### 组件职责

| 组件 | 职责 |
|------|------|
| **Agent Gateway** | 统一入口：租户解析、鉴权、限流、路由到 Worker |
| **Agent Worker** | 执行 `Runner.run_async`，维护租户 `Runner` 池，无状态可横向扩容 |
| **Channel Adapter** | IM 协议 ↔ 内部 `Content`/`Event` 转换、webhook 验签、去重 |
| **Storage Adapter** | 统一数据访问门面，按租户配置路由到具体后端 |
| **Admin API** | 租户 CRUD、配置下发、灰度发布、回滚、审计查询 |
| **Telemetry Collector** | OTel + Prometheus，按 `tenant_id` 维度聚合 |

## 3. 子项目划分

| # | 子项目 | 交付物 |
|---|--------|--------|
| SP1 | 租户模型 + 隔离 | `Tenant` 模型、`TenantRegistry`、`SecretStore`、`Masker` |
| SP2 | 数据访问 + 多后端 | `BackendFactory`、`StorageAdapter`、session 迁移 |
| SP3 | Runner 构建 + 路由 | `TenantAgentFactory`、`RunnerPool` |
| SP4 | IM Channel Adapter | `ChannelAdapter`、`WecomAdapter`、`TelegramAdapter`、`ChannelRouter`、`WeComCrypto` |
| SP5 | 治理 + 审计 | `AuditLogger`、`ToolPermissionFilter`、`BudgetFilter` |
| SP6 | 故障恢复 + 运维 | 运维手册、Docker Compose、Kubernetes manifests |

## 4. 租户模型与隔离

### 4.1 `Tenant` 模型

```python
class Tenant(BaseModel):
    tenant_id: str                    # 全局唯一，^[a-zA-Z0-9_-]{1,64}$
    app_config: AppConfig             # 默认 app_name、agent 描述、instruction
    model_settings: ModelConfig       # provider、model_name、endpoint、api_key_ref
    tool_permissions: ToolPermissions # 白名单/黑名单/危险工具确认
    im_channels: list[ChannelBinding] # 企微/Telegram 绑定
    data_backends: DataBackendConfig  # session/memory/... 各自后端
    audit_policy: AuditPolicy         # 审计开关/采样/保留期
    budgets: BudgetConfig             # token/成本预算
    rate_limits: RateLimitConfig      # 频率限制
    status: Literal["active", "disabled"]
    version: int                      # 单调递增，用于热更新/回滚
```

命名空间约定：SDK 层 `app_name = tenant.sdk_app_name = f"{tenant_id}:{app_config.app_name}"`。

### 4.2 隔离机制

| 维度 | 机制 |
|------|------|
| 配置隔离 | 每租户独立 `Tenant` 配置对象 |
| 数据隔离 | `app_name` 命名空间（SQL 列 / Redis 前缀 / 向量库 collection） |
| 工具权限隔离 | `ToolPermissionFilter` 运行时拦截 + 构建期白名单过滤 |
| 日志脱敏 | `Masker` / `SensitiveDataFilter` |
| 密钥管理 | `SecretStore`（Env/KMS/Vault），密钥永不落日志/trace/审计 |

## 5. 数据访问与多后端

- `BackendFactory`：按 `BackendSpec(type, dsn, secret_ref, options)` 实例化 Session/Memory/Artifact 服务，跨租户共享连接池（`spec.model_dump_json()` 作缓存键），`{password}` 占位符经 `SecretStore` 注入。
- `StorageAdapter`：按 `tenant_id` 解析并缓存后端，按 `tenant.version` 失效（配置热更新）。
- 迁移：`migrate_session` 逐 session 复制（幂等：目标已存在则跳过），源端全量读。

### 数据模型（核心表）

```
tenants (tenant_id PK, config JSON, status, version, update_time)
tenant_versions (tenant_id, version PK, config JSON)   -- 回滚快照
sessions / events / mem_events                         -- 复用 SDK StorageSession/Event/Memory
summary / channel_binding / audit_log                  -- 扩展表
```

## 6. Runner 构建与路由（无状态 Worker）

- `TenantAgentFactory.build_runner(tenant_id)`：`Tenant` → `LlmAgent`（模型经 `ModelRegistry` 构建 + `SecretStore` 注入 API key、工具按权限过滤）+ `Runner`（`app_name=tenant.sdk_app_name`）。
- `RunnerPool.get_runner(tenant_id)`：懒加载 + 按 `tenant.version` 失效重建 + per-tenant `asyncio.Lock` 防并发重复构建 + 替换时关闭旧 Runner。
- **无需 sticky session**：Session/Memory 在共享后端，Worker 无状态，任意节点可接管同一 session。

## 7. IM Channel Adapter

- `ChannelAdapter` 抽象：`verify_signature` / `parse_inbound` / `render_outbound` / `session_id` / `user_id`。
- 归一化模型 `InboundMessage` / `OutboundMessage`；`event_to_text` 排除思维链（`thought` part）。
- `ChannelRouter`：webhook → 租户绑定 + 验签 + 去重（`(channel, chat_id, msg_id)`，TTL）+ session/user 映射。
- session 规则：单聊 `chat_{channel}_{chat_id}`，群聊 `group_{channel}_{chat_id}`。
- 通道实现：Telegram（长轮询）、企业微信自建应用（回调 + AES 解密）、企业微信智能机器人（WebSocket 长连接）。

## 8. 治理、监控与安全

- 治理 Filter：`ToolPermissionFilter`（白/黑名单运行时拦截）、`BudgetFilter`（per-request token 限制），拦截时设置 `PermissionError`/`RuntimeError` 供审计 `error_type`。
- 审计：`AuditEvent`（tenant_id/channel/user_id/session_id/agent_name/tool_name/decision/latency/error_type/cost/trace_id）→ `AuditLogger`（仅脱敏内容字段）→ `AuditSink`。
- 指标/tracing：复用 SDK OTel `gen_ai.*` 指标与 span（`gen_ai.app.name` 即租户命名空间）。

## 9. 故障恢复与运维

- 降级：节点故障靠无状态 + readiness probe；IM 重试靠指数退避 + 幂等去重；模型超时靠 `ModelRetryConfig`；工具失败返回 error 让 agent 自愈。
- 灰度/回滚：`Tenant.version` + `TenantRegistry.rollback`，`RunnerPool`/`StorageAdapter` 按版本热更新。
- 部署：最小 Docker Compose（Gateway + Worker + Adapter + Redis + MySQL）；生产 Kubernetes（Deployment/Service/HPA + Secret）。

## 10. 目录结构

```
trpc_agent_sdk/
  tenant/          # SP1-5：模型、注册表、后端工厂/适配器、Runner 池、审计/治理
    _tenant.py  _secret_store.py  _masker.py  _registry.py  _tenant_source.py
    _backend_factory.py  _storage_adapter.py  _agent_factory.py  _runner_pool.py
    _audit.py  _governance.py
  channels/        # SP4：IM 适配器
    _messages.py  _base.py  _wecom.py  _telegram.py  _router.py  _wecom_crypto.py
examples/
  telegram_e2e/  wecom_e2e/  wecom_smartbot_e2e/
deploy/
  docker-compose.yml  k8s/  e2e/
```

## 11. 关键设计决策

1. **编排层方案**：不改 SDK 核心，`app_name` 作为租户键。
2. **无状态 Worker**：不依赖 sticky session，靠共享后端。
3. **密钥零明文**：`SecretStore` 引用注入，脱敏贯穿日志/trace/审计。
4. **配置热更新**：`tenant.version` 贯穿缓存失效（StorageAdapter + RunnerPool）。
5. **治理与审计解耦**：Filter 只拦截，审计由外层统一记录。
6. **长连接优先**：企业微信智能机器人用 WebSocket 长连接（免域名/HTTPS），自建应用用回调 + AES。
