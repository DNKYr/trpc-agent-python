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
    return Tenant(tenant_id="acme", model_settings=ModelConfig(provider="openai", model_name="gpt-4o"))


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
    assert loaded.model_settings.model_name == "gpt-4o"
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
