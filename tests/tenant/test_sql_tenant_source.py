# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for the SQL tenant source using in-memory SQLite."""

from trpc_agent_sdk.tenant._tenant import ModelConfig
from trpc_agent_sdk.tenant._tenant import Tenant
from trpc_agent_sdk.tenant._tenant_source import SqlTenantSource


def _tenant():
    return Tenant(tenant_id="acme", model_settings=ModelConfig(provider="openai", model_name="gpt-4o"))


async def test_sql_roundtrip():
    source = SqlTenantSource(db_url="sqlite:///:memory:", is_async=False)
    await source.put(_tenant())
    loaded = await source.get("acme")
    assert loaded is not None
    assert loaded.model_settings.model_name == "gpt-4o"
    await source.close()


async def test_sql_update_overwrites_config():
    source = SqlTenantSource(db_url="sqlite:///:memory:", is_async=False)
    await source.put(_tenant())
    updated = _tenant()
    updated.model_settings.model_name = "gpt-4o-mini"
    await source.put(updated)
    loaded = await source.get("acme")
    assert loaded.model_settings.model_name == "gpt-4o-mini"
    await source.close()


async def test_sql_list():
    source = SqlTenantSource(db_url="sqlite:///:memory:", is_async=False)
    await source.put(Tenant(tenant_id="acme", model_settings=ModelConfig(provider="openai", model_name="gpt-4o")))
    await source.put(Tenant(tenant_id="beta", model_settings=ModelConfig(provider="openai", model_name="gpt-4o")))
    tenants = await source.list()
    assert {t.tenant_id for t in tenants} == {"acme", "beta"}
    await source.close()


async def test_sql_update_increments_version():
    source = SqlTenantSource(db_url="sqlite:///:memory:", is_async=False)
    await source.put(_tenant())
    updated = _tenant()
    updated.model_settings.model_name = "gpt-4o-mini"
    await source.put(updated)
    loaded = await source.get("acme")
    assert loaded.version == 2
    assert loaded.model_settings.model_name == "gpt-4o-mini"
    await source.close()


async def test_sql_delete():
    source = SqlTenantSource(db_url="sqlite:///:memory:", is_async=False)
    await source.put(_tenant())
    await source.delete("acme")
    assert await source.get("acme") is None
    await source.close()
