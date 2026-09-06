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
    factory.build_runner = AsyncMock(side_effect=lambda tenant_id: AsyncMock())
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
