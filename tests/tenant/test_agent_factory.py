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
from trpc_agent_sdk.tenant._tenant import BackendSpec
from trpc_agent_sdk.tenant._tenant import DataBackendConfig
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
    backends = DataBackendConfig(
        session=BackendSpec(type="in_memory"),
        memory=BackendSpec(type="in_memory"),
        artifact=BackendSpec(type="in_memory"),
    )
    await source.put(Tenant(tenant_id="acme",
                            model_settings=ModelConfig(provider="openai", model_name="gpt-4o"),
                            data_backends=backends))
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
