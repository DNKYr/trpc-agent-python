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
from ._tenant import BackendSpec
from ._tenant import Tenant


class StorageAdapter:
    """Resolve and cache per-tenant data backends."""

    def __init__(self, registry: TenantRegistry, factory: BackendFactory):
        self._registry = registry
        self._factory = factory
        self._session_cache: dict[str, BaseSessionService] = {}
        self._memory_cache: dict[str, BaseMemoryService] = {}
        self._artifact_cache: dict[str, ArtifactServiceABC] = {}
        self._session_versions: dict[str, int] = {}
        self._memory_versions: dict[str, int] = {}
        self._artifact_versions: dict[str, int] = {}

    async def session_service(self, tenant_id: str) -> BaseSessionService:
        tenant = await self._get_tenant(tenant_id)
        if self._session_versions.get(tenant_id) != tenant.version:
            self._session_cache[tenant_id] = await self._factory.session_service(tenant.data_backends.session)
            self._session_versions[tenant_id] = tenant.version
        return self._session_cache[tenant_id]

    async def memory_service(self, tenant_id: str) -> BaseMemoryService:
        tenant = await self._get_tenant(tenant_id)
        if self._memory_versions.get(tenant_id) != tenant.version:
            self._memory_cache[tenant_id] = await self._factory.memory_service(tenant.data_backends.memory)
            self._memory_versions[tenant_id] = tenant.version
        return self._memory_cache[tenant_id]

    async def artifact_service(self, tenant_id: str) -> ArtifactServiceABC:
        tenant = await self._get_tenant(tenant_id)
        if self._artifact_versions.get(tenant_id) != tenant.version:
            self._artifact_cache[tenant_id] = await self._factory.artifact_service(tenant.data_backends.artifact)
            self._artifact_versions[tenant_id] = tenant.version
        return self._artifact_cache[tenant_id]

    async def migrate_session(self, *, tenant_id: str,
                              source: BackendSpec, target: BackendSpec) -> int:
        """Copy a tenant's sessions from source backend to target backend."""
        app_name = (await self._get_tenant(tenant_id)).sdk_app_name
        src = await self._factory.session_service(source)
        dst = await self._factory.session_service(target)
        count = 0
        listed = await src.list_sessions(app_name=app_name)
        for summary in listed.sessions:
            if await dst.get_session(app_name=summary.app_name, user_id=summary.user_id,
                                     session_id=summary.id) is not None:
                continue
            full = await src.get_session(app_name=summary.app_name, user_id=summary.user_id,
                                         session_id=summary.id)
            if full is None:
                continue
            session = await dst.create_session(app_name=full.app_name, user_id=full.user_id,
                                               session_id=full.id, state=full.state)
            for event in full.events:
                await dst.append_event(session=session, event=event)
            count += 1
        return count

    async def close(self) -> None:
        await self._factory.close()

    async def _get_tenant(self, tenant_id: str) -> Tenant:
        tenant = await self._registry.get(tenant_id)
        if tenant is None:
            raise TenantNotFoundError(tenant_id)
        return tenant
