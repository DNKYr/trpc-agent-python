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
        model_settings=ModelConfig(provider="openai", model_name="gpt-4o"),
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
