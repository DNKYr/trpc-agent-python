# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for secret store implementations."""

import pytest

from trpc_agent_sdk.tenant._errors import SecretNotFoundError
from trpc_agent_sdk.tenant._secret_store import EnvSecretStore
from trpc_agent_sdk.tenant._secret_store import InMemorySecretStore


async def test_in_memory_get_put_delete():
    store = InMemorySecretStore()
    await store.put("api_key", "sk-secret-value")
    assert await store.get("api_key") == "sk-secret-value"
    await store.delete("api_key")
    with pytest.raises(SecretNotFoundError):
        await store.get("api_key")


async def test_in_memory_missing_raises_without_leaking_value():
    store = InMemorySecretStore()
    with pytest.raises(SecretNotFoundError) as exc_info:
        await store.get("db_password")
    assert "db_password" in str(exc_info.value)


async def test_env_secret_store_uses_prefix(monkeypatch):
    monkeypatch.setenv("TENANT_SECRET_API_KEY", "env-secret")
    store = EnvSecretStore()
    assert await store.get("API_KEY") == "env-secret"


async def test_env_secret_store_missing(monkeypatch):
    monkeypatch.delenv("TENANT_SECRET_NOPE", raising=False)
    store = EnvSecretStore()
    with pytest.raises(SecretNotFoundError):
        await store.get("NOPE")
