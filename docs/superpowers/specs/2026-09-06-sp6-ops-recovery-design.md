# SP6 — 故障恢复与运维 详细设计（运维手册）

> 父文档：`2026-09-06-multi-tenant-node-deployment-design.md`
> 状态：待评审
> 范围：降级策略、灰度发布与回滚、容量评估、最小/生产部署方案（Docker Compose + Kubernetes）
> 交付物：本运维手册 + `deploy/docker-compose.yml` + `deploy/k8s/*.yaml`

---

## 1. 降级策略

| 故障 | 降级策略 | 现有支撑 |
|------|----------|----------|
| 节点故障 | 健康检查摘除 + 重路由到健康 Worker；无状态（Session/Memory 在共享后端），任意节点可接管 | SP3 `RunnerPool`（无状态）；部署层 readiness probe |
| IM 重试 | 指数退避 + 幂等去重；失败消息进死信/降级为异步延迟回复 | SP4 `ChannelRouter`（`(channel,chat_id,msg_id)` 去重）；`ExponentialBackoffConfig` |
| 数据库短暂不可用 | 熔断（circuit breaker）→ 快速失败/只读降级；恢复后半开→全开 | SP2 `StorageAdapter`/`BackendFactory`（后端抽象，可换） |
| 模型超时 | 有限重试（`ModelRetryConfig` num_retries/backoff）+ 超时上限；最终返回 error event | 已有 `_model_retry_config.py` |
| 工具执行失败 | 工具返回 error → agent 自愈（retry/换策略）；`RunConfig.max_tool_calls` 限次 | SDK tool 机制 + `RunConfig` |

**降级原则**：优先「可用性 > 一致性」，失败快速、可观测（审计 `error_type` + trace），不在热路径阻塞。

---

## 2. 灰度发布与回滚

- **版本化**：`Tenant.version`（SP1）每次配置变更递增；`TenantRegistry` 保留历史快照（`tenant_versions` 表）。
- **灰度（canary）**：按租户（或租户内百分比）切流。做法：新配置写入一个**副本租户**或按 `version` 分流的 `RuleConfig`（分流规则：`tenant_id` 哈希 → 命中比例）。逐步放量：1% → 10% → 50% → 100%。
- **回滚**：`TenantRegistry.rollback(tenant_id, version)` 一键回退到上一稳定版本；SP3 `RunnerPool` 已按 `tenant.version` 失效重建 Runner，回滚即时生效。
- **配置变更通知**：`TenantRegistry.subscribe` 推送 `(tenant_id, version)`（SP1），供 Gateway 热加载。

---

## 3. 容量评估

估算公式（压测基线为准）：

| 指标 | 公式 |
|------|------|
| 每节点并发 session | `节点 CPU 核数 × 并发因子`（并发因子受模型 I/O 与事件流带宽约束，实测 0.5–2） |
| 平均 token 消耗 | `并发 session × 每 session 平均 token`（用于成本与预算 `BudgetConfig`） |
| Redis/SQL QPS | `并发 session × 每 session 每秒 event 读写次数`（写事件 + 读历史 + memory 读写） |
| IM 回调峰值 | `活跃用户 × 峰值消息速率`（用于 Channel Adapter 扩容与限流 `RateLimitConfig`） |

**示例基线**（每节点 8 核，中等模型）：~16–32 并发 session；Redis/SQL 需支撑 ~1–3k QPS/节点；IM 回调峰值按 `峰值=日活×0.5%×10 msg/min` 粗估。

**扩展点**：Gateway/Worker/Adapter 无状态，横向扩容线性；瓶颈在共享后端（Redis/SQL/向量库），需独立扩缩容与连接池调优。

---

## 4. 部署方案

### 4.1 最小可运行（Docker Compose）

组件：`gateway`（入口/路由）+ `worker`（Runner）+ `channel-adapter`（IM webhook）+ `redis` + `mysql`。见 `deploy/docker-compose.yml`。

### 4.2 生产推荐（Kubernetes）

- `Gateway` / `Worker` / `Channel Adapter` 独立 `Deployment` + `Service` + `HPA`（CPU/并发指标自动扩缩）。
- `Redis` / `SQL` / 向量库 用托管服务（或 StatefulSet）；OTel Collector 以 sidecar/DaemonSet 注入。
- 密钥经 `Secret`（对应 `SecretStore`）；配置经 `ConfigMap`。
- 见 `deploy/k8s/`（`gateway.yaml`、`worker.yaml`、`adapter.yaml`、`redis.yaml`、`hpa.yaml`）。

---

## 5. 已确认决策（评审结论）

1. **交付物**：运维手册 + 部署配置（Docker Compose + K8s manifests），不含降级代码（复用 `ModelRetryConfig` 等现有机制）。
2. **降级代码**：不做（重试/熔断复用 SDK 现有，或运维层处理）。
3. **部署配置位置**：`deploy/` 目录（`docker-compose.yml` + `k8s/`）。
