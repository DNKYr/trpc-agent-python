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
        "data_backends": DataBackendConfig(session=BackendSpec(type="in_memory")),
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
    beta_backends = DataBackendConfig(session=BackendSpec(type="sql", dsn="sqlite:///:memory:"))
    await source.put(_tenant("beta", data_backends=beta_backends))
    adapter = StorageAdapter(TenantRegistry(source), BackendFactory())
    a = await adapter.session_service("acme")
    b = await adapter.session_service("beta")
    assert isinstance(a, InMemorySessionService)
    assert isinstance(b, SqlSessionService)
    await adapter.close()
