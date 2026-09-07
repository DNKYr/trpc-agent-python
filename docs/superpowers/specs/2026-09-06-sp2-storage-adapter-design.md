# SP2 — 统一数据访问抽象与多后端 详细设计

> 父文档：`2026-09-06-multi-tenant-node-deployment-design.md`
> 状态：待评审
> 范围：`BackendFactory`（按 `BackendSpec` 实例化后端 + 连接池共享 + 密钥解析）、`StorageAdapter`（按租户解析/缓存后端门面）、session 数据迁移
> 明确**不包含**：summary / knowledge / audit 的独立后端（summary 内置在 session 服务 summarizer，knowledge 为向量库，audit 属 SP5）；memory/artifact 迁移；节点拓扑（SP3）

---

## 1. 目标

在 SP1 的 `Tenant`/`DataBackendConfig` 之上，提供统一数据访问门面：

- `BackendFactory`：由 `BackendSpec(type, dsn, secret_ref, options)` 实例化具体的 Session/Memory/Artifact 服务，跨租户共享连接池，密钥从 `SecretStore` 解析注入。
- `StorageAdapter`：给定 `tenant_id`，按该租户 `data_backends` 配置解析并缓存 session/memory/artifact 后端。
- `migrate_session`：把某租户的 session 数据从一个后端迁移到另一个后端。

## 2. 关键决策（锁定）

1. **复用 SDK 现有后端实现，不新造存储层**：Session 用 `InMemory/Redis/RedisCluster/SqlSessionService`，Memory 用 `InMemory/Redis/SqlMemoryService`，Artifact 用 `InMemoryArtifactService`。SP2 只做"选择与编排"。

2. **连接池共享**：`BackendFactory` 以 `BackendSpec.model_dump_json()` 为缓存键；同 type+dsn 的多个租户共享同一服务实例（连接池）。

3. **DSN 密钥注入**：`BackendSpec.dsn` 可含 `{password}` 占位符；`BackendFactory` 用 `secret_ref` 从 `SecretStore` 取值后替换占位符。`secret_ref` 存在但 dsn 无占位符时，将 secret 放入 `options`（如 `password=`）。

4. **`is_async`**：`BackendFactory` 构造参数 `is_async: bool = False` 作为默认；单个 `BackendSpec.options` 里的 `is_async` 键可覆盖。

5. **迁移 = 一次性的运维动作**：`migrate_session(tenant_id, source, target)` 显式给定源/目标 `BackendSpec`，不依赖租户当前配置；按 `tenant.sdk_app_name` 命名空间读取源、重建到目标。

6. **artifact 仅 `in_memory`**：对象存储后端推迟到后续 SP；`artifact_service` 对其他 type 抛 `ValueError`。

---

## 3. 文件结构

```
trpc_agent_sdk/tenant/
    __init__.py               # 追加导出 BackendFactory / StorageAdapter
    _backend_factory.py       # BackendFactory
    _storage_adapter.py       # StorageAdapter + migrate_session

tests/tenant/
    test_backend_factory.py
    test_storage_adapter.py
    test_session_migration.py
```

> 复用 SP1 的 `BackendSpec` / `DataBackendConfig` / `SecretStore` / `TenantRegistry`。

---

## 4. `BackendFactory`

```python
from trpc_agent_sdk.abc import ArtifactServiceABC
from trpc_agent_sdk.memory import BaseMemoryService
from trpc_agent_sdk.sessions import BaseSessionService

class BackendFactory:
    """按 BackendSpec 实例化并缓存后端服务。"""

    def __init__(self, secret_store: SecretStore | None = None, is_async: bool = False):
        self._secret_store = secret_store
        self._is_async = is_async
        self._session_cache: dict[str, BaseSessionService] = {}
        self._memory_cache: dict[str, BaseMemoryService] = {}
        self._artifact_cache: dict[str, ArtifactServiceABC] = {}

    async def session_service(self, spec: BackendSpec) -> BaseSessionService:
        key = spec.model_dump_json()
        if key not in self._session_cache:
            self._session_cache[key] = await self._build_session(spec)
        return self._session_cache[key]

    async def memory_service(self, spec: BackendSpec) -> BaseMemoryService:
        # 同 session，构建时 enabled=True（由 options 可覆盖）
        ...

    async def artifact_service(self, spec: BackendSpec) -> ArtifactServiceABC:
        # 仅支持 type == "in_memory"

    async def close(self) -> None:
        # 依次 close 所有缓存的服务

    async def _build_session(self, spec: BackendSpec) -> BaseSessionService:
        options = dict(spec.options)
        # 保证全量读（选项 1）：session 读窗口由 factory 统一控制，丢弃 options 中的 session_config
        options.pop("session_config", None)
        is_async = bool(options.pop("is_async", self._is_async))
        if spec.type == "in_memory":
            return InMemorySessionService(**options)
        dsn = await self._resolve_dsn(spec)
        if spec.type == "redis":
            return RedisSessionService(db_url=dsn, is_async=is_async, **options)
        if spec.type == "sql":
            return SqlSessionService(db_url=dsn, is_async=is_async, **options)
        raise ValueError(f"unsupported session backend type: {spec.type}")

    async def _resolve_dsn(self, spec: BackendSpec) -> str:
        dsn = spec.dsn or ""
        if spec.secret_ref:
            if self._secret_store is None:
                raise ValueError("secret_ref set but no SecretStore configured")
            secret = await self._secret_store.get(spec.secret_ref)
            dsn = dsn.replace("{password}", secret)
        return dsn
```

**支持的 type 与实现映射：**

| 服务 | type | 实现 |
|------|------|------|
| session | `in_memory` | `InMemorySessionService` |
| session | `redis` | `RedisSessionService` |
| session | `sql` | `SqlSessionService` |
| memory | `in_memory` | `InMemoryMemoryService(enabled=True)` |
| memory | `redis` | `RedisMemoryService(enabled=True)` |
| memory | `sql` | `SqlMemoryService(enabled=True)` |
| artifact | `in_memory` | `InMemoryArtifactService` |

---

## 5. `StorageAdapter`

```python
class StorageAdapter:
    """按租户 data_backends 解析并缓存数据后端。"""

    def __init__(self, registry: TenantRegistry, factory: BackendFactory):
        self._registry = registry
        self._factory = factory
        self._session_cache: dict[str, BaseSessionService] = {}
        self._memory_cache: dict[str, BaseMemoryService] = {}
        self._artifact_cache: dict[str, ArtifactServiceABC] = {}

    async def session_service(self, tenant_id: str) -> BaseSessionService:
        tenant = await self._get_tenant(tenant_id)
        if tenant_id not in self._session_cache:
            self._session_cache[tenant_id] = await self._factory.session_service(tenant.data_backends.session)
        return self._session_cache[tenant_id]

    async def memory_service(self, tenant_id: str) -> BaseMemoryService: ...
    async def artifact_service(self, tenant_id: str) -> ArtifactServiceABC: ...

    async def _get_tenant(self, tenant_id: str) -> Tenant:
        tenant = await self._registry.get(tenant_id)
        if tenant is None:
            raise TenantNotFoundError(tenant_id)
        return tenant

    async def close(self) -> None:
        # 委托 factory.close() 关闭共享连接池
        await self._factory.close()
```

**要点：**
- 后端缓存按 `tenant_id` 维度（每个租户一个 session/memory/artifact 引用），底层连接池由 `BackendFactory` 跨租户共享。
- `memory_service` 返回的服务 `enabled=True`，供 SP3 的 `Runner` 使用（`Runner` 检查 `memory_service.enabled`）。
- 租户禁用（`status != active`）→ `_get_tenant` 抛 `TenantNotFoundError`。

---

## 6. 迁移（session）

```python
    async def migrate_session(self, *, tenant_id: str,
                              source: BackendSpec, target: BackendSpec) -> int:
        """
        把 tenant 的 session 数据从 source 后端迁移到 target 后端。

        流程：
        1. app_name = tenant.sdk_app_name
        2. src = factory.session_service(source); dst = factory.session_service(target)
        3. for s in (await src.list_sessions(app_name=app_name)).sessions:
               if await dst.get_session(app_name=s.app_name, user_id=s.user_id, session_id=s.id):
                   continue   # 目标已存在该 session，跳过（幂等）
               full = await src.get_session(app_name=s.app_name, user_id=s.user_id, session_id=s.id)
               await dst.create_session(app_name=..., user_id=..., session_id=..., state=full.state)
               for event in full.events:
                   await dst.append_event(session=..., event=event)
        4. return 迁移的 session 数（本次新迁移的数量）
        """
```

**迁移语义与边界：**
- **全量读（选项 1）**：由 `BackendFactory` 保证——session 服务始终以默认 `SessionServiceConfig`（`num_recent_events=0`，即"无限制读全部"）构建，并丢弃 `options` 中的 `session_config`，因此 `get_session` 不会截断历史事件；迁移使用工厂缓存的同一实例（对 InMemory 源即持有数据的实例，对 SQL/Redis 源即读共享存储的实例）。
- **幂等（session 级）**：迁移前先查目标，已存在的 session 跳过；重复执行 `migrate_session` 不会重复追加事件（`append_event` 对同一 `event.id` 重复写入会触发 SQL 主键冲突，故以"跳过已存在 session"保证幂等）。
- 迁移是"复制"而非"删除源"：迁移完成后由运维显式停用/清理源后端（双写→批迁→读切换的"批迁"阶段）。
- `list_sessions` 返回的 session 无 events（`events=[]`），需逐 session `get_session` 取全量再重建。

---

## 7. 数据同步与一致性（设计层面，已在父文档 §5.3，此处为 SP2 落地映射）

| 关注点 | SP2 落地 |
|--------|----------|
| 多节点并发写同一 session | 复用 SDK 既有机制：`event.id` 主键幂等 + `update_time` 乐观比较（`_sql_session_service.py` stale 检测） |
| event/state/summary 顺序 | 复用 `Runner` post-turn 串行化（`runners.py`），SP2 不另做 |
| Memory 跨节点可见性 | 后端写共享存储；SP2 只保证"服务实例共享"，一致性由后端决定 |
| IM 幂等 | SP4（channel 层 `(channel,msg_id)` 去重），SP2 不涉及 |
| 迁移（Redis→SQL） | 本节 `migrate_session`（复制式，逐 session 重建） |

---

## 8. 测试策略

| 测试文件 | 覆盖 |
|----------|------|
| `test_backend_factory.py` | 各 type→实现映射；缓存共享（同 spec 返回同实例）；`{password}` 占位符替换；`secret_ref` 无 SecretStore 抛错；不支持 type 抛错；`close` 关闭缓存服务 |
| `test_storage_adapter.py` | 按 tenant_id 解析后端并缓存；缺失/禁用租户抛 `TenantNotFoundError`；不同租户不同后端不串 |
| `test_session_migration.py` | InMemory→SQL、SQL→InMemory、InMemory→InMemory 迁移；事件顺序保持；重复迁移幂等（不产生重复事件）；返回迁移 session 数 |

> 测试后端用 InMemory + SQLite（`sqlite:///:memory:`），不依赖外部 Redis。

---

## 9. 已确认决策（评审结论）

1. **后端覆盖**：仅 session / memory / artifact（summary/knowledge/audit 推迟）。
2. **迁移范围**：仅 session（memory/artifact 迁移推迟）。
3. **artifact 后端**：仅 `in_memory`（对象存储推迟）。
4. **迁移全量读（选项 1）**：由 `BackendFactory` 保证 session 服务始终默认全量读（`num_recent_events=0`），并丢弃 `options` 中的 `session_config`；迁移复用工厂缓存实例。
4. **包位置**：`trpc_agent_sdk/tenant/` 内新增 `_backend_factory.py` + `_storage_adapter.py`。

---

## 10. 已知限制（后续 SP 处理）

1. **租户级缓存不随配置变更失效**：`StorageAdapter` 按 `tenant_id` 缓存后端，`TenantRegistry.put` 更新 `data_backends` 后仍返回旧后端（进程重启前）。~~待 SP3 定义配置热加载语义时，改为按后端 spec / `tenant.version` 作缓存键。~~ **已于 SP3 修复**：`StorageAdapter` 三个服务 getter 改为按 `tenant.version` 失效。
2. **迁移中断可能丢失事件**：`migrate_session` 先 `create_session` 再逐 event `append_event`，若中途失败，重试时 `get_session` 非空即跳过，缺事件不再补齐。待实现 per-session 事务/补偿。
3. **密钥轮换不达缓存后端**：`BackendFactory` 缓存键不含解析后的 secret，`SecretStore` 更新后旧连接池仍用旧密钥直到重启。待后续提供缓存失效或密钥注入重算。
