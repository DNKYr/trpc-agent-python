# SP5 — 治理与审计 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `trpc_agent_sdk/tenant` 包内新增 `_audit.py`（`AuditEvent`/`AuditSink`/`InMemoryAuditSink`/`AuditLogger`）与 `_governance.py`（`ToolPermissionFilter`/`BudgetFilter`/`TenantFilterFactory`），实现租户级审计日志与治理 Filter 链。

**Architecture:** 纯新增，复用 SP1（`Tenant`/`ToolPermissions`/`BudgetConfig`/`Masker`）与 SDK（`BaseFilter`/`FilterResult`/`FilterType`/`get_tool_var`）。治理 Filter 只拦截（`rsp.is_continue=False`），不注入 `AuditLogger`（解耦）。

**Tech Stack:** Python 3.10+、pydantic v2、pytest + pytest-asyncio（`asyncio_mode=auto`）。

## Global Constraints

- 包位置：`trpc_agent_sdk/tenant/`（`_audit.py` + `_governance.py`），测试 `tests/tenant/`。
- `ToolPermissionFilter` 是 `FilterType.TOOL` 的 `BaseFilter`，用 `get_tool_var().name` 判断工具名，命中 denylist 或不在 allowlist（非 allow_all）时 `rsp.is_continue=False`。
- `BudgetFilter` 是 `FilterType.AGENT` 的 `BaseFilter`，`per_request_token_limit` 超限时 `rsp.is_continue=False`；token 计数经 `add_tokens()` 由外层更新（monthly/cost 限制后续）。
- `AuditLogger(sink, masker=None)` 写入前对 `AuditEvent` 的字符串字段脱敏。
- 测试命令：`.venv/bin/pytest tests/tenant/... -v`（**必须用 `.venv/bin/pytest`**）。
- 行宽 120；flake8 忽略 `E402, W503`；抽象方法用 docstring 体（E704）。
- 提交信息前缀：`feat(tenant):`。

---

## 文件结构

```
trpc_agent_sdk/tenant/
    _audit.py         # AuditEvent / AuditSink / InMemoryAuditSink / AuditLogger
    _governance.py    # ToolPermissionFilter / BudgetFilter / TenantFilterFactory

tests/tenant/
    test_audit.py
    test_governance.py
```

---

### Task 1: 审计日志

**Files:**
- Create: `trpc_agent_sdk/tenant/_audit.py`
- Create: `tests/tenant/test_audit.py`

**Interfaces:**
- Produces: `AuditEvent`（12 字段）、`AuditSink`（ABC：`async write(event)`/`async close()`）、`InMemoryAuditSink(max_entries=10000)`（`write`/`query(tenant_id=None)`/`close`）、`AuditLogger(sink, masker=None)`（`async log(event)`）。

- [ ] **Step 1: 写失败测试**

`tests/tenant/test_audit.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for the audit log."""

from trpc_agent_sdk.tenant._audit import AuditEvent
from trpc_agent_sdk.tenant._audit import AuditLogger
from trpc_agent_sdk.tenant._audit import InMemoryAuditSink
from trpc_agent_sdk.tenant._masker import Masker


async def test_in_memory_sink_write_and_query():
    sink = InMemoryAuditSink()
    await sink.write(AuditEvent(tenant_id="acme", decision="allow"))
    await sink.write(AuditEvent(tenant_id="beta", decision="deny"))
    assert len(await sink.query()) == 2
    assert len(await sink.query(tenant_id="acme")) == 1
    assert (await sink.query(tenant_id="acme"))[0].tenant_id == "acme"


async def test_in_memory_sink_evicts_oldest():
    sink = InMemoryAuditSink(max_entries=2)
    await sink.write(AuditEvent(tenant_id="a"))
    await sink.write(AuditEvent(tenant_id="b"))
    await sink.write(AuditEvent(tenant_id="c"))
    events = await sink.query()
    assert [e.tenant_id for e in events] == ["b", "c"]


async def test_audit_logger_masks_string_fields():
    masker = Masker(extra_patterns={"x": r"secret-\d+"})
    sink = InMemoryAuditSink()
    logger = AuditLogger(sink, masker=masker)
    await logger.log(AuditEvent(tenant_id="acme", agent_name="secret-42"))
    written = (await sink.query())[0]
    assert written.agent_name == "[REDACTED]"


async def test_audit_logger_without_masker():
    sink = InMemoryAuditSink()
    logger = AuditLogger(sink)
    await logger.log(AuditEvent(tenant_id="acme", user_id="u1"))
    assert (await sink.query())[0].user_id == "u1"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `.venv/bin/pytest tests/tenant/test_audit.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'trpc_agent_sdk.tenant._audit'`）

- [ ] **Step 3: 写最小实现**

`trpc_agent_sdk/tenant/_audit.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tenant audit log."""

from __future__ import annotations

import time
from abc import ABC
from abc import abstractmethod
from typing import Optional

from pydantic import BaseModel
from pydantic import Field

from ._masker import Masker


class AuditEvent(BaseModel):
    """A single audit record."""

    tenant_id: str
    channel: str = ""
    user_id: str = ""
    session_id: str = ""
    agent_name: str = ""
    tool_name: str = ""
    decision: str = ""
    latency: float = 0.0
    error_type: str = ""
    cost: float = 0.0
    trace_id: str = ""
    timestamp: float = Field(default_factory=time.time)


class AuditSink(ABC):
    """Abstract audit log sink."""

    @abstractmethod
    async def write(self, event: AuditEvent) -> None:
        """Write one audit event."""

    @abstractmethod
    async def close(self) -> None:
        """Release resources."""


class InMemoryAuditSink(AuditSink):
    """In-memory audit sink for development and testing."""

    def __init__(self, max_entries: int = 10000):
        self._max_entries = max(1, int(max_entries))
        self._events: list[AuditEvent] = []

    async def write(self, event: AuditEvent) -> None:
        self._events.append(event)
        if len(self._events) > self._max_entries:
            self._events = self._events[-self._max_entries:]

    async def query(self, tenant_id: Optional[str] = None) -> list[AuditEvent]:
        if tenant_id is None:
            return list(self._events)
        return [e for e in self._events if e.tenant_id == tenant_id]

    async def close(self) -> None:
        self._events.clear()


class AuditLogger:
    """Mask string fields and write audit events to a sink."""

    def __init__(self, sink: AuditSink, masker: Optional[Masker] = None):
        self._sink = sink
        self._masker = masker

    async def log(self, event: AuditEvent) -> None:
        if self._masker is not None:
            event = event.model_copy(update={
                field: self._masker.mask(str(value))
                for field, value in event.model_dump().items()
                if isinstance(value, str) and value
            })
        await self._sink.write(event)
```

- [ ] **Step 4: 运行测试验证通过**

Run: `.venv/bin/pytest tests/tenant/test_audit.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add trpc_agent_sdk/tenant/_audit.py tests/tenant/test_audit.py
git commit -m "feat(tenant): add audit log"
```

---

### Task 2: 治理 Filter

**Files:**
- Create: `trpc_agent_sdk/tenant/_governance.py`
- Create: `tests/tenant/test_governance.py`

**Interfaces:**
- Consumes: `ToolPermissions`/`BudgetConfig`/`Tenant`（SP1）、`BaseFilter`/`FilterType`/`FilterResult`（SDK）、`get_tool_var`（SDK）。
- Produces: `ToolPermissionFilter(permissions)`、`BudgetFilter(budgets)`、`TenantFilterFactory`（`build_filters(tenant) -> list[BaseFilter]`）。

- [ ] **Step 1: 写失败测试**

`tests/tenant/test_governance.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for governance filters."""

from unittest.mock import MagicMock

import pytest

from trpc_agent_sdk.abc import FilterResult
from trpc_agent_sdk.abc import FilterType

from trpc_agent_sdk.tenant._governance import BudgetFilter
from trpc_agent_sdk.tenant._governance import TenantFilterFactory
from trpc_agent_sdk.tenant._governance import ToolPermissionFilter
from trpc_agent_sdk.tenant._tenant import BudgetConfig
from trpc_agent_sdk.tenant._tenant import ModelConfig
from trpc_agent_sdk.tenant._tenant import Tenant
from trpc_agent_sdk.tenant._tenant import ToolPermissions


def _result():
    return FilterResult()


async def test_tool_filter_allow_all():
    flt = ToolPermissionFilter(ToolPermissions(allow_all=True))
    rsp = _result()
    await flt._before(None, None, rsp)
    assert rsp.is_continue is True


async def test_tool_filter_denylist_blocks(monkeypatch):
    flt = ToolPermissionFilter(ToolPermissions(denylist=["danger"]))
    tool = MagicMock()
    tool.name = "danger"
    monkeypatch.setattr("trpc_agent_sdk.tenant._governance.get_tool_var", lambda: tool)
    rsp = _result()
    await flt._before(None, None, rsp)
    assert rsp.is_continue is False


async def test_tool_filter_allowlist_blocks_unknown(monkeypatch):
    flt = ToolPermissionFilter(ToolPermissions(allowlist=["safe"]))
    tool = MagicMock()
    tool.name = "other"
    monkeypatch.setattr("trpc_agent_sdk.tenant._governance.get_tool_var", lambda: tool)
    rsp = _result()
    await flt._before(None, None, rsp)
    assert rsp.is_continue is False


async def test_budget_filter_blocks_over_limit():
    flt = BudgetFilter(BudgetConfig(enabled=True, per_request_token_limit=10))
    flt.add_tokens(11)
    rsp = _result()
    await flt._before(None, None, rsp)
    assert rsp.is_continue is False


async def test_budget_filter_allows_under_limit():
    flt = BudgetFilter(BudgetConfig(enabled=True, per_request_token_limit=10))
    flt.add_tokens(5)
    rsp = _result()
    await flt._before(None, None, rsp)
    assert rsp.is_continue is True


def test_filter_factory_assembles():
    tenant = Tenant(tenant_id="acme",
                    model_settings=ModelConfig(provider="openai", model_name="gpt-4o"),
                    tool_permissions=ToolPermissions(denylist=["danger"]),
                    budgets=BudgetConfig(enabled=True, per_request_token_limit=10))
    filters = TenantFilterFactory().build_filters(tenant)
    assert len(filters) == 2
    assert isinstance(filters[0], ToolPermissionFilter)
    assert isinstance(filters[1], BudgetFilter)


def test_filter_factory_empty():
    tenant = Tenant(tenant_id="acme",
                    model_settings=ModelConfig(provider="openai", model_name="gpt-4o"))
    assert TenantFilterFactory().build_filters(tenant) == []
```

- [ ] **Step 2: 运行测试验证失败**

Run: `.venv/bin/pytest tests/tenant/test_governance.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'trpc_agent_sdk.tenant._governance'`）

- [ ] **Step 3: 写最小实现**

`trpc_agent_sdk/tenant/_governance.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tenant governance filters."""

from __future__ import annotations

from trpc_agent_sdk.abc import FilterResult
from trpc_agent_sdk.abc import FilterType
from trpc_agent_sdk.filter import BaseFilter
from trpc_agent_sdk.tools import get_tool_var

from ._tenant import BudgetConfig
from ._tenant import Tenant
from ._tenant import ToolPermissions


class ToolPermissionFilter(BaseFilter):
    """Block tool execution for denylisted or non-allowlisted tools."""

    def __init__(self, permissions: ToolPermissions):
        super().__init__()
        self._permissions = permissions
        self._type = FilterType.TOOL

    async def _before(self, ctx, req, rsp: FilterResult):
        tool = get_tool_var()
        if tool is None:
            return
        name = getattr(tool, "name", "")
        if name in self._permissions.denylist:
            rsp.is_continue = False
            return
        if not self._permissions.allow_all and self._permissions.allowlist and name not in self._permissions.allowlist:
            rsp.is_continue = False
            return


class BudgetFilter(BaseFilter):
    """Block when per-request token usage exceeds the configured limit."""

    def __init__(self, budgets: BudgetConfig):
        super().__init__()
        self._budgets = budgets
        self._type = FilterType.AGENT
        self._token_count = 0

    def add_tokens(self, count: int) -> None:
        """Accumulate token usage (called by the outer layer from usage_metadata)."""
        self._token_count += count

    async def _before(self, ctx, req, rsp: FilterResult):
        limit = self._budgets.per_request_token_limit
        if limit is not None and self._token_count >= limit:
            rsp.is_continue = False
            return


class TenantFilterFactory:
    """Assemble a per-tenant governance filter list from Tenant config."""

    def build_filters(self, tenant: Tenant) -> list[BaseFilter]:
        filters: list[BaseFilter] = []
        permissions = tenant.tool_permissions
        if permissions.allow_all or permissions.allowlist or permissions.denylist:
            filters.append(ToolPermissionFilter(permissions))
        if tenant.budgets.enabled:
            filters.append(BudgetFilter(tenant.budgets))
        return filters
```

- [ ] **Step 4: 运行测试验证通过**

Run: `.venv/bin/pytest tests/tenant/test_governance.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```bash
git add trpc_agent_sdk/tenant/_governance.py tests/tenant/test_governance.py
git commit -m "feat(tenant): add governance filters"
```

---

### Task 3: 公开导出与全量验证

**Files:**
- Modify: `trpc_agent_sdk/tenant/__init__.py`

**Interfaces:**
- Produces：公开导出 `AuditEvent`、`AuditSink`、`InMemoryAuditSink`、`AuditLogger`、`ToolPermissionFilter`、`BudgetFilter`、`TenantFilterFactory`。

- [ ] **Step 1: 写失败测试**

在 `tests/tenant/test_audit.py` 末尾追加：
```python
def test_public_exports_governance():
    import trpc_agent_sdk.tenant as tenant_pkg
    for name in ("AuditEvent", "AuditSink", "InMemoryAuditSink", "AuditLogger",
                 "ToolPermissionFilter", "BudgetFilter", "TenantFilterFactory"):
        assert hasattr(tenant_pkg, name), f"missing export: {name}"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `.venv/bin/pytest tests/tenant/test_audit.py::test_public_exports_governance -v`
Expected: FAIL（`AssertionError`）

- [ ] **Step 3: 写最小实现**

在 `trpc_agent_sdk/tenant/__init__.py` 追加：
```python
from ._audit import AuditEvent
from ._audit import AuditLogger
from ._audit import AuditSink
from ._audit import InMemoryAuditSink
from ._governance import BudgetFilter
from ._governance import TenantFilterFactory
from ._governance import ToolPermissionFilter
```
并将 7 个名字加入 `__all__`（按字母序插入）。

- [ ] **Step 4: 运行全量测试**

Run: `.venv/bin/pytest tests/tenant/ -v`
Expected: PASS（全部通过）

- [ ] **Step 5: 运行 lint**

Run: `.venv/bin/python -m flake8 trpc_agent_sdk/tenant tests/tenant --max-line-length=120 --extend-exclude=".git,__pycache__"`
Expected: 无输出（干净）

- [ ] **Step 6: 提交**

```bash
git add trpc_agent_sdk/tenant/__init__.py tests/tenant/test_audit.py
git commit -m "feat(tenant): expose governance and audit API"
```

---

## 自检清单

1. **规格覆盖**：SP5 规格的审计（§4）、治理 Filter（§5）、测试（§6）均有对应 Task。
2. **无占位符**：所有 Task 含完整代码与测试、命令与预期输出。
3. **类型/命名一致**：`AuditEvent`/`AuditSink`/`InMemoryAuditSink`/`AuditLogger`/`ToolPermissionFilter`/`BudgetFilter`/`TenantFilterFactory`/`get_tool_var` 与 SP1/SDK 一致。
