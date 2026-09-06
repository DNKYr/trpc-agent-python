# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Per-tenant storage adapter facade."""

from __future__ import annotations

from trpc_agent_sdk.abc import ArtifactServiceABC
from trpc_agent_sdk.memory import BaseMemoryService
from trpc_agent_sdk.sessions import BaseSessionService

from ._backend_factory import BackendFactory
from ._errors import TenantNotFoundError
from ._registry import TenantRegistry
from ._tenant import Tenant


class StorageAdapter:
    """Resolve and cache per-tenant data backends."""

    def __init__(self, registry: TenantRegistry, factory: BackendFactory):
        self._registry = registry
        self._factory = factory
        self._session_cache: dict[str, BaseSessionService] = {}
        self._memory_cache: dict[str, BaseMemoryService] = {}
        self._artifact_cache: dict[str, ArtifactServiceABC] = {}

    async def session_service(self, tenant_id: str) -> BaseSessionService:
        tenant = await self._get_tenant(tenant_id)
        if tenant_id not in self._session_cache:
            self._session_cache[tenant_id] = await self._factory.session_service(tenant.data_backends.session)
        return self._session_cache[tenant_id]

    async def memory_service(self, tenant_id: str) -> BaseMemoryService:
        tenant = await self._get_tenant(tenant_id)
        if tenant_id not in self._memory_cache:
            self._memory_cache[tenant_id] = await self._factory.memory_service(tenant.data_backends.memory)
        return self._memory_cache[tenant_id]

    async def artifact_service(self, tenant_id: str) -> ArtifactServiceABC:
        tenant = await self._get_tenant(tenant_id)
        if tenant_id not in self._artifact_cache:
            self._artifact_cache[tenant_id] = await self._factory.artifact_service(tenant.data_backends.artifact)
        return self._artifact_cache[tenant_id]

    async def close(self) -> None:
        await self._factory.close()

    async def _get_tenant(self, tenant_id: str) -> Tenant:
        tenant = await self._registry.get(tenant_id)
        if tenant is None:
            raise TenantNotFoundError(tenant_id)
        return tenant
