# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Route tenant_id to Runner with lazy construction and config-change invalidation."""

from __future__ import annotations

from trpc_agent_sdk.runners import Runner

from ._agent_factory import TenantAgentFactory
from ._errors import TenantNotFoundError
from ._registry import TenantRegistry


class RunnerPool:
    """Route tenant_id -> Runner, rebuilding on tenant version change."""

    def __init__(self, registry: TenantRegistry, factory: TenantAgentFactory):
        self._registry = registry
        self._factory = factory
        self._runners: dict[str, Runner] = {}
        self._versions: dict[str, int] = {}

    async def get_runner(self, tenant_id: str) -> Runner:
        tenant = await self._registry.get(tenant_id)
        if tenant is None:
            raise TenantNotFoundError(tenant_id)
        cached = self._runners.get(tenant_id)
        if cached is not None and self._versions.get(tenant_id) == tenant.version:
            return cached
        runner = await self._factory.build_runner(tenant_id)
        self._runners[tenant_id] = runner
        self._versions[tenant_id] = tenant.version
        return runner

    async def close(self) -> None:
        for runner in self._runners.values():
            await runner.close()
        self._runners.clear()
        self._versions.clear()
