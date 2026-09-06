# SP2 — 统一数据访问抽象与多后端 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `trpc_agent_sdk/tenant` 包内新增 `BackendFactory`（按 `BackendSpec` 实例化并缓存后端 + 密钥解析）与 `StorageAdapter`（按租户解析后端 + session 迁移），实现 session/memory/artifact 三类数据后端的统一访问门面。

**Architecture:** 纯新增，复用 SDK 现有后端实现（`InMemory/Redis/SqlSessionService`、`InMemory/Redis/SqlMemoryService`、`InMemoryArtifactService`）。`BackendFactory` 以 `spec.model_dump_json()` 为缓存键跨租户共享连接池；session 服务始终默认全量读（丢弃 `options` 中的 `session_config`）；`StorageAdapter` 按 `tenant_id` 缓存后端，`migrate_session` 以 session 级跳过实现幂等。

**Tech Stack:** Python 3.10+、pydantic v2、SQLAlchemy 2.0、pytest + pytest-asyncio（`asyncio_mode=auto`）、`unittest.mock`。

## Global Constraints

- 包位置：`trpc_agent_sdk/tenant/`（新增 `_backend_factory.py`、`_storage_adapter.py`）。
- 复用 SP1 的 `BackendSpec` / `DataBackendConfig` / `SecretStore` / `TenantRegistry` / `TenantNotFoundError`（均已存在）。
- session 服务全量读：`_build_session` 必须 `options.pop("session_config", None)`，保证 `num_recent_events=0`。
- 幂等：`migrate_session` 迁移前查目标 `get_session`，已存在则跳过该 session。
- 测试命令：`.venv/bin/pytest tests/tenant/... -v`（**必须用 `.venv/bin/pytest`，勿用裸 `pytest`**）。
- Redis/SQL 后端构造是惰性的（不连接），测试可安全 `isinstance` 断言，不调用 DB 操作。
- 行宽 120；flake8 忽略 `E402, W503`；不要用 `...` 一行体（E704）。
- 提交信息前缀：`feat(tenant):`。

---

## 文件结构

```
trpc_agent_sdk/tenant/
    _backend_factory.py    # BackendFactory（Task 1 建 session，Task 2 加 memory/artifact）
    _storage_adapter.py    # StorageAdapter（Task 3 建解析，Task 4 加迁移）

tests/tenant/
    test_backend_factory.py
    test_storage_adapter.py
    test_session_migration.py
```

---

### Task 1: BackendFactory — session 服务

**Files:**
- Create: `trpc_agent_sdk/tenant/_backend_factory.py`
- Create: `tests/tenant/test_backend_factory.py`

**Interfaces:**
- Consumes: `BackendSpec`、`SecretStore`（SP1）；`InMemorySessionService`/`RedisSessionService`/`SqlSessionService`（`trpc_agent_sdk.sessions`）。
- Produces: `BackendFactory(secret_store=None, is_async=False)`，方法 `async session_service(spec) -> BaseSessionService`、`async close()`；私有 `_build_session`、`_resolve_dsn`。

- [ ] **Step 1: 写失败测试**

`tests/tenant/test_backend_factory.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for BackendFactory session service."""

from unittest.mock import patch

import pytest

from trpc_agent_sdk.sessions import InMemorySessionService
from trpc_agent_sdk.sessions import RedisSessionService
from trpc_agent_sdk.sessions import SqlSessionService

from trpc_agent_sdk.tenant._backend_factory import BackendFactory
from trpc_agent_sdk.tenant._secret_store import InMemorySecretStore
from trpc_agent_sdk.tenant._tenant import BackendSpec


async def test_session_in_memory():
    factory = BackendFactory()
    svc = await factory.session_service(BackendSpec(type="in_memory"))
    assert isinstance(svc, InMemorySessionService)


async def test_session_sql():
    factory = BackendFactory()
    svc = await factory.session_service(BackendSpec(type="sql", dsn="sqlite:///:memory:"))
    assert isinstance(svc, SqlSessionService)
    await factory.close()


async def test_session_redis():
    factory = BackendFactory()
    svc = await factory.session_service(BackendSpec(type="redis", dsn="redis://localhost:6379/0"))
    assert isinstance(svc, RedisSessionService)


async def test_session_cache_shares_instance():
    factory = BackendFactory()
    spec = BackendSpec(type="in_memory")
    assert (await factory.session_service(spec)) is (await factory.session_service(spec))


async def test_unsupported_session_type_raises():
    factory = BackendFactory()
    with pytest.raises(ValueError):
        await factory.session_service(BackendSpec(type="nosql"))


async def test_session_password_injected():
    store = InMemorySecretStore({"pw": "s3cr3t"})
    factory = BackendFactory(secret_store=store)
    spec = BackendSpec(type="redis", dsn="redis://:{password}@localhost:6379/0", secret_ref="pw")
    with patch("trpc_agent_sdk.tenant._backend_factory.RedisSessionService") as mock_cls:
        await factory.session_service(spec)
        assert mock_cls.call_args.kwargs["db_url"] == "redis://:s3cr3t@localhost:6379/0"


async def test_secret_ref_without_store_raises():
    factory = BackendFactory()
    spec = BackendSpec(type="redis", dsn="redis://:{password}@localhost:6379/0", secret_ref="pw")
    with pytest.raises(ValueError):
        await factory.session_service(spec)
```

- [ ] **Step 2: 运行测试验证失败**

Run: `.venv/bin/pytest tests/tenant/test_backend_factory.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'trpc_agent_sdk.tenant._backend_factory'`）

- [ ] **Step 3: 写最小实现**

`trpc_agent_sdk/tenant/_backend_factory.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Backend factory for instantiating data services from BackendSpec."""

from __future__ import annotations

from typing import Optional

from trpc_agent_sdk.sessions import BaseSessionService
from trpc_agent_sdk.sessions import InMemorySessionService
from trpc_agent_sdk.sessions import RedisSessionService
from trpc_agent_sdk.sessions import SqlSessionService

from ._secret_store import SecretStore
from ._tenant import BackendSpec


class BackendFactory:
    """Instantiate and cache data backend services from BackendSpec."""

    def __init__(self, secret_store: Optional[SecretStore] = None, is_async: bool = False):
        self._secret_store = secret_store
        self._is_async = is_async
        self._session_cache: dict[str, BaseSessionService] = {}

    async def session_service(self, spec: BackendSpec) -> BaseSessionService:
        key = spec.model_dump_json()
        if key not in self._session_cache:
            self._session_cache[key] = await self._build_session(spec)
        return self._session_cache[key]

    async def close(self) -> None:
        for svc in list(self._session_cache.values()):
            await svc.close()
        self._session_cache.clear()

    async def _build_session(self, spec: BackendSpec) -> BaseSessionService:
        options = dict(spec.options)
        # Full read: the factory owns the session read window (num_recent_events stays 0).
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

- [ ] **Step 4: 运行测试验证通过**

Run: `.venv/bin/pytest tests/tenant/test_backend_factory.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```bash
git add trpc_agent_sdk/tenant/_backend_factory.py tests/tenant/test_backend_factory.py
git commit -m "feat(tenant): add BackendFactory session service"
```

---

### Task 2: BackendFactory — memory 与 artifact 服务

**Files:**
- Modify: `trpc_agent_sdk/tenant/_backend_factory.py`（追加 memory/artifact 缓存、`_build_memory`、`_build_artifact`；`close` 增加 memory 关闭）
- Modify: `tests/tenant/test_backend_factory.py`（追加测试）

**Interfaces:**
- Consumes: `InMemoryMemoryService`/`RedisMemoryService`/`SqlMemoryService`（`trpc_agent_sdk.memory`）、`InMemoryArtifactService`（`trpc_agent_sdk.artifacts`）、`ArtifactServiceABC`（`trpc_agent_sdk.abc`）。
- Produces: `async memory_service(spec) -> BaseMemoryService`（`enabled=True` 默认）、`async artifact_service(spec) -> ArtifactServiceABC`（仅 `in_memory`）。

- [ ] **Step 1: 写失败测试**

在 `tests/tenant/test_backend_factory.py` 末尾追加：
```python
from trpc_agent_sdk.artifacts import InMemoryArtifactService
from trpc_agent_sdk.memory import InMemoryMemoryService
from trpc_agent_sdk.memory import RedisMemoryService
from trpc_agent_sdk.memory import SqlMemoryService


async def test_memory_in_memory_enabled():
    factory = BackendFactory()
    svc = await factory.memory_service(BackendSpec(type="in_memory"))
    assert isinstance(svc, InMemoryMemoryService)
    assert svc.enabled is True


async def test_memory_sql():
    factory = BackendFactory()
    svc = await factory.memory_service(BackendSpec(type="sql", dsn="sqlite:///:memory:"))
    assert isinstance(svc, SqlMemoryService)
    await factory.close()


async def test_memory_redis():
    factory = BackendFactory()
    svc = await factory.memory_service(BackendSpec(type="redis", dsn="redis://localhost:6379/0"))
    assert isinstance(svc, RedisMemoryService)


async def test_memory_cache_shares_instance():
    factory = BackendFactory()
    spec = BackendSpec(type="in_memory")
    assert (await factory.memory_service(spec)) is (await factory.memory_service(spec))


async def test_unsupported_memory_type_raises():
    factory = BackendFactory()
    with pytest.raises(ValueError):
        await factory.memory_service(BackendSpec(type="nosql"))


async def test_artifact_in_memory():
    factory = BackendFactory()
    svc = await factory.artifact_service(BackendSpec(type="in_memory"))
    assert isinstance(svc, InMemoryArtifactService)


async def test_unsupported_artifact_type_raises():
    factory = BackendFactory()
    with pytest.raises(ValueError):
        await factory.artifact_service(BackendSpec(type="object"))
```

- [ ] **Step 2: 运行测试验证失败**

Run: `.venv/bin/pytest tests/tenant/test_backend_factory.py -v`
Expected: FAIL（`AttributeError: 'BackendFactory' object has no attribute 'memory_service'`）

- [ ] **Step 3: 写最小实现**

在 `_backend_factory.py` 顶部追加 imports：
```python
from trpc_agent_sdk.abc import ArtifactServiceABC
from trpc_agent_sdk.artifacts import InMemoryArtifactService
from trpc_agent_sdk.memory import BaseMemoryService
from trpc_agent_sdk.memory import InMemoryMemoryService
from trpc_agent_sdk.memory import RedisMemoryService
from trpc_agent_sdk.memory import SqlMemoryService
```

在 `__init__` 中追加两个缓存字段（在 `_session_cache` 之后）：
```python
        self._memory_cache: dict[str, BaseMemoryService] = {}
        self._artifact_cache: dict[str, ArtifactServiceABC] = {}
```

将 `close` 方法替换为：
```python
    async def close(self) -> None:
        for svc in list(self._session_cache.values()):
            await svc.close()
        for svc in list(self._memory_cache.values()):
            await svc.close()
        self._session_cache.clear()
        self._memory_cache.clear()
        self._artifact_cache.clear()
```

在 `_build_session` 之后追加：
```python
    async def memory_service(self, spec: BackendSpec) -> BaseMemoryService:
        key = spec.model_dump_json()
        if key not in self._memory_cache:
            self._memory_cache[key] = await self._build_memory(spec)
        return self._memory_cache[key]

    async def artifact_service(self, spec: BackendSpec) -> ArtifactServiceABC:
        key = spec.model_dump_json()
        if key not in self._artifact_cache:
            self._artifact_cache[key] = self._build_artifact(spec)
        return self._artifact_cache[key]

    async def _build_memory(self, spec: BackendSpec) -> BaseMemoryService:
        options = dict(spec.options)
        is_async = bool(options.pop("is_async", self._is_async))
        enabled = bool(options.pop("enabled", True))
        if spec.type == "in_memory":
            return InMemoryMemoryService(enabled=enabled, **options)
        dsn = await self._resolve_dsn(spec)
        if spec.type == "redis":
            return RedisMemoryService(db_url=dsn, is_async=is_async, enabled=enabled, **options)
        if spec.type == "sql":
            return SqlMemoryService(db_url=dsn, is_async=is_async, enabled=enabled, **options)
        raise ValueError(f"unsupported memory backend type: {spec.type}")

    def _build_artifact(self, spec: BackendSpec) -> ArtifactServiceABC:
        if spec.type == "in_memory":
            return InMemoryArtifactService()
        raise ValueError(f"unsupported artifact backend type: {spec.type}")
```

- [ ] **Step 4: 运行测试验证通过**

Run: `.venv/bin/pytest tests/tenant/test_backend_factory.py -v`
Expected: PASS（14 passed）

- [ ] **Step 5: 提交**

```bash
git add trpc_agent_sdk/tenant/_backend_factory.py tests/tenant/test_backend_factory.py
git commit -m "feat(tenant): add BackendFactory memory and artifact services"
```

---

### Task 3: StorageAdapter — 按租户解析后端

**Files:**
- Create: `trpc_agent_sdk/tenant/_storage_adapter.py`
- Create: `tests/tenant/test_storage_adapter.py`

**Interfaces:**
- Consumes: `TenantRegistry`、`BackendFactory`（Task 1/2）、`TenantNotFoundError`（SP1）、`Tenant`（SP1）。
- Produces: `StorageAdapter(registry, factory)`，方法 `async session_service(tenant_id) -> BaseSessionService`、`async memory_service(tenant_id) -> BaseMemoryService`、`async artifact_service(tenant_id) -> ArtifactServiceABC`、`async close()`；私有 `_get_tenant`。

- [ ] **Step 1: 写失败测试**

`tests/tenant/test_storage_adapter.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for StorageAdapter."""

import pytest

from trpc_agent_sdk.sessions import InMemorySessionService
from trpc_agent_sdk.sessions import SqlSessionService

from trpc_agent_sdk.tenant._backend_factory import BackendFactory
from trpc_agent_sdk.tenant._errors import TenantNotFoundError
from trpc_agent_sdk.tenant._registry import TenantRegistry
from trpc_agent_sdk.tenant._storage_adapter import StorageAdapter
from trpc_agent_sdk.tenant._tenant import BackendSpec
from trpc_agent_sdk.tenant._tenant import DataBackendConfig
from trpc_agent_sdk.tenant._tenant import ModelConfig
from trpc_agent_sdk.tenant._tenant import Tenant
from trpc_agent_sdk.tenant._tenant_source import InMemoryTenantSource


def _tenant(tenant_id="acme", status="active", **overrides):
    data = {
        "tenant_id": tenant_id,
        "model_settings": ModelConfig(provider="openai", model_name="gpt-4o"),
        "status": status,
    }
    data.update(overrides)
    return Tenant(**data)


async def test_session_service_by_tenant():
    source = InMemoryTenantSource()
    await source.put(_tenant())
    adapter = StorageAdapter(TenantRegistry(source), BackendFactory())
    svc = await adapter.session_service("acme")
    assert isinstance(svc, InMemorySessionService)


async def test_session_service_caches_per_tenant():
    source = InMemoryTenantSource()
    await source.put(_tenant())
    adapter = StorageAdapter(TenantRegistry(source), BackendFactory())
    assert (await adapter.session_service("acme")) is (await adapter.session_service("acme"))


async def test_missing_tenant_raises():
    adapter = StorageAdapter(TenantRegistry(InMemoryTenantSource()), BackendFactory())
    with pytest.raises(TenantNotFoundError):
        await adapter.session_service("nope")


async def test_disabled_tenant_raises():
    source = InMemoryTenantSource()
    await source.put(_tenant(status="disabled"))
    adapter = StorageAdapter(TenantRegistry(source), BackendFactory())
    with pytest.raises(TenantNotFoundError):
        await adapter.session_service("acme")


async def test_distinct_tenants_distinct_backends():
    source = InMemoryTenantSource()
    await source.put(_tenant("acme", data_backends=DataBackendConfig(session=BackendSpec(type="in_memory"))))
    await source.put(_tenant("beta",
                             data_backends=DataBackendConfig(session=BackendSpec(type="sql", dsn="sqlite:///:memory:"))))
    adapter = StorageAdapter(TenantRegistry(source), BackendFactory())
    a = await adapter.session_service("acme")
    b = await adapter.session_service("beta")
    assert isinstance(a, InMemorySessionService)
    assert isinstance(b, SqlSessionService)
    await adapter.close()
```

- [ ] **Step 2: 运行测试验证失败**

Run: `.venv/bin/pytest tests/tenant/test_storage_adapter.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'trpc_agent_sdk.tenant._storage_adapter'`）

- [ ] **Step 3: 写最小实现**

`trpc_agent_sdk/tenant/_storage_adapter.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Per-tenant storage adapter facade."""

from __future__ import annotations

from trpc_agent_sdk.abc import ArtifactServiceABC
from trpc_agent_sdk.memory import BaseMemoryService
from trpc_agent_sdk.sessions import BaseSessionService

from ._backend_factory import BackendFactory
from ._errors import TenantNotFoundError
from ._registry import TenantRegistry
from ._tenant import Tenant


class StorageAdapter:
    """Resolve and cache per-tenant data backends."""

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

    async def memory_service(self, tenant_id: str) -> BaseMemoryService:
        tenant = await self._get_tenant(tenant_id)
        if tenant_id not in self._memory_cache:
            self._memory_cache[tenant_id] = await self._factory.memory_service(tenant.data_backends.memory)
        return self._memory_cache[tenant_id]

    async def artifact_service(self, tenant_id: str) -> ArtifactServiceABC:
        tenant = await self._get_tenant(tenant_id)
        if tenant_id not in self._artifact_cache:
            self._artifact_cache[tenant_id] = await self._factory.artifact_service(tenant.data_backends.artifact)
        return self._artifact_cache[tenant_id]

    async def close(self) -> None:
        await self._factory.close()

    async def _get_tenant(self, tenant_id: str) -> Tenant:
        tenant = await self._registry.get(tenant_id)
        if tenant is None:
            raise TenantNotFoundError(tenant_id)
        return tenant
```

- [ ] **Step 4: 运行测试验证通过**

Run: `.venv/bin/pytest tests/tenant/test_storage_adapter.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add trpc_agent_sdk/tenant/_storage_adapter.py tests/tenant/test_storage_adapter.py
git commit -m "feat(tenant): add StorageAdapter per-tenant backend resolution"
```

---

### Task 4: StorageAdapter — migrate_session

**Files:**
- Modify: `trpc_agent_sdk/tenant/_storage_adapter.py`（追加 `migrate_session`）
- Create: `tests/tenant/test_session_migration.py`

**Interfaces:**
- Consumes: `BackendSpec`（SP1）、`Event`（`trpc_agent_sdk.events`）、`Content`/`Part`（`trpc_agent_sdk.types`）、`BackendFactory.session_service`（Task 1）。
- Produces: `async migrate_session(*, tenant_id, source: BackendSpec, target: BackendSpec) -> int`（返回本次新迁移的 session 数）。

- [ ] **Step 1: 写失败测试**

`tests/tenant/test_session_migration.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for session migration."""

from trpc_agent_sdk.events import Event
from trpc_agent_sdk.types import Content
from trpc_agent_sdk.types import Part

from trpc_agent_sdk.tenant._backend_factory import BackendFactory
from trpc_agent_sdk.tenant._registry import TenantRegistry
from trpc_agent_sdk.tenant._storage_adapter import StorageAdapter
from trpc_agent_sdk.tenant._tenant import BackendSpec
from trpc_agent_sdk.tenant._tenant import ModelConfig
from trpc_agent_sdk.tenant._tenant import Tenant
from trpc_agent_sdk.tenant._tenant_source import InMemoryTenantSource

APP_NAME = "acme:default"


def _tenant():
    return Tenant(tenant_id="acme", model_settings=ModelConfig(provider="openai", model_name="gpt-4o"))


def _make_adapter():
    source = InMemoryTenantSource()
    factory = BackendFactory()
    adapter = StorageAdapter(TenantRegistry(source), factory)
    return adapter, source, factory


def _user_event(text):
    return Event(invocation_id="inv1", author="user", content=Content(parts=[Part(text=text)]))


async def test_migrate_in_memory_to_sql():
    adapter, source, factory = _make_adapter()
    await source.put(_tenant())
    src_spec = BackendSpec(type="in_memory")
    tgt_spec = BackendSpec(type="sql", dsn="sqlite:///:memory:")

    src = await factory.session_service(src_spec)
    session = await src.create_session(app_name=APP_NAME, user_id="u1", session_id="s1")
    await src.append_event(session, _user_event("hello"))

    migrated = await adapter.migrate_session(tenant_id="acme", source=src_spec, target=tgt_spec)
    assert migrated == 1

    tgt = await factory.session_service(tgt_spec)
    got = await tgt.get_session(app_name=APP_NAME, user_id="u1", session_id="s1")
    assert got is not None
    assert len(got.events) == 1
    await factory.close()


async def test_migrate_preserves_event_order():
    adapter, source, factory = _make_adapter()
    await source.put(_tenant())
    src_spec = BackendSpec(type="in_memory")
    tgt_spec = BackendSpec(type="sql", dsn="sqlite:///:memory:")

    src = await factory.session_service(src_spec)
    session = await src.create_session(app_name=APP_NAME, user_id="u1", session_id="s1")
    await src.append_event(session, _user_event("first"))
    await src.append_event(session, _user_event("second"))

    await adapter.migrate_session(tenant_id="acme", source=src_spec, target=tgt_spec)

    tgt = await factory.session_service(tgt_spec)
    got = await tgt.get_session(app_name=APP_NAME, user_id="u1", session_id="s1")
    assert [e.get_text() for e in got.events] == ["first", "second"]
    await factory.close()


async def test_migrate_is_idempotent():
    adapter, source, factory = _make_adapter()
    await source.put(_tenant())
    src_spec = BackendSpec(type="in_memory")
    tgt_spec = BackendSpec(type="sql", dsn="sqlite:///:memory:")

    src = await factory.session_service(src_spec)
    session = await src.create_session(app_name=APP_NAME, user_id="u1", session_id="s1")
    await src.append_event(session, _user_event("hello"))

    assert await adapter.migrate_session(tenant_id="acme", source=src_spec, target=tgt_spec) == 1
    assert await adapter.migrate_session(tenant_id="acme", source=src_spec, target=tgt_spec) == 0

    tgt = await factory.session_service(tgt_spec)
    got = await tgt.get_session(app_name=APP_NAME, user_id="u1", session_id="s1")
    assert len(got.events) == 1
    await factory.close()


async def test_migrate_sql_to_in_memory():
    adapter, source, factory = _make_adapter()
    await source.put(_tenant())
    src_spec = BackendSpec(type="sql", dsn="sqlite:///:memory:")
    tgt_spec = BackendSpec(type="in_memory")

    src = await factory.session_service(src_spec)
    session = await src.create_session(app_name=APP_NAME, user_id="u1", session_id="s1")
    await src.append_event(session, _user_event("hello"))

    await adapter.migrate_session(tenant_id="acme", source=src_spec, target=tgt_spec)

    tgt = await factory.session_service(tgt_spec)
    got = await tgt.get_session(app_name=APP_NAME, user_id="u1", session_id="s1")
    assert got is not None
    assert len(got.events) == 1
    await factory.close()
```

- [ ] **Step 2: 运行测试验证失败**

Run: `.venv/bin/pytest tests/tenant/test_session_migration.py -v`
Expected: FAIL（`AttributeError: 'StorageAdapter' object has no attribute 'migrate_session'`）

- [ ] **Step 3: 写最小实现**

在 `_storage_adapter.py` 追加 import 与方法。追加 import（`Event`/`Content`/`Part` 无需导入，直接使用传入的 event 对象；只需 `BackendSpec`）：
```python
from ._tenant import BackendSpec
```
在 `close` 方法之前追加：
```python
    async def migrate_session(self, *, tenant_id: str,
                              source: BackendSpec, target: BackendSpec) -> int:
        """Copy a tenant's sessions from source backend to target backend."""
        app_name = (await self._get_tenant(tenant_id)).sdk_app_name
        src = await self._factory.session_service(source)
        dst = await self._factory.session_service(target)
        count = 0
        listed = await src.list_sessions(app_name=app_name)
        for summary in listed.sessions:
            if await dst.get_session(app_name=summary.app_name, user_id=summary.user_id,
                                     session_id=summary.id) is not None:
                continue
            full = await src.get_session(app_name=summary.app_name, user_id=summary.user_id,
                                         session_id=summary.id)
            if full is None:
                continue
            session = await dst.create_session(app_name=full.app_name, user_id=full.user_id,
                                               session_id=full.id, state=full.state)
            for event in full.events:
                await dst.append_event(session=session, event=event)
            count += 1
        return count
```

- [ ] **Step 4: 运行测试验证通过**

Run: `.venv/bin/pytest tests/tenant/test_session_migration.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add trpc_agent_sdk/tenant/_storage_adapter.py tests/tenant/test_session_migration.py
git commit -m "feat(tenant): add session migration to StorageAdapter"
```

---

### Task 5: 公开导出与全量验证

**Files:**
- Modify: `trpc_agent_sdk/tenant/__init__.py`

**Interfaces:**
- Produces：公开导出 `BackendFactory`、`StorageAdapter`。

- [ ] **Step 1: 写失败测试**

在 `tests/tenant/test_storage_adapter.py` 末尾追加：
```python
def test_public_exports_backend():
    import trpc_agent_sdk.tenant as tenant_pkg
    assert hasattr(tenant_pkg, "BackendFactory")
    assert hasattr(tenant_pkg, "StorageAdapter")
```

- [ ] **Step 2: 运行测试验证失败**

Run: `.venv/bin/pytest tests/tenant/test_storage_adapter.py::test_public_exports_backend -v`
Expected: FAIL（`AssertionError`，`BackendFactory` 未导出）

- [ ] **Step 3: 写最小实现**

在 `trpc_agent_sdk/tenant/__init__.py` 追加：
```python
from ._backend_factory import BackendFactory
from ._storage_adapter import StorageAdapter
```
并将 `"BackendFactory"`、`"StorageAdapter"` 加入 `__all__`（按字母序插入）。

- [ ] **Step 4: 运行全量测试**

Run: `.venv/bin/pytest tests/tenant/ -v`
Expected: PASS（全部通过）

- [ ] **Step 5: 运行 lint**

Run: `.venv/bin/python -m flake8 trpc_agent_sdk/tenant tests/tenant --max-line-length=120 --extend-exclude=".git,__pycache__"`
Expected: 无输出（干净）

- [ ] **Step 6: 提交**

```bash
git add trpc_agent_sdk/tenant/__init__.py tests/tenant/test_storage_adapter.py
git commit -m "feat(tenant): expose BackendFactory and StorageAdapter"
```

---

## 自检清单

1. **规格覆盖**：SP2 规格的 BackendFactory（§4）、StorageAdapter（§5）、migrate_session（§6）、测试策略（§8）均有对应 Task。
2. **无占位符**：所有 Task 含完整代码与测试、命令与预期输出。
3. **类型/命名一致**：`session_service`/`memory_service`/`artifact_service`/`migrate_session`/`_resolve_dsn`/`_build_session`/`_build_memory`/`_build_artifact` 在各 Task 间一致；`BackendFactory`/`StorageAdapter`/`TenantNotFoundError` 与 SP1 一致。
