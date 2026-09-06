# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for BackendFactory session service."""

from unittest.mock import patch

import pytest

from trpc_agent_sdk.sessions import InMemorySessionService
from trpc_agent_sdk.sessions import RedisSessionService
from trpc_agent_sdk.sessions import SqlSessionService

from trpc_agent_sdk.tenant._backend_factory import BackendFactory
from trpc_agent_sdk.tenant._secret_store import InMemorySecretStore
from trpc_agent_sdk.tenant._tenant import BackendSpec


async def test_session_in_memory():
    factory = BackendFactory()
    svc = await factory.session_service(BackendSpec(type="in_memory"))
    assert isinstance(svc, InMemorySessionService)


async def test_session_sql():
    factory = BackendFactory()
    svc = await factory.session_service(BackendSpec(type="sql", dsn="sqlite:///:memory:"))
    assert isinstance(svc, SqlSessionService)
    await factory.close()


async def test_session_redis():
    factory = BackendFactory()
    svc = await factory.session_service(BackendSpec(type="redis", dsn="redis://localhost:6379/0"))
    assert isinstance(svc, RedisSessionService)


async def test_session_cache_shares_instance():
    factory = BackendFactory()
    spec = BackendSpec(type="in_memory")
    assert (await factory.session_service(spec)) is (await factory.session_service(spec))


async def test_unsupported_session_type_raises():
    factory = BackendFactory()
    with pytest.raises(ValueError):
        await factory.session_service(BackendSpec(type="nosql"))


async def test_session_password_injected():
    store = InMemorySecretStore({"pw": "s3cr3t"})
    factory = BackendFactory(secret_store=store)
    spec = BackendSpec(type="redis", dsn="redis://:{password}@localhost:6379/0", secret_ref="pw")
    with patch("trpc_agent_sdk.tenant._backend_factory.RedisSessionService") as mock_cls:
        await factory.session_service(spec)
        assert mock_cls.call_args.kwargs["db_url"] == "redis://:s3cr3t@localhost:6379/0"


async def test_secret_ref_without_store_raises():
    factory = BackendFactory()
    spec = BackendSpec(type="redis", dsn="redis://:{password}@localhost:6379/0", secret_ref="pw")
    with pytest.raises(ValueError):
        await factory.session_service(spec)


from trpc_agent_sdk.artifacts import InMemoryArtifactService
from trpc_agent_sdk.memory import InMemoryMemoryService
from trpc_agent_sdk.memory import RedisMemoryService
from trpc_agent_sdk.memory import SqlMemoryService


async def test_memory_in_memory_enabled():
    factory = BackendFactory()
    svc = await factory.memory_service(BackendSpec(type="in_memory"))
    assert isinstance(svc, InMemoryMemoryService)
    assert svc.enabled is True


async def test_memory_sql():
    factory = BackendFactory()
    svc = await factory.memory_service(BackendSpec(type="sql", dsn="sqlite:///:memory:"))
    assert isinstance(svc, SqlMemoryService)
    await factory.close()


async def test_memory_redis():
    factory = BackendFactory()
    svc = await factory.memory_service(BackendSpec(type="redis", dsn="redis://localhost:6379/0"))
    assert isinstance(svc, RedisMemoryService)


async def test_memory_cache_shares_instance():
    factory = BackendFactory()
    spec = BackendSpec(type="in_memory")
    assert (await factory.memory_service(spec)) is (await factory.memory_service(spec))


async def test_unsupported_memory_type_raises():
    factory = BackendFactory()
    with pytest.raises(ValueError):
        await factory.memory_service(BackendSpec(type="nosql"))


async def test_artifact_in_memory():
    factory = BackendFactory()
    svc = await factory.artifact_service(BackendSpec(type="in_memory"))
    assert isinstance(svc, InMemoryArtifactService)


async def test_unsupported_artifact_type_raises():
    factory = BackendFactory()
    with pytest.raises(ValueError):
        await factory.artifact_service(BackendSpec(type="object"))
