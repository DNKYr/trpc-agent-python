# SP3 — 节点拓扑（进程内）与路由 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `trpc_agent_sdk/tenant` 包内新增 `TenantAgentFactory`（由 `Tenant` 构建 `Runner`/`LlmAgent`，含模型/工具/指令组装）与 `RunnerPool`（`tenant_id` → `Runner` 路由 + 懒加载 + 配置变更失效）。

**Architecture:** 纯新增，复用 SP1（`Tenant`/`ModelConfig`/`ToolPermissions`/`TenantRegistry`/`SecretStore`/`TenantNotFoundError`）与 SP2（`StorageAdapter`/`BackendFactory`）。模型经 `ModelRegistry.create_model` 构建；agent `name` 用 `_agent_name(tenant_id)` 规范化；`Runner` 以 `close_*_on_close=False` 保护共享连接池。

**Tech Stack:** Python 3.10+、pydantic v2、pytest + pytest-asyncio（`asyncio_mode=auto`）、`unittest.mock`。

## Global Constraints

- 包位置：`trpc_agent_sdk/tenant/`（新增 `_agent_factory.py`、`_runner_pool.py`）。
- agent `name` = `"agent_" + tenant_id.replace("-", "_")`（合法 Python 标识符，不用含 `:` 的 `sdk_app_name`）。
- `Runner.app_name` = `tenant.sdk_app_name`；`close_session_service_on_close=False`、`close_memory_service_on_close=False`。
- `RunnerPool` 按 `(tenant_id, tenant.version)` 缓存；version 变化则重建。
- 测试命令：`.venv/bin/pytest tests/tenant/... -v`（**必须用 `.venv/bin/pytest`**）。
- 模型 `gpt-4o` 已注册（`OpenAIModel`），构造不发起网络请求，测试可直接使用真实 `OpenAIModel`。
- 行宽 120；flake8 忽略 `E402, W503`；不要 `...` 一行体（E704）。
- 提交信息前缀：`feat(tenant):`。

---

## 文件结构

```
trpc_agent_sdk/tenant/
    _agent_factory.py    # TenantAgentFactory + _agent_name
    _runner_pool.py      # RunnerPool

tests/tenant/
    test_agent_factory.py
    test_runner_pool.py
```

---

### Task 1: TenantAgentFactory

**Files:**
- Create: `trpc_agent_sdk/tenant/_agent_factory.py`
- Create: `tests/tenant/test_agent_factory.py`

**Interfaces:**
- Consumes: `TenantRegistry`/`SecretStore`/`Tenant`/`ModelConfig`/`ToolPermissions`/`TenantNotFoundError`（SP1）；`StorageAdapter`（SP2）；`LlmAgent`/`Runner`/`ModelRegistry`/`BaseTool`（SDK）。
- Produces: `TenantAgentFactory(registry, storage, secret_store, tool_registry=None)`，方法 `async build_runner(tenant_id) -> Runner`、`async _get_tenant`、`async _build_model`、`_filter_tools`；模块函数 `_agent_name(tenant_id) -> str`。

- [ ] **Step 1: 写失败测试**

`tests/tenant/test_agent_factory.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for TenantAgentFactory."""

from unittest.mock import patch

import pytest

from trpc_agent_sdk.models import OpenAIModel

from trpc_agent_sdk.tenant._agent_factory import TenantAgentFactory
from trpc_agent_sdk.tenant._agent_factory import _agent_name
from trpc_agent_sdk.tenant._backend_factory import BackendFactory
from trpc_agent_sdk.tenant._errors import TenantNotFoundError
from trpc_agent_sdk.tenant._registry import TenantRegistry
from trpc_agent_sdk.tenant._secret_store import InMemorySecretStore
from trpc_agent_sdk.tenant._storage_adapter import StorageAdapter
from trpc_agent_sdk.tenant._tenant import ModelConfig
from trpc_agent_sdk.tenant._tenant import Tenant
from trpc_agent_sdk.tenant._tenant import ToolPermissions
from trpc_agent_sdk.tenant._tenant_source import InMemoryTenantSource


def _make_factory(source=None, tool_registry=None):
    source = source if source is not None else InMemoryTenantSource()
    registry = TenantRegistry(source)
    storage = StorageAdapter(registry, BackendFactory())
    factory = TenantAgentFactory(registry, storage, InMemorySecretStore(), tool_registry or {})
    return factory, source, storage


def test_agent_name_is_identifier():
    assert _agent_name("acme") == "agent_acme"
    assert _agent_name("acme-co") == "agent_acme_co"
    assert _agent_name("acme-co").isidentifier()


async def test_build_model_injects_api_key_and_base_url():
    store = InMemorySecretStore({"key": "sk-123"})
    source = InMemoryTenantSource()
    registry = TenantRegistry(source)
    storage = StorageAdapter(registry, BackendFactory())
    factory = TenantAgentFactory(registry, storage, store, {})
    config = ModelConfig(provider="openai", model_name="gpt-4o",
                         api_key_ref="key", endpoint="https://api.example.com")
    with patch("trpc_agent_sdk.tenant._agent_factory.ModelRegistry.create_model") as mock_create:
        mock_create.return_value = object()
        await factory._build_model(config)
        assert mock_create.call_args.args[0] == "gpt-4o"
        assert mock_create.call_args.kwargs["api_key"] == "sk-123"
        assert mock_create.call_args.kwargs["base_url"] == "https://api.example.com"


def test_filter_tools_allow_all():
    tools = {"a": "tool-a", "b": "tool-b"}
    factory, _, _ = _make_factory(tool_registry=tools)
    assert set(factory._filter_tools(ToolPermissions(allow_all=True))) == {"tool-a", "tool-b"}


def test_filter_tools_allowlist_minus_denylist():
    tools = {"a": "tool-a", "b": "tool-b", "c": "tool-c"}
    factory, _, _ = _make_factory(tool_registry=tools)
    result = factory._filter_tools(ToolPermissions(allowlist=["a", "b"], denylist=["b"]))
    assert result == ["tool-a"]


async def test_build_runner_end_to_end():
    source = InMemoryTenantSource()
    await source.put(Tenant(tenant_id="acme", model_settings=ModelConfig(provider="openai", model_name="gpt-4o")))
    factory, _, storage = _make_factory(source=source)
    runner = await factory.build_runner("acme")
    assert runner.app_name == "acme:default"
    assert runner.agent.name == "agent_acme"
    assert isinstance(runner.agent.model, OpenAIModel)
    assert runner._close_session_service_on_close is False
    assert runner._close_memory_service_on_close is False
    await storage.close()


async def test_build_runner_missing_tenant_raises():
    factory, _, _ = _make_factory()
    with pytest.raises(TenantNotFoundError):
        await factory.build_runner("nope")
```

- [ ] **Step 2: 运行测试验证失败**

Run: `.venv/bin/pytest tests/tenant/test_agent_factory.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'trpc_agent_sdk.tenant._agent_factory'`）

- [ ] **Step 3: 写最小实现**

`trpc_agent_sdk/tenant/_agent_factory.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Build Runner/Agent instances from a Tenant configuration."""

from __future__ import annotations

from typing import Optional

from trpc_agent_sdk.agents import LlmAgent
from trpc_agent_sdk.models import LLMModel
from trpc_agent_sdk.models import ModelRegistry
from trpc_agent_sdk.runners import Runner
from trpc_agent_sdk.tools import BaseTool

from ._errors import TenantNotFoundError
from ._registry import TenantRegistry
from ._secret_store import SecretStore
from ._storage_adapter import StorageAdapter
from ._tenant import ModelConfig
from ._tenant import Tenant
from ._tenant import ToolPermissions


def _agent_name(tenant_id: str) -> str:
    """Derive a valid Python identifier for the agent name."""
    return "agent_" + tenant_id.replace("-", "_")


class TenantAgentFactory:
    """Build Runner/Agent from a Tenant configuration."""

    def __init__(self, registry: TenantRegistry, storage: StorageAdapter,
                 secret_store: SecretStore, tool_registry: Optional[dict[str, BaseTool]] = None):
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
```

- [ ] **Step 4: 运行测试验证通过**

Run: `.venv/bin/pytest tests/tenant/test_agent_factory.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
git add trpc_agent_sdk/tenant/_agent_factory.py tests/tenant/test_agent_factory.py
git commit -m "feat(tenant): add TenantAgentFactory"
```

---

### Task 2: RunnerPool

**Files:**
- Create: `trpc_agent_sdk/tenant/_runner_pool.py`
- Create: `tests/tenant/test_runner_pool.py`

**Interfaces:**
- Consumes: `TenantRegistry`/`Tenant`/`TenantNotFoundError`（SP1）、`TenantAgentFactory`（Task 1）、`Runner`（SDK）。
- Produces: `RunnerPool(registry, factory)`，方法 `async get_runner(tenant_id) -> Runner`、`async close()`。

- [ ] **Step 1: 写失败测试**

`tests/tenant/test_runner_pool.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for RunnerPool."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from trpc_agent_sdk.tenant._errors import TenantNotFoundError
from trpc_agent_sdk.tenant._registry import TenantRegistry
from trpc_agent_sdk.tenant._runner_pool import RunnerPool
from trpc_agent_sdk.tenant._tenant import ModelConfig
from trpc_agent_sdk.tenant._tenant import Tenant
from trpc_agent_sdk.tenant._tenant_source import InMemoryTenantSource


def _tenant(version=1):
    return Tenant(tenant_id="acme",
                  model_settings=ModelConfig(provider="openai", model_name="gpt-4o"),
                  version=version)


def _make_pool(source=None):
    source = source if source is not None else InMemoryTenantSource()
    registry = TenantRegistry(source)
    factory = MagicMock()
    factory.build_runner = AsyncMock(return_value=MagicMock())
    pool = RunnerPool(registry, factory)
    return pool, source, registry, factory


async def test_get_runner_lazy_and_cached():
    pool, source, _, factory = _make_pool()
    await source.put(_tenant())
    r1 = await pool.get_runner("acme")
    r2 = await pool.get_runner("acme")
    assert r1 is r2
    assert factory.build_runner.await_count == 1


async def test_get_runner_rebuilds_on_version_change():
    pool, source, registry, factory = _make_pool()
    await source.put(_tenant(version=1))
    r1 = await pool.get_runner("acme")
    await registry.put(_tenant(version=1))  # registry bumps to version 2
    r2 = await pool.get_runner("acme")
    assert r1 is not r2


async def test_get_runner_missing_tenant_raises():
    pool, _, _, _ = _make_pool()
    with pytest.raises(TenantNotFoundError):
        await pool.get_runner("nope")


async def test_close_clears_cache():
    pool, source, _, _ = _make_pool()
    await source.put(_tenant())
    await pool.get_runner("acme")
    await pool.close()
    assert pool._runners == {}
    assert pool._versions == {}
```

- [ ] **Step 2: 运行测试验证失败**

Run: `.venv/bin/pytest tests/tenant/test_runner_pool.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'trpc_agent_sdk.tenant._runner_pool'`）

- [ ] **Step 3: 写最小实现**

`trpc_agent_sdk/tenant/_runner_pool.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Route tenant_id to Runner with lazy construction and config-change invalidation."""

from __future__ import annotations

from trpc_agent_sdk.runners import Runner

from ._agent_factory import TenantAgentFactory
from ._errors import TenantNotFoundError
from ._registry import TenantRegistry


class RunnerPool:
    """Route tenant_id -> Runner, rebuilding on tenant version change."""

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

- [ ] **Step 4: 运行测试验证通过**

Run: `.venv/bin/pytest tests/tenant/test_runner_pool.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add trpc_agent_sdk/tenant/_runner_pool.py tests/tenant/test_runner_pool.py
git commit -m "feat(tenant): add RunnerPool for tenant-to-runner routing"
```

---

### Task 3: 公开导出与全量验证

**Files:**
- Modify: `trpc_agent_sdk/tenant/__init__.py`

**Interfaces:**
- Produces：公开导出 `TenantAgentFactory`、`RunnerPool`。

- [ ] **Step 1: 写失败测试**

在 `tests/tenant/test_runner_pool.py` 末尾追加：
```python
def test_public_exports_runner():
    import trpc_agent_sdk.tenant as tenant_pkg
    assert hasattr(tenant_pkg, "TenantAgentFactory")
    assert hasattr(tenant_pkg, "RunnerPool")
```

- [ ] **Step 2: 运行测试验证失败**

Run: `.venv/bin/pytest tests/tenant/test_runner_pool.py::test_public_exports_runner -v`
Expected: FAIL（`AssertionError`）

- [ ] **Step 3: 写最小实现**

在 `trpc_agent_sdk/tenant/__init__.py` 追加：
```python
from ._agent_factory import TenantAgentFactory
from ._runner_pool import RunnerPool
```
并将 `"RunnerPool"`、`"TenantAgentFactory"` 加入 `__all__`（按字母序插入）。

- [ ] **Step 4: 运行全量测试**

Run: `.venv/bin/pytest tests/tenant/ -v`
Expected: PASS（全部通过）

- [ ] **Step 5: 运行 lint**

Run: `.venv/bin/python -m flake8 trpc_agent_sdk/tenant tests/tenant --max-line-length=120 --extend-exclude=".git,__pycache__"`
Expected: 无输出（干净）

- [ ] **Step 6: 提交**

```bash
git add trpc_agent_sdk/tenant/__init__.py tests/tenant/test_runner_pool.py
git commit -m "feat(tenant): expose TenantAgentFactory and RunnerPool"
```

---

## 自检清单

1. **规格覆盖**：SP3 规格的 TenantAgentFactory（§4）、RunnerPool（§5）、测试策略（§6）均有对应 Task。
2. **无占位符**：所有 Task 含完整代码与测试、命令与预期输出。
3. **类型/命名一致**：`_agent_name`、`build_runner`、`_build_model`、`_filter_tools`、`get_runner`、`TenantNotFoundError` 与 SP1/SP2 一致；`close_session_service_on_close`/`close_memory_service_on_close` 私有属性 `_close_*` 用于测试断言。
