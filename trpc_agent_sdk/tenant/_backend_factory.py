# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Backend factory for instantiating data services from BackendSpec."""

from __future__ import annotations

from typing import Optional

from trpc_agent_sdk.abc import ArtifactServiceABC
from trpc_agent_sdk.artifacts import InMemoryArtifactService
from trpc_agent_sdk.memory import BaseMemoryService
from trpc_agent_sdk.memory import InMemoryMemoryService
from trpc_agent_sdk.memory import RedisMemoryService
from trpc_agent_sdk.memory import SqlMemoryService
from trpc_agent_sdk.sessions import BaseSessionService
from trpc_agent_sdk.sessions import InMemorySessionService
from trpc_agent_sdk.sessions import RedisSessionService
from trpc_agent_sdk.sessions import SqlSessionService

from ._secret_store import SecretStore
from ._tenant import BackendSpec


class BackendFactory:
    """Instantiate and cache data backend services from BackendSpec."""

    def __init__(self, secret_store: Optional[SecretStore] = None, is_async: bool = False):
        self._secret_store = secret_store
        self._is_async = is_async
        self._session_cache: dict[str, BaseSessionService] = {}
        self._memory_cache: dict[str, BaseMemoryService] = {}
        self._artifact_cache: dict[str, ArtifactServiceABC] = {}

    async def session_service(self, spec: BackendSpec) -> BaseSessionService:
        key = spec.model_dump_json()
        if key not in self._session_cache:
            self._session_cache[key] = await self._build_session(spec)
        return self._session_cache[key]

    async def close(self) -> None:
        for svc in list(self._session_cache.values()):
            await svc.close()
        for svc in list(self._memory_cache.values()):
            await svc.close()
        self._session_cache.clear()
        self._memory_cache.clear()
        self._artifact_cache.clear()

    async def _build_session(self, spec: BackendSpec) -> BaseSessionService:
        options = dict(spec.options)
        # Full read: the factory owns the session read window (num_recent_events stays 0).
        options.pop("session_config", None)
        is_async = bool(options.pop("is_async", self._is_async))
        if spec.type == "in_memory":
            return InMemorySessionService(**options)
        dsn = await self._resolve_dsn(spec)
        if spec.type == "redis":
            return RedisSessionService(db_url=dsn, is_async=is_async, **options)
        if spec.type == "sql":
            return SqlSessionService(db_url=dsn, is_async=is_async, **options)
        raise ValueError(f"unsupported session backend type: {spec.type}")

    async def memory_service(self, spec: BackendSpec) -> BaseMemoryService:
        key = spec.model_dump_json()
        if key not in self._memory_cache:
            self._memory_cache[key] = await self._build_memory(spec)
        return self._memory_cache[key]

    async def artifact_service(self, spec: BackendSpec) -> ArtifactServiceABC:
        key = spec.model_dump_json()
        if key not in self._artifact_cache:
            self._artifact_cache[key] = self._build_artifact(spec)
        return self._artifact_cache[key]

    async def _build_memory(self, spec: BackendSpec) -> BaseMemoryService:
        options = dict(spec.options)
        is_async = bool(options.pop("is_async", self._is_async))
        enabled = bool(options.pop("enabled", True))
        if spec.type == "in_memory":
            return InMemoryMemoryService(enabled=enabled, **options)
        dsn = await self._resolve_dsn(spec)
        if spec.type == "redis":
            return RedisMemoryService(db_url=dsn, is_async=is_async, enabled=enabled, **options)
        if spec.type == "sql":
            return SqlMemoryService(db_url=dsn, is_async=is_async, enabled=enabled, **options)
        raise ValueError(f"unsupported memory backend type: {spec.type}")

    def _build_artifact(self, spec: BackendSpec) -> ArtifactServiceABC:
        if spec.type in ("in_memory", "redis"):
            return InMemoryArtifactService()
        raise ValueError(f"unsupported artifact backend type: {spec.type}")

    async def _resolve_dsn(self, spec: BackendSpec) -> str:
        dsn = spec.dsn or ""
        if spec.secret_ref:
            if self._secret_store is None:
                raise ValueError("secret_ref set but no SecretStore configured")
            secret = await self._secret_store.get(spec.secret_ref)
            dsn = dsn.replace("{password}", secret)
        return dsn
