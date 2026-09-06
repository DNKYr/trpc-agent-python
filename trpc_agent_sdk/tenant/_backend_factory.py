# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Backend factory for instantiating data services from BackendSpec."""

from __future__ import annotations

from typing import Optional

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

    async def session_service(self, spec: BackendSpec) -> BaseSessionService:
        key = spec.model_dump_json()
        if key not in self._session_cache:
            self._session_cache[key] = await self._build_session(spec)
        return self._session_cache[key]

    async def close(self) -> None:
        for svc in list(self._session_cache.values()):
            await svc.close()
        self._session_cache.clear()

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

    async def _resolve_dsn(self, spec: BackendSpec) -> str:
        dsn = spec.dsn or ""
        if spec.secret_ref:
            if self._secret_store is None:
                raise ValueError("secret_ref set but no SecretStore configured")
            secret = await self._secret_store.get(spec.secret_ref)
            dsn = dsn.replace("{password}", secret)
        return dsn
