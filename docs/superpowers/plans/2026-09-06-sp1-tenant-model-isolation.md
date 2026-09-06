# SP1 — 租户模型与隔离机制 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 tRPC-Agent-Python SDK 新增 `trpc_agent_sdk/tenant` 包，提供租户配置模型、`SecretStore`、`Masker`、`TenantRegistry` 与租户数据源（InMemory/File/Sql），实现配置/密钥/日志隔离。

**Architecture:** 纯新增包，零 SDK 核心改动。`tenant_id` 校验后映射为 SDK 层 `app_name = f"{tenant_id}:{app_config.app_name}"`。密钥只存 `secret_ref`，由 `SecretStore` 运行时解析。`Masker` 挂到 `logging` 层脱敏。

**Tech Stack:** Python 3.10+、pydantic v2、SQLAlchemy 2.0、pytest + pytest-asyncio（`asyncio_mode=auto`）、标准库 `logging`/`re`/`os`/`pathlib`。

## Global Constraints

- 包名：`tenant`（`trpc_agent_sdk/tenant/`），测试目录 `tests/tenant/`。
- `tenant_id` 正则：`^[a-zA-Z0-9_-]{1,64}$`。
- `DataBackendConfig` 所有字段默认 `BackendSpec(type="redis")`。
- `Runner`/`Agent` 构建**不在** SP1 范围（推迟 SP3）。
- 行宽 120；flake8 忽略 `E402, W503`；yapf 风格 `based_on_style = pep8`。
- 测试命令：`pytest tests/tenant/... -v`（全局 `addopts = "-ra -q"`，`asyncio_mode = "auto"`，无需 `@pytest.mark.asyncio`）。
- 提交信息前缀：`feat(tenant):`。

---

## 文件结构

```
trpc_agent_sdk/tenant/
    __init__.py          # 公开导出（Task 1 建骨架，Task 8 补齐）
    _errors.py           # TenantNotFoundError / SecretNotFoundError
    _tenant.py           # Tenant 及子模型
    _secret_store.py     # SecretStore 抽象 + Env/InMemory
    _masker.py           # Masker + SensitiveDataFilter
    _registry.py         # TenantSource 抽象 + TenantRegistry
    _tenant_source.py    # InMemory/File/Sql TenantSource

tests/tenant/
    __init__.py
    test_errors.py
    test_tenant.py
    test_secret_store.py
    test_masker.py
    test_registry.py
    test_tenant_source.py
    test_sql_tenant_source.py
```

---

### Task 1: 包骨架与异常类型

**Files:**
- Create: `trpc_agent_sdk/tenant/__init__.py`
- Create: `trpc_agent_sdk/tenant/_errors.py`
- Create: `tests/tenant/__init__.py`
- Create: `tests/tenant/test_errors.py`

**Interfaces:**
- Produces: `TenantNotFoundError(tenant_id) -> Exception`（`.tenant_id` 属性）、`SecretNotFoundError(ref) -> Exception`（`.ref` 属性）。

- [ ] **Step 1: 写失败测试**

`tests/tenant/test_errors.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for tenant error types."""

import pytest

from trpc_agent_sdk.tenant._errors import SecretNotFoundError
from trpc_agent_sdk.tenant._errors import TenantNotFoundError


def test_tenant_not_found_error():
    err = TenantNotFoundError("acme")
    assert err.tenant_id == "acme"
    assert "acme" in str(err)


def test_secret_not_found_error():
    err = SecretNotFoundError("api_key")
    assert err.ref == "api_key"
    assert "api_key" in str(err)


def test_errors_are_exceptions():
    assert issubclass(TenantNotFoundError, Exception)
    assert issubclass(SecretNotFoundError, Exception)
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/tenant/test_errors.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'trpc_agent_sdk.tenant'`）

- [ ] **Step 3: 写最小实现**

`trpc_agent_sdk/tenant/__init__.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tenant model and isolation primitives."""
```

`trpc_agent_sdk/tenant/_errors.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tenant-specific exceptions."""

from __future__ import annotations


class TenantNotFoundError(Exception):
    """Raised when a tenant is not found in the registry."""

    def __init__(self, tenant_id: str):
        super().__init__(f"tenant not found: {tenant_id}")
        self.tenant_id = tenant_id


class SecretNotFoundError(Exception):
    """Raised when a referenced secret is missing from the secret store."""

    def __init__(self, ref: str):
        super().__init__(f"secret not found: {ref}")
        self.ref = ref
```

`tests/tenant/__init__.py`（空文件）:
```python
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/tenant/test_errors.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add trpc_agent_sdk/tenant tests/tenant
git commit -m "feat(tenant): add tenant and secret exception types"
```

---

### Task 2: Tenant 模型

**Files:**
- Create: `trpc_agent_sdk/tenant/_tenant.py`
- Create: `tests/tenant/test_tenant.py`

**Interfaces:**
- Produces: `Tenant`（`tenant_id`, `app_config`, `model_config`, `tool_permissions`, `im_channels`, `data_backends`, `audit_policy`, `budgets`, `rate_limits`, `status`, `version`；属性 `sdk_app_name`）。子模型：`ModelConfig`, `AppConfig`, `ToolPermissions`, `ChannelBinding`, `BackendSpec`, `DataBackendConfig`, `AuditPolicy`, `BudgetConfig`, `RateLimitConfig`。
- 约束：`tenant_id` 匹配 `^[a-zA-Z0-9_-]{1,64}$`；`AuditPolicy.sample_rate` 在 `[0,1]`。

- [ ] **Step 1: 写失败测试**

`tests/tenant/test_tenant.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for the Tenant configuration model."""

import pytest
from pydantic import ValidationError

from trpc_agent_sdk.tenant._tenant import AuditPolicy
from trpc_agent_sdk.tenant._tenant import DataBackendConfig
from trpc_agent_sdk.tenant._tenant import ModelConfig
from trpc_agent_sdk.tenant._tenant import Tenant


def _make_tenant(**overrides):
    data = {
        "tenant_id": "acme",
        "model_config": ModelConfig(provider="openai", model_name="gpt-4o"),
    }
    data.update(overrides)
    return Tenant(**data)


def test_sdk_app_name_combines_tenant_and_app():
    tenant = _make_tenant()
    assert tenant.sdk_app_name == "acme:default"


def test_sdk_app_name_uses_custom_app_name():
    tenant = _make_tenant(app_config={"app_name": "support"})
    assert tenant.sdk_app_name == "acme:support"


def test_tenant_id_rejects_invalid_chars():
    with pytest.raises(ValidationError):
        _make_tenant(tenant_id="acme:prod")


def test_tenant_id_rejects_empty():
    with pytest.raises(ValidationError):
        _make_tenant(tenant_id="")


def test_tenant_id_rejects_too_long():
    with pytest.raises(ValidationError):
        _make_tenant(tenant_id="a" * 65)


def test_sample_rate_rejects_out_of_range():
    with pytest.raises(ValidationError):
        AuditPolicy(sample_rate=1.5)


def test_data_backends_default_to_redis():
    backends = DataBackendConfig()
    for name in ("session", "memory", "summary", "artifact", "knowledge", "audit"):
        assert getattr(backends, name).type == "redis"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/tenant/test_tenant.py -v`
Expected: FAIL（`ImportError` / `ModuleNotFoundError: No module named 'trpc_agent_sdk.tenant._tenant'`）

- [ ] **Step 3: 写最小实现**

`trpc_agent_sdk/tenant/_tenant.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tenant configuration models."""

from __future__ import annotations

import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

_TENANT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class ModelConfig(BaseModel):
    """Per-tenant model configuration."""

    provider: str
    model_name: str
    endpoint: Optional[str] = None
    api_key_ref: Optional[str] = None
    timeout_seconds: float = 60.0
    temperature: Optional[float] = None
    extra: dict[str, Any] = Field(default_factory=dict)


class AppConfig(BaseModel):
    """Per-tenant application configuration."""

    app_name: str = "default"
    display_name: str = ""
    agent_description: str = ""
    instruction: str = ""


class ToolPermissions(BaseModel):
    """Per-tenant tool permissions."""

    allow_all: bool = False
    allowlist: list[str] = Field(default_factory=list)
    denylist: list[str] = Field(default_factory=list)
    require_confirmation: list[str] = Field(default_factory=list)


class ChannelBinding(BaseModel):
    """IM channel binding (consumed by SP4)."""

    binding_id: str
    channel_type: str
    webhook_url: str
    token_ref: Optional[str] = None
    secret_ref: Optional[str] = None
    verify_enabled: bool = True
    dedup_ttl_seconds: int = 300
    external_account_id: Optional[str] = None


class BackendSpec(BaseModel):
    """A single data backend specification (consumed by SP2)."""

    type: str
    dsn: Optional[str] = None
    secret_ref: Optional[str] = None
    options: dict[str, Any] = Field(default_factory=dict)


class DataBackendConfig(BaseModel):
    """Per-tenant data backend configuration (defaults to Redis)."""

    session: BackendSpec = Field(default_factory=lambda: BackendSpec(type="redis"))
    memory: BackendSpec = Field(default_factory=lambda: BackendSpec(type="redis"))
    summary: BackendSpec = Field(default_factory=lambda: BackendSpec(type="redis"))
    artifact: BackendSpec = Field(default_factory=lambda: BackendSpec(type="redis"))
    knowledge: BackendSpec = Field(default_factory=lambda: BackendSpec(type="redis"))
    audit: BackendSpec = Field(default_factory=lambda: BackendSpec(type="redis"))


class AuditPolicy(BaseModel):
    """Per-tenant audit policy (consumed by SP5)."""

    enabled: bool = True
    sample_rate: float = 1.0
    retention_days: int = 180
    include_content: bool = False

    @field_validator("sample_rate")
    @classmethod
    def _validate_sample_rate(cls, value: float) -> float:
        if not (0.0 <= value <= 1.0):
            raise ValueError("sample_rate must be between 0 and 1")
        return value


class BudgetConfig(BaseModel):
    """Per-tenant budget limits (consumed by SP5 filter)."""

    enabled: bool = False
    monthly_token_limit: Optional[int] = None
    monthly_cost_limit_usd: Optional[float] = None
    per_request_token_limit: Optional[int] = None


class RateLimitConfig(BaseModel):
    """Per-tenant rate limits (consumed by SP3/SP4)."""

    enabled: bool = False
    requests_per_minute: Optional[int] = None
    im_messages_per_minute: Optional[int] = None


class Tenant(BaseModel):
    """Full tenant configuration."""

    tenant_id: str
    app_config: AppConfig = Field(default_factory=AppConfig)
    model_config: ModelConfig
    tool_permissions: ToolPermissions = Field(default_factory=ToolPermissions)
    im_channels: list[ChannelBinding] = Field(default_factory=list)
    data_backends: DataBackendConfig = Field(default_factory=DataBackendConfig)
    audit_policy: AuditPolicy = Field(default_factory=AuditPolicy)
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)
    rate_limits: RateLimitConfig = Field(default_factory=RateLimitConfig)
    status: Literal["active", "disabled"] = "active"
    version: int = 1

    @field_validator("tenant_id")
    @classmethod
    def _validate_tenant_id(cls, value: str) -> str:
        if not _TENANT_ID_RE.match(value):
            raise ValueError("tenant_id must match ^[a-zA-Z0-9_-]{1,64}$")
        return value

    @property
    def sdk_app_name(self) -> str:
        """The app_name namespace used at the SDK layer."""
        return f"{self.tenant_id}:{self.app_config.app_name}"
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/tenant/test_tenant.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```bash
git add trpc_agent_sdk/tenant/_tenant.py tests/tenant/test_tenant.py
git commit -m "feat(tenant): add Tenant configuration model"
```

---

### Task 3: SecretStore

**Files:**
- Create: `trpc_agent_sdk/tenant/_secret_store.py`
- Create: `tests/tenant/test_secret_store.py`

**Interfaces:**
- Produces: `SecretStore`（ABC：`async get/put/delete`）、`EnvSecretStore(prefix="TENANT_SECRET_")`、`InMemorySecretStore(secrets=None)`。
- `get` 缺失时抛 `SecretNotFoundError(ref)`（来自 Task 1）。

- [ ] **Step 1: 写失败测试**

`tests/tenant/test_secret_store.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for secret store implementations."""

import pytest

from trpc_agent_sdk.tenant._errors import SecretNotFoundError
from trpc_agent_sdk.tenant._secret_store import EnvSecretStore
from trpc_agent_sdk.tenant._secret_store import InMemorySecretStore


async def test_in_memory_get_put_delete():
    store = InMemorySecretStore()
    await store.put("api_key", "sk-secret-value")
    assert await store.get("api_key") == "sk-secret-value"
    await store.delete("api_key")
    with pytest.raises(SecretNotFoundError):
        await store.get("api_key")


async def test_in_memory_missing_raises_without_leaking_value():
    store = InMemorySecretStore()
    with pytest.raises(SecretNotFoundError) as exc_info:
        await store.get("db_password")
    assert "db_password" in str(exc_info.value)


async def test_env_secret_store_uses_prefix(monkeypatch):
    monkeypatch.setenv("TENANT_SECRET_API_KEY", "env-secret")
    store = EnvSecretStore()
    assert await store.get("API_KEY") == "env-secret"


async def test_env_secret_store_missing(monkeypatch):
    monkeypatch.delenv("TENANT_SECRET_NOPE", raising=False)
    store = EnvSecretStore()
    with pytest.raises(SecretNotFoundError):
        await store.get("NOPE")
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/tenant/test_secret_store.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'trpc_agent_sdk.tenant._secret_store'`）

- [ ] **Step 3: 写最小实现**

`trpc_agent_sdk/tenant/_secret_store.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Secret store abstraction and implementations."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod

from ._errors import SecretNotFoundError


class SecretStore(ABC):
    """Abstract secret store for IM tokens, model API keys, DB passwords, etc."""

    @abstractmethod
    async def get(self, ref: str) -> str:
        """Return the secret value for the reference; raise SecretNotFoundError if missing."""

    @abstractmethod
    async def put(self, ref: str, value: str) -> None:
        """Store a secret value under the reference."""

    @abstractmethod
    async def delete(self, ref: str) -> None:
        """Delete the secret under the reference."""


class EnvSecretStore(SecretStore):
    """Environment-variable backed secret store."""

    def __init__(self, prefix: str = "TENANT_SECRET_"):
        self._prefix = prefix

    async def get(self, ref: str) -> str:
        value = os.environ.get(self._prefix + ref)
        if value is None:
            raise SecretNotFoundError(ref)
        return value

    async def put(self, ref: str, value: str) -> None:
        os.environ[self._prefix + ref] = value

    async def delete(self, ref: str) -> None:
        os.environ.pop(self._prefix + ref, None)


class InMemorySecretStore(SecretStore):
    """In-memory secret store for development and testing."""

    def __init__(self, secrets: dict[str, str] | None = None):
        self._secrets = dict(secrets or {})

    async def get(self, ref: str) -> str:
        if ref not in self._secrets:
            raise SecretNotFoundError(ref)
        return self._secrets[ref]

    async def put(self, ref: str, value: str) -> None:
        self._secrets[ref] = value

    async def delete(self, ref: str) -> None:
        self._secrets.pop(ref, None)
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/tenant/test_secret_store.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add trpc_agent_sdk/tenant/_secret_store.py tests/tenant/test_secret_store.py
git commit -m "feat(tenant): add SecretStore abstraction and implementations"
```

---

### Task 4: Masker（日志脱敏）

**Files:**
- Create: `trpc_agent_sdk/tenant/_masker.py`
- Create: `tests/tenant/test_masker.py`

**Interfaces:**
- Produces: `Masker(extra_patterns=None)`（`mask(text) -> str`、`register(name, pattern, replacement="[REDACTED]")`）、`SensitiveDataFilter(masker)`（`logging.Filter` 子类）。

- [ ] **Step 1: 写失败测试**

`tests/tenant/test_masker.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for the Masker and SensitiveDataFilter."""

import logging

from trpc_agent_sdk.tenant._masker import Masker
from trpc_agent_sdk.tenant._masker import SensitiveDataFilter


def test_mask_api_key_assignment():
    masker = Masker()
    masked = masker.mask("api_key=sk-abc123xyz")
    assert "sk-abc123xyz" not in masked
    assert "[REDACTED]" in masked


def test_mask_phone_number():
    masker = Masker()
    masked = masker.mask("call 13812345678 now")
    assert "13812345678" not in masked


def test_register_custom_pattern():
    masker = Masker()
    masker.register("custom", r"\bCUSTOM_\d+\b", replacement="<hidden>")
    assert masker.mask("value CUSTOM_123 end") == "value <hidden> end"


def test_sensitive_data_filter_masks_formatted_message():
    masker = Masker()
    flt = SensitiveDataFilter(masker)
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="api_key=%s", args=("sk-leak-here",), exc_info=None,
    )
    assert flt.filter(record) is True
    assert "sk-leak-here" not in record.getMessage()
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/tenant/test_masker.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'trpc_agent_sdk.tenant._masker'`）

- [ ] **Step 3: 写最小实现**

`trpc_agent_sdk/tenant/_masker.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Sensitive-data masking utilities for logs, traces, and audit records."""

from __future__ import annotations

import logging
import re

DEFAULT_PATTERNS: dict[str, str] = {
    "api_key": r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+",
    "phone": r"\b1[3-9]\d{9}\b",
    "id_card": r"\b\d{17}[\dXx]\b",
    "bearer": r"(?i)bearer\s+[a-z0-9._-]+",
}

DEFAULT_REPLACEMENT = "[REDACTED]"


class Masker:
    """Regex-based sensitive-data redactor."""

    def __init__(self, extra_patterns: dict[str, str] | None = None):
        self._rules: dict[str, tuple[re.Pattern, str]] = {}
        for name, pattern in DEFAULT_PATTERNS.items():
            self._rules[name] = (re.compile(pattern), DEFAULT_REPLACEMENT)
        for name, pattern in (extra_patterns or {}).items():
            self._rules[name] = (re.compile(pattern), DEFAULT_REPLACEMENT)

    def register(self, name: str, pattern: str, replacement: str = DEFAULT_REPLACEMENT) -> None:
        """Register or override a named pattern with a custom replacement."""
        self._rules[name] = (re.compile(pattern), replacement)

    def mask(self, text: str) -> str:
        """Return the text with all registered patterns redacted."""
        result = text
        for compiled, replacement in self._rules.values():
            result = compiled.sub(replacement, result)
        return result


class SensitiveDataFilter(logging.Filter):
    """A logging.Filter that redacts sensitive data from log records."""

    def __init__(self, masker: Masker):
        super().__init__()
        self._masker = masker

    def filter(self, record: logging.LogRecord) -> bool:
        formatted = record.getMessage()
        record.msg = self._masker.mask(formatted)
        record.args = ()
        return True
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/tenant/test_masker.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add trpc_agent_sdk/tenant/_masker.py tests/tenant/test_masker.py
git commit -m "feat(tenant): add Masker and log SensitiveDataFilter"
```

---

### Task 5: TenantSource 抽象 + TenantRegistry

**Files:**
- Create: `trpc_agent_sdk/tenant/_registry.py`
- Create: `tests/tenant/test_registry.py`

**Interfaces:**
- Consumes: `Tenant`（Task 2）。
- Produces: `TenantSource`（ABC：`async get/put/delete/list`）、`TenantRegistry(source, cache_ttl_seconds=30.0, history_size=10)`，方法 `async get/put/delete/list/rollback` 与 `subscribe(cb)`。
  - `get(tenant_id)`：缓存命中即返回；`status != "active"` 返回 `None`。
  - `put(tenant) -> Tenant`：自动 `version+1`（已存在时），写入 history（上限 `history_size`），失效缓存，触发订阅回调，返回落库的 `Tenant`。
  - `rollback(tenant_id, version)`：从 history 找到快照后重新 `put`，返回新 `Tenant`；找不到返回 `None`。

- [ ] **Step 1: 写失败测试**

`tests/tenant/test_registry.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for TenantRegistry and TenantSource."""

from typing import Optional

from trpc_agent_sdk.tenant._registry import TenantRegistry
from trpc_agent_sdk.tenant._registry import TenantSource
from trpc_agent_sdk.tenant._tenant import ModelConfig
from trpc_agent_sdk.tenant._tenant import Tenant


class _FakeSource(TenantSource):
    def __init__(self):
        self._tenants = {}

    async def get(self, tenant_id: str) -> Optional[Tenant]:
        return self._tenants.get(tenant_id)

    async def put(self, tenant: Tenant) -> None:
        self._tenants[tenant.tenant_id] = tenant

    async def delete(self, tenant_id: str) -> None:
        self._tenants.pop(tenant_id, None)

    async def list(self) -> list[Tenant]:
        return list(self._tenants.values())


def _tenant(version=1, status="active"):
    return Tenant(
        tenant_id="acme",
        model_config=ModelConfig(provider="openai", model_name="gpt-4o"),
        version=version,
        status=status,
    )


async def test_put_increments_version():
    registry = TenantRegistry(_FakeSource())
    first = await registry.put(_tenant())
    second = await registry.put(_tenant(version=first.version))
    assert first.version == 1
    assert second.version == 2


async def test_get_returns_none_for_disabled_tenant():
    registry = TenantRegistry(_FakeSource())
    await registry.put(_tenant(status="disabled"))
    assert await registry.get("acme") is None


async def test_get_missing_returns_none():
    registry = TenantRegistry(_FakeSource())
    assert await registry.get("nope") is None


async def test_rollback_restores_config_with_new_version():
    registry = TenantRegistry(_FakeSource())
    await registry.put(_tenant())  # v1
    modified = _tenant(version=1)
    modified.app_config.instruction = "v2 instruction"
    await registry.put(modified)  # v2

    restored = await registry.rollback("acme", 1)
    assert restored is not None
    assert restored.version == 3
    assert restored.app_config.instruction == ""


async def test_rollback_missing_version_returns_none():
    registry = TenantRegistry(_FakeSource())
    await registry.put(_tenant())
    assert await registry.rollback("acme", 99) is None


async def test_subscribe_fires_on_put():
    registry = TenantRegistry(_FakeSource())
    seen = []

    async def on_change(tenant_id, version):
        seen.append((tenant_id, version))

    registry.subscribe(on_change)
    await registry.put(_tenant())
    assert seen == [("acme", 1)]


async def test_delete_removes_from_cache_and_source():
    source = _FakeSource()
    registry = TenantRegistry(source)
    await registry.put(_tenant())
    await registry.delete("acme")
    assert await registry.get("acme") is None
    assert await source.list() == []
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/tenant/test_registry.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'trpc_agent_sdk.tenant._registry'`）

- [ ] **Step 3: 写最小实现**

`trpc_agent_sdk/tenant/_registry.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tenant registry and source abstraction."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Awaitable, Callable, Optional

from ._tenant import Tenant


class TenantSource(ABC):
    """Abstract persistence source for tenant configurations."""

    @abstractmethod
    async def get(self, tenant_id: str) -> Optional[Tenant]: ...

    @abstractmethod
    async def put(self, tenant: Tenant) -> None: ...

    @abstractmethod
    async def delete(self, tenant_id: str) -> None: ...

    @abstractmethod
    async def list(self) -> list[Tenant]: ...


class TenantRegistry:
    """Cached tenant registry with change notification and version rollback."""

    def __init__(self, source: TenantSource, *, cache_ttl_seconds: float = 30.0, history_size: int = 10):
        self._source = source
        self._cache_ttl_seconds = cache_ttl_seconds
        self._history_size = max(1, int(history_size))
        self._cache: dict[str, tuple[Tenant, float]] = {}
        self._history: dict[str, list[Tenant]] = {}
        self._subscribers: list[Callable[[str, int], Awaitable[None]]] = []

    async def get(self, tenant_id: str) -> Optional[Tenant]:
        now = time.monotonic()
        cached = self._cache.get(tenant_id)
        if cached is not None:
            tenant, cached_at = cached
            if now - cached_at < self._cache_ttl_seconds:
                return tenant if tenant.status == "active" else None
        tenant = await self._source.get(tenant_id)
        if tenant is None:
            return None
        self._cache[tenant_id] = (tenant, now)
        return tenant if tenant.status == "active" else None

    async def put(self, tenant: Tenant) -> Tenant:
        existing = await self._source.get(tenant.tenant_id)
        new_version = (existing.version + 1) if existing else (tenant.version or 1)
        stored = tenant.model_copy(update={"version": new_version})
        await self._source.put(stored)
        history = self._history.setdefault(stored.tenant_id, [])
        history.insert(0, stored)
        del history[self._history_size:]
        self._cache.pop(stored.tenant_id, None)
        for cb in self._subscribers:
            await cb(stored.tenant_id, new_version)
        return stored

    async def delete(self, tenant_id: str) -> None:
        await self._source.delete(tenant_id)
        self._cache.pop(tenant_id, None)
        self._history.pop(tenant_id, None)

    async def list(self) -> list[Tenant]:
        return await self._source.list()

    async def rollback(self, tenant_id: str, version: int) -> Optional[Tenant]:
        for snapshot in self._history.get(tenant_id, []):
            if snapshot.version == version:
                return await self.put(snapshot)
        return None

    def subscribe(self, cb: Callable[[str, int], Awaitable[None]]) -> None:
        """Register a change callback invoked with (tenant_id, new_version) on put."""
        self._subscribers.append(cb)
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/tenant/test_registry.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```bash
git add trpc_agent_sdk/tenant/_registry.py tests/tenant/test_registry.py
git commit -m "feat(tenant): add TenantRegistry and TenantSource abstraction"
```

---

### Task 6: InMemory 与 File TenantSource

**Files:**
- Create: `trpc_agent_sdk/tenant/_tenant_source.py`
- Create: `tests/tenant/test_tenant_source.py`

**Interfaces:**
- Consumes: `TenantSource`（Task 5）、`Tenant`（Task 2）。
- Produces: `InMemoryTenantSource()`、`FileTenantSource(directory, extension=".json")`。

- [ ] **Step 1: 写失败测试**

`tests/tenant/test_tenant_source.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for InMemory and File tenant sources."""

from trpc_agent_sdk.tenant._tenant import ModelConfig
from trpc_agent_sdk.tenant._tenant import Tenant
from trpc_agent_sdk.tenant._tenant_source import FileTenantSource
from trpc_agent_sdk.tenant._tenant_source import InMemoryTenantSource


def _tenant():
    return Tenant(tenant_id="acme", model_config=ModelConfig(provider="openai", model_name="gpt-4o"))


async def test_in_memory_roundtrip():
    source = InMemoryTenantSource()
    await source.put(_tenant())
    loaded = await source.get("acme")
    assert loaded is not None
    assert loaded.tenant_id == "acme"
    assert await source.list() == [loaded]


async def test_in_memory_delete():
    source = InMemoryTenantSource()
    await source.put(_tenant())
    await source.delete("acme")
    assert await source.get("acme") is None


async def test_file_roundtrip(tmp_path):
    source = FileTenantSource(str(tmp_path))
    await source.put(_tenant())
    loaded = await source.get("acme")
    assert loaded is not None
    assert loaded.model_config.model_name == "gpt-4o"
    assert await source.list() == [loaded]


async def test_file_delete(tmp_path):
    source = FileTenantSource(str(tmp_path))
    await source.put(_tenant())
    await source.delete("acme")
    assert await source.get("acme") is None
    assert await source.list() == []


async def test_file_get_missing(tmp_path):
    source = FileTenantSource(str(tmp_path))
    assert await source.get("nope") is None
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/tenant/test_tenant_source.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'trpc_agent_sdk.tenant._tenant_source'`）

- [ ] **Step 3: 写最小实现**

`trpc_agent_sdk/tenant/_tenant_source.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""In-memory and file-backed tenant source implementations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from ._registry import TenantSource
from ._tenant import Tenant


class InMemoryTenantSource(TenantSource):
    """In-memory tenant source for development and testing."""

    def __init__(self):
        self._tenants: dict[str, Tenant] = {}

    async def get(self, tenant_id: str) -> Optional[Tenant]:
        return self._tenants.get(tenant_id)

    async def put(self, tenant: Tenant) -> None:
        self._tenants[tenant.tenant_id] = tenant

    async def delete(self, tenant_id: str) -> None:
        self._tenants.pop(tenant_id, None)

    async def list(self) -> list[Tenant]:
        return list(self._tenants.values())


class FileTenantSource(TenantSource):
    """File-backed tenant source; one JSON file per tenant."""

    def __init__(self, directory: str, extension: str = ".json"):
        self._directory = directory
        self._extension = extension

    def _path(self, tenant_id: str) -> Path:
        return Path(self._directory) / f"{tenant_id}{self._extension}"

    async def get(self, tenant_id: str) -> Optional[Tenant]:
        path = self._path(tenant_id)
        if not path.exists():
            return None
        return Tenant.model_validate(json.loads(path.read_text(encoding="utf-8")))

    async def put(self, tenant: Tenant) -> None:
        Path(self._directory).mkdir(parents=True, exist_ok=True)
        self._path(tenant.tenant_id).write_text(tenant.model_dump_json(), encoding="utf-8")

    async def delete(self, tenant_id: str) -> None:
        path = self._path(tenant_id)
        if path.exists():
            path.unlink()

    async def list(self) -> list[Tenant]:
        directory = Path(self._directory)
        if not directory.exists():
            return []
        tenants = []
        for path in directory.iterdir():
            if path.suffix == self._extension:
                tenants.append(Tenant.model_validate(json.loads(path.read_text(encoding="utf-8"))))
        return tenants
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/tenant/test_tenant_source.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add trpc_agent_sdk/tenant/_tenant_source.py tests/tenant/test_tenant_source.py
git commit -m "feat(tenant): add InMemory and File tenant sources"
```

---

### Task 7: SqlTenantSource

**Files:**
- Modify: `trpc_agent_sdk/tenant/_tenant_source.py`（追加 SQL 相关类）
- Create: `tests/tenant/test_sql_tenant_source.py`

**Interfaces:**
- Consumes: `TenantSource`（Task 5）、`Tenant`（Task 2）、`trpc_agent_sdk.storage` 的 `SqlStorage/SqlKey/SqlCondition/DynamicJSON/UTF8MB4String/PreciseTimestamp/DEFAULT_MAX_KEY_LENGTH/DEFAULT_MAX_VARCHAR_LENGTH`。
- Produces: `SqlTenantSource(db_url, is_async=False, **kwargs)`（含 `close()`）、`StorageTenant`、`StorageTenantVersion`。

- [ ] **Step 1: 写失败测试**

`tests/tenant/test_sql_tenant_source.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for the SQL tenant source using in-memory SQLite."""

import pytest

from trpc_agent_sdk.tenant._tenant import ModelConfig
from trpc_agent_sdk.tenant._tenant import Tenant
from trpc_agent_sdk.tenant._tenant_source import SqlTenantSource


def _tenant():
    return Tenant(tenant_id="acme", model_config=ModelConfig(provider="openai", model_name="gpt-4o"))


async def test_sql_roundtrip():
    source = SqlTenantSource(db_url="sqlite:///:memory:", is_async=False)
    await source.put(_tenant())
    loaded = await source.get("acme")
    assert loaded is not None
    assert loaded.model_config.model_name == "gpt-4o"
    await source.close()


async def test_sql_update_overwrites_config():
    source = SqlTenantSource(db_url="sqlite:///:memory:", is_async=False)
    await source.put(_tenant())
    updated = _tenant()
    updated.model_config.model_name = "gpt-4o-mini"
    await source.put(updated)
    loaded = await source.get("acme")
    assert loaded.model_config.model_name == "gpt-4o-mini"
    await source.close()


async def test_sql_list():
    source = SqlTenantSource(db_url="sqlite:///:memory:", is_async=False)
    await source.put(Tenant(tenant_id="acme", model_config=ModelConfig(provider="openai", model_name="gpt-4o")))
    await source.put(Tenant(tenant_id="beta", model_config=ModelConfig(provider="openai", model_name="gpt-4o")))
    tenants = await source.list()
    assert {t.tenant_id for t in tenants} == {"acme", "beta"}
    await source.close()


async def test_sql_delete():
    source = SqlTenantSource(db_url="sqlite:///:memory:", is_async=False)
    await source.put(_tenant())
    await source.delete("acme")
    assert await source.get("acme") is None
    await source.close()
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/tenant/test_sql_tenant_source.py -v`
Expected: FAIL（`ImportError: cannot import name 'SqlTenantSource'`）

- [ ] **Step 3: 写最小实现**

在 `_tenant_source.py` 顶部追加 imports，并在文件末尾追加类。新增 imports：

```python
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Integer, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from trpc_agent_sdk.storage import DEFAULT_MAX_KEY_LENGTH
from trpc_agent_sdk.storage import DEFAULT_MAX_VARCHAR_LENGTH
from trpc_agent_sdk.storage import DynamicJSON
from trpc_agent_sdk.storage import PreciseTimestamp
from trpc_agent_sdk.storage import SqlCondition
from trpc_agent_sdk.storage import SqlKey
from trpc_agent_sdk.storage import SqlStorage
from trpc_agent_sdk.storage import UTF8MB4String
```

文件末尾追加：

```python
class TenantStorageBase(DeclarativeBase):
    """Base for SqlTenantSource tables only."""


class StorageTenant(TenantStorageBase):
    """A tenant row stored in SQL."""

    __tablename__ = "tenants"

    tenant_id: Mapped[str] = mapped_column(UTF8MB4String(DEFAULT_MAX_KEY_LENGTH), primary_key=True)
    config: Mapped[dict[str, Any]] = mapped_column(DynamicJSON, default=dict)
    status: Mapped[str] = mapped_column(UTF8MB4String(DEFAULT_MAX_VARCHAR_LENGTH))
    version: Mapped[int] = mapped_column(Integer)
    update_time: Mapped[datetime] = mapped_column(PreciseTimestamp, default=func.now(), onupdate=func.now())

    def to_tenant(self) -> Tenant:
        return Tenant.model_validate(self.config)

    @classmethod
    def from_tenant(cls, tenant: Tenant) -> "StorageTenant":
        return cls(
            tenant_id=tenant.tenant_id,
            config=tenant.model_dump(mode="json"),
            status=tenant.status,
            version=tenant.version,
        )


class StorageTenantVersion(TenantStorageBase):
    """An immutable snapshot of a tenant config version, for rollback."""

    __tablename__ = "tenant_versions"

    tenant_id: Mapped[str] = mapped_column(UTF8MB4String(DEFAULT_MAX_KEY_LENGTH), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    config: Mapped[dict[str, Any]] = mapped_column(DynamicJSON, default=dict)


class SqlTenantSource(TenantSource):
    """SQL-backed tenant source."""

    def __init__(self, db_url: str, is_async: bool = False, **kwargs: Any):
        self._sql_storage = SqlStorage(is_async=is_async, db_url=db_url, metadata=TenantStorageBase.metadata, **kwargs)

    async def get(self, tenant_id: str) -> Optional[Tenant]:
        async with self._sql_storage.create_db_session() as sql_session:
            key = SqlKey(key=(tenant_id,), storage_cls=StorageTenant)
            row: Optional[StorageTenant] = await self._sql_storage.get(sql_session, key)
            if row is None:
                return None
            return row.to_tenant()

    async def put(self, tenant: Tenant) -> None:
        async with self._sql_storage.create_db_session() as sql_session:
            key = SqlKey(key=(tenant.tenant_id,), storage_cls=StorageTenant)
            existing: Optional[StorageTenant] = await self._sql_storage.get(sql_session, key)
            if existing is None:
                await self._sql_storage.add(sql_session, StorageTenant.from_tenant(tenant))
            else:
                existing.config = tenant.model_dump(mode="json")
                existing.status = tenant.status
                existing.version = tenant.version
            version_row = StorageTenantVersion(
                tenant_id=tenant.tenant_id,
                version=tenant.version,
                config=tenant.model_dump(mode="json"),
            )
            await self._sql_storage.add(sql_session, version_row)
            await self._sql_storage.commit(sql_session)

    async def delete(self, tenant_id: str) -> None:
        async with self._sql_storage.create_db_session() as sql_session:
            conditions = SqlCondition(filters=[StorageTenant.tenant_id == tenant_id])
            await self._sql_storage.delete(sql_session, SqlKey(key=(tenant_id,), storage_cls=StorageTenant), conditions)
            version_conditions = SqlCondition(filters=[StorageTenantVersion.tenant_id == tenant_id])
            await self._sql_storage.delete(sql_session,
                                           SqlKey(key=(tenant_id,), storage_cls=StorageTenantVersion),
                                           version_conditions)
            await self._sql_storage.commit(sql_session)

    async def list(self) -> list[Tenant]:
        async with self._sql_storage.create_db_session() as sql_session:
            key = SqlKey(key=tuple(), storage_cls=StorageTenant)
            rows = await self._sql_storage.query(sql_session, key, SqlCondition())
            return [row.to_tenant() for row in rows]

    async def close(self) -> None:
        await self._sql_storage.close()
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/tenant/test_sql_tenant_source.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add trpc_agent_sdk/tenant/_tenant_source.py tests/tenant/test_sql_tenant_source.py
git commit -m "feat(tenant): add SqlTenantSource with version snapshots"
```

---

### Task 8: 公开导出与全量验证

**Files:**
- Modify: `trpc_agent_sdk/tenant/__init__.py`

**Interfaces:**
- Produces（公开 API）：`Tenant`, `ModelConfig`, `AppConfig`, `ToolPermissions`, `ChannelBinding`, `BackendSpec`, `DataBackendConfig`, `AuditPolicy`, `BudgetConfig`, `RateLimitConfig`, `SecretStore`, `EnvSecretStore`, `InMemorySecretStore`, `Masker`, `SensitiveDataFilter`, `TenantSource`, `TenantRegistry`, `InMemoryTenantSource`, `FileTenantSource`, `SqlTenantSource`, `StorageTenant`, `StorageTenantVersion`, `TenantNotFoundError`, `SecretNotFoundError`。

- [ ] **Step 1: 写失败测试**

在 `tests/tenant/test_tenant.py` 末尾追加一条公开导出测试：
```python
def test_public_exports():
    import trpc_agent_sdk.tenant as tenant_pkg
    for name in (
        "Tenant", "ModelConfig", "AppConfig", "ToolPermissions", "ChannelBinding",
        "BackendSpec", "DataBackendConfig", "AuditPolicy", "BudgetConfig", "RateLimitConfig",
        "SecretStore", "EnvSecretStore", "InMemorySecretStore", "Masker", "SensitiveDataFilter",
        "TenantSource", "TenantRegistry", "InMemoryTenantSource", "FileTenantSource",
        "SqlTenantSource", "TenantNotFoundError", "SecretNotFoundError",
    ):
        assert hasattr(tenant_pkg, name), f"missing export: {name}"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/tenant/test_tenant.py::test_public_exports -v`
Expected: FAIL（`AssertionError: missing export: Tenant`）

- [ ] **Step 3: 写最小实现**

`trpc_agent_sdk/tenant/__init__.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tenant model and isolation primitives."""

from ._errors import SecretNotFoundError
from ._errors import TenantNotFoundError
from ._masker import Masker
from ._masker import SensitiveDataFilter
from ._registry import TenantRegistry
from ._registry import TenantSource
from ._secret_store import EnvSecretStore
from ._secret_store import InMemorySecretStore
from ._secret_store import SecretStore
from ._tenant import AppConfig
from ._tenant import AuditPolicy
from ._tenant import BackendSpec
from ._tenant import BudgetConfig
from ._tenant import ChannelBinding
from ._tenant import DataBackendConfig
from ._tenant import ModelConfig
from ._tenant import RateLimitConfig
from ._tenant import Tenant
from ._tenant import ToolPermissions
from ._tenant_source import FileTenantSource
from ._tenant_source import InMemoryTenantSource
from ._tenant_source import SqlTenantSource
from ._tenant_source import StorageTenant
from ._tenant_source import StorageTenantVersion

__all__ = [
    "AppConfig",
    "AuditPolicy",
    "BackendSpec",
    "BudgetConfig",
    "ChannelBinding",
    "DataBackendConfig",
    "EnvSecretStore",
    "FileTenantSource",
    "InMemorySecretStore",
    "InMemoryTenantSource",
    "Masker",
    "ModelConfig",
    "RateLimitConfig",
    "SecretNotFoundError",
    "SecretStore",
    "SensitiveDataFilter",
    "SqlTenantSource",
    "StorageTenant",
    "StorageTenantVersion",
    "Tenant",
    "TenantNotFoundError",
    "TenantRegistry",
    "TenantSource",
    "ToolPermissions",
]
```

- [ ] **Step 4: 运行全量测试**

Run: `pytest tests/tenant/ -v`
Expected: PASS（全部通过）

- [ ] **Step 5: 运行 lint**

Run: `bash lint_flake8.sh trpc_agent_sdk/tenant`
Expected: 无输出（无错误）

- [ ] **Step 6: 提交**

```bash
git add trpc_agent_sdk/tenant/__init__.py tests/tenant/test_tenant.py
git commit -m "feat(tenant): expose public tenant API"
```

---

## 自检清单

1. **规格覆盖**：SP1 规格的模型（§4）、SecretStore（§5）、Masker（§6）、TenantRegistry/TenantSource（§7）、表结构（§8）、测试策略（§9）均有对应 Task。
2. **无占位符**：所有 Task 含完整代码与测试代码、命令与预期输出。
3. **类型/命名一致**：`tenant_id`、`sdk_app_name`、`SecretNotFoundError`、`TenantNotFoundError`、`StorageTenant`、`StorageTenantVersion` 在各 Task 间保持一致；`DataBackendConfig` 默认 redis 与规格一致。
