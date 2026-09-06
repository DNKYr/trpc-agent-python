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
