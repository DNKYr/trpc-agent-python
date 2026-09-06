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
