# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tenant registry and source abstraction."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Awaitable, Callable, Optional

from ._tenant import Tenant


class TenantSource(ABC):
    """Abstract persistence source for tenant configurations."""

    @abstractmethod
    async def get(self, tenant_id: str) -> Optional[Tenant]:
        """Return the tenant config for tenant_id, or None if not found."""

    @abstractmethod
    async def put(self, tenant: Tenant) -> None:
        """Persist the tenant config."""

    @abstractmethod
    async def delete(self, tenant_id: str) -> None:
        """Delete the tenant config."""

    @abstractmethod
    async def list(self) -> list[Tenant]:
        """Return all tenant configs."""


class TenantRegistry:
    """Cached tenant registry with change notification and version rollback."""

    def __init__(self, source: TenantSource, *, cache_ttl_seconds: float = 30.0, history_size: int = 10):
        self._source = source
        self._cache_ttl_seconds = cache_ttl_seconds
        self._history_size = max(1, int(history_size))
        self._cache: dict[str, tuple[Tenant, float]] = {}
        self._history: dict[str, list[Tenant]] = {}
        self._subscribers: list[Callable[[str, int], Awaitable[None]]] = []

    async def get(self, tenant_id: str) -> Optional[Tenant]:
        now = time.monotonic()
        cached = self._cache.get(tenant_id)
        if cached is not None:
            tenant, cached_at = cached
            if now - cached_at < self._cache_ttl_seconds:
                return tenant if tenant.status == "active" else None
        tenant = await self._source.get(tenant_id)
        if tenant is None:
            return None
        self._cache[tenant_id] = (tenant, now)
        return tenant if tenant.status == "active" else None

    async def put(self, tenant: Tenant) -> Tenant:
        existing = await self._source.get(tenant.tenant_id)
        new_version = (existing.version + 1) if existing else (tenant.version or 1)
        stored = tenant.model_copy(update={"version": new_version})
        await self._source.put(stored)
        history = self._history.setdefault(stored.tenant_id, [])
        history.insert(0, stored)
        del history[self._history_size:]
        self._cache.pop(stored.tenant_id, None)
        for cb in self._subscribers:
            await cb(stored.tenant_id, new_version)
        return stored

    async def delete(self, tenant_id: str) -> None:
        await self._source.delete(tenant_id)
        self._cache.pop(tenant_id, None)
        self._history.pop(tenant_id, None)

    async def list(self) -> list[Tenant]:
        return await self._source.list()

    async def rollback(self, tenant_id: str, version: int) -> Optional[Tenant]:
        for snapshot in self._history.get(tenant_id, []):
            if snapshot.version == version:
                return await self.put(snapshot)
        return None

    def subscribe(self, cb: Callable[[str, int], Awaitable[None]]) -> None:
        """Register a change callback invoked with (tenant_id, new_version) on put."""
        self._subscribers.append(cb)
