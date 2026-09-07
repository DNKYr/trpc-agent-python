# SP3 — 节点拓扑（进程内）与路由 详细设计

> 父文档：`2026-09-06-multi-tenant-node-deployment-design.md`
> 状态：待评审
> 范围：`TenantAgentFactory`（由 `Tenant` 构建 `Runner`/`Agent`）、`RunnerPool`（`tenant_id` → `Runner` 路由 + 懒加载 + 配置变更失效）
> 明确**不包含**：网络/HTTP Gateway 服务与 Worker 进程（SP6 部署）、Channel Adapter（SP4）、Admin API/遥测/审计（SP5）

---

## 1. 目标

在 SP1（租户模型）+ SP2（数据后端门面）之上，打通"配置 → Runner"的最后一环：

- `TenantAgentFactory`：按 `Tenant` 的 `model_settings`/`tool_permissions`/`app_config` 构建 `LlmAgent` 与 `Runner`（模型从 `SecretStore` 解析 API key，工具按白名单过滤）。
- `RunnerPool`：`tenant_id` → `Runner` 的路由与缓存，懒加载构建，租户配置版本变更时重建。

## 2. 关键决策（锁定）

1. **agent name 不能用 `sdk_app_name`**：`sdk_app_name = f"{tenant_id}:{app_name}"` 含 `:`，不满足 SDK 对 agent `name` "Python 标识符"的约定（`abc/_agent.py`、`agents/sub_agent/_runner.py:193`）。改用规范化标识符 `_agent_name(tenant_id) = "agent_" + tenant_id.replace("-", "_")`（`tenant_id` 为 `[a-zA-Z0-9_-]`，替换后必为合法标识符）。`Runner.app_name` 仍用 `tenant.sdk_app_name`（数据隔离键，与 agent name 解耦）。

2. **模型构建走 `ModelRegistry`**：`ModelRegistry.create_model(model_settings.model_name, api_key=..., base_url=..., **extra)`。`provider` 字段在 SP3 仅作信息/校验用（不参与类选择，按 `model_name` 正则解析）。`api_key_ref` 从 `SecretStore` 解析注入；`endpoint` → `base_url`。`temperature`/`timeout_seconds` 在 SP3 不接线（经 `extra` 透传，后续 SP 处理）。

3. **工具过滤**：`TenantAgentFactory` 持有全局工具注册表 `dict[str, BaseTool]`；`allow_all=True` 取全量，否则 `allowlist - denylist`；`require_confirmation` 列表仅透传数据（SP5 装配 Filter）。

4. **Runner 不关闭共享服务**：构建 `Runner` 时 `close_session_service_on_close=False`、`close_memory_service_on_close=False`（session/memory 连接池归 `BackendFactory`/`StorageAdapter` 所有，多租户共享）。

5. **配置变更失效**：`RunnerPool` 按 `(tenant_id, tenant.version)` 缓存；`get_runner` 时若 `tenant.version` 变化则重建。这补齐 SP2 已知限制 #1。

6. **无状态 Worker**：`Runner` 不持有会话本地状态，Session/Memory 在共享后端；`RunnerPool` 可安全在多节点各自实例化（SP3 仅进程内，跨节点一致性由共享后端保证）。

---

## 3. 文件结构

```
trpc_agent_sdk/tenant/
    __init__.py                 # 追加导出 TenantAgentFactory / RunnerPool
    _agent_factory.py           # TenantAgentFactory
    _runner_pool.py             # RunnerPool

tests/tenant/
    test_agent_factory.py
    test_runner_pool.py
```

> 复用 SP1 的 `Tenant`/`ModelConfig`/`ToolPermissions`/`TenantRegistry`/`SecretStore`/`TenantNotFoundError`，SP2 的 `StorageAdapter`/`BackendFactory`。

---

## 4. `TenantAgentFactory`

```python
from trpc_agent_sdk.agents import LlmAgent
from trpc_agent_sdk.models import LLMModel
from trpc_agent_sdk.models import ModelRegistry
from trpc_agent_sdk.runners import Runner
from trpc_agent_sdk.tools import BaseTool


class TenantAgentFactory:
    """Build Runner/Agent from a Tenant configuration."""

    def __init__(self, registry: TenantRegistry, storage: StorageAdapter,
                 secret_store: SecretStore, tool_registry: dict[str, BaseTool] | None = None):
        self._registry = registry
        self._storage = storage
        self._secret_store = secret_store
        self._tool_registry = tool_registry or {}

    async def build_runner(self, tenant_id: str) -> Runner:
        tenant = await self._get_tenant(tenant_id)
        model = await self._build_model(tenant.model_settings)
        tools = self._filter_tools(tenant.tool_permissions)
        agent = LlmAgent(name=_agent_name(tenant.tenant_id), model=model,
                         instruction=tenant.app_config.instruction, tools=tools)
        session_service = await self._storage.session_service(tenant_id)
        memory_service = await self._storage.memory_service(tenant_id)
        artifact_service = await self._storage.artifact_service(tenant_id)
        return Runner(app_name=tenant.sdk_app_name, agent=agent,
                      session_service=session_service, memory_service=memory_service,
                      artifact_service=artifact_service,
                      close_session_service_on_close=False,
                      close_memory_service_on_close=False)

    async def _get_tenant(self, tenant_id: str) -> Tenant:
        tenant = await self._registry.get(tenant_id)
        if tenant is None:
            raise TenantNotFoundError(tenant_id)
        return tenant

    async def _build_model(self, config: ModelConfig) -> LLMModel:
        kwargs = dict(config.extra)
        if config.endpoint:
            kwargs["base_url"] = config.endpoint
        if config.api_key_ref:
            kwargs["api_key"] = await self._secret_store.get(config.api_key_ref)
        return ModelRegistry.create_model(config.model_name, **kwargs)

    def _filter_tools(self, permissions: ToolPermissions) -> list[BaseTool]:
        if permissions.allow_all:
            return list(self._tool_registry.values())
        allowed = set(permissions.allowlist) - set(permissions.denylist)
        return [self._tool_registry[name] for name in allowed if name in self._tool_registry]


def _agent_name(tenant_id: str) -> str:
    """Derive a valid Python identifier for the agent name (SDK requires identifier)."""
    return "agent_" + tenant_id.replace("-", "_")
```

**要点：**
- `Runner.app_name = tenant.sdk_app_name`（数据隔离键）；`LlmAgent.name` 用 `_agent_name(tenant_id)`（合法标识符）。
- 模型 `api_key` 经 `SecretStore.get(api_key_ref)` 解析，绝不落日志；缺密钥抛 `SecretNotFoundError`。
- `memory_service` 由 SP2 的 `StorageAdapter` 提供（`enabled=True`），`Runner` 会据此做 post-turn memory 持久化。

---

## 5. `RunnerPool`

```python
class RunnerPool:
    """Route tenant_id -> Runner with lazy construction and config-change invalidation."""

    def __init__(self, registry: TenantRegistry, factory: TenantAgentFactory):
        self._registry = registry
        self._factory = factory
        self._runners: dict[str, Runner] = {}
        self._versions: dict[str, int] = {}

    async def get_runner(self, tenant_id: str) -> Runner:
        tenant = await self._registry.get(tenant_id)
        if tenant is None:
            raise TenantNotFoundError(tenant_id)
        cached = self._runners.get(tenant_id)
        if cached is not None and self._versions.get(tenant_id) == tenant.version:
            return cached
        lock = self._locks.setdefault(tenant_id, asyncio.Lock())
        async with lock:
            cached = self._runners.get(tenant_id)
            if cached is not None and self._versions.get(tenant_id) == tenant.version:
                return cached
            if cached is not None:
                await cached.close()          # 替换前关闭旧 runner（不关共享服务）
            runner = await self._factory.build_runner(tenant_id)
            self._runners[tenant_id] = runner
            self._versions[tenant_id] = tenant.version
            return runner

    async def close(self) -> None:
        for runner in self._runners.values():
            await runner.close()
        self._runners.clear()
        self._versions.clear()
```

**要点：**
- 首次 `get_runner` 懒构建；`tenant.version` 变化时重建（配置热更新）。
- **配置变更失效（补齐 SP2 限制 #1）**：`StorageAdapter` 的三个服务 getter 改为按 `tenant.version` 失效（版本变化时重建后端，见 SP2 补丁）；`RunnerPool` 同步按版本重建 Runner。因此 `data_backends` 变更（如 Redis→SQL）会拿到新后端。
- **替换时关闭旧 runner**：重建前 `await cached.close()`（`Runner.close` 不会关共享 session/memory 服务）。
- **并发去重**：每租户 `asyncio.Lock` 双检锁，避免首次/变更时的重复构建与泄漏。
- 路由到正确 session 由 SDK `Runner.run_async(user_id, session_id, new_message)` 完成——session 由 `app_name=tenant.sdk_app_name` 命名空间隔离，Gateway 只需拿到正确 tenant 的 `Runner` 再调用 `run_async`。

---

## 6. 测试策略

| 测试文件 | 覆盖 |
|----------|------|
| `test_agent_factory.py` | 模型构建（`ModelRegistry.create_model` 收到正确 model_name/api_key/base_url）；API key 从 SecretStore 解析；工具白名单/黑名单/allow_all 过滤；agent `name` 是合法标识符；`Runner.app_name == tenant.sdk_app_name`；`close_session_service_on_close=False`；缺失/禁用租户抛 `TenantNotFoundError` |
| `test_runner_pool.py` | 首次懒构建；缓存命中返回同实例；`tenant.version` 变更后重建（新实例）；缺失租户抛错；`close` 后缓存清空 |

> 测试中模型用 mock/未注册名或断言 `create_model` 调用参数；用 `InMemorySecretStore` 提供 key；用 `InMemoryTenantSource` + SP2 `BackendFactory`（session=in_memory）。不真正调用 LLM。

---

## 7. 已确认决策（评审结论）

1. **范围**：仅进程内 runner 构建 + 路由（`TenantAgentFactory` + `RunnerPool`）；网络拓扑/HTTP/Worker 留 SP6。
2. **agent name**：`_agent_name(tenant_id)` 规范化，不用含 `:` 的 `sdk_app_name`。
3. **模型构建**：`ModelRegistry.create_model`（按 `model_name` 解析）；`provider` 仅信息用。
4. **配置变更失效**：`StorageAdapter` 后端缓存与 `RunnerPool` 均按 `tenant.version` 失效，`data_backends`/模型/工具变更均重建（补齐 SP2 限制 #1）。
