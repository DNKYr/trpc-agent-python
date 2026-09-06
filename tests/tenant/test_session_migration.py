# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for session migration."""

from trpc_agent_sdk.events import Event
from trpc_agent_sdk.types import Content
from trpc_agent_sdk.types import Part

from trpc_agent_sdk.tenant._backend_factory import BackendFactory
from trpc_agent_sdk.tenant._registry import TenantRegistry
from trpc_agent_sdk.tenant._storage_adapter import StorageAdapter
from trpc_agent_sdk.tenant._tenant import BackendSpec
from trpc_agent_sdk.tenant._tenant import ModelConfig
from trpc_agent_sdk.tenant._tenant import Tenant
from trpc_agent_sdk.tenant._tenant_source import InMemoryTenantSource

APP_NAME = "acme:default"


def _tenant():
    return Tenant(tenant_id="acme", model_settings=ModelConfig(provider="openai", model_name="gpt-4o"))


def _make_adapter():
    source = InMemoryTenantSource()
    factory = BackendFactory()
    adapter = StorageAdapter(TenantRegistry(source), factory)
    return adapter, source, factory


def _user_event(text):
    return Event(invocation_id="inv1", author="user", content=Content(parts=[Part(text=text)]))


async def test_migrate_in_memory_to_sql():
    adapter, source, factory = _make_adapter()
    await source.put(_tenant())
    src_spec = BackendSpec(type="in_memory")
    tgt_spec = BackendSpec(type="sql", dsn="sqlite:///:memory:")

    src = await factory.session_service(src_spec)
    session = await src.create_session(app_name=APP_NAME, user_id="u1", session_id="s1")
    await src.append_event(session, _user_event("hello"))

    migrated = await adapter.migrate_session(tenant_id="acme", source=src_spec, target=tgt_spec)
    assert migrated == 1

    tgt = await factory.session_service(tgt_spec)
    got = await tgt.get_session(app_name=APP_NAME, user_id="u1", session_id="s1")
    assert got is not None
    assert len(got.events) == 1
    await factory.close()


async def test_migrate_preserves_event_order():
    adapter, source, factory = _make_adapter()
    await source.put(_tenant())
    src_spec = BackendSpec(type="in_memory")
    tgt_spec = BackendSpec(type="sql", dsn="sqlite:///:memory:")

    src = await factory.session_service(src_spec)
    session = await src.create_session(app_name=APP_NAME, user_id="u1", session_id="s1")
    await src.append_event(session, _user_event("first"))
    await src.append_event(session, _user_event("second"))

    await adapter.migrate_session(tenant_id="acme", source=src_spec, target=tgt_spec)

    tgt = await factory.session_service(tgt_spec)
    got = await tgt.get_session(app_name=APP_NAME, user_id="u1", session_id="s1")
    assert [e.get_text() for e in got.events] == ["first", "second"]
    await factory.close()


async def test_migrate_is_idempotent():
    adapter, source, factory = _make_adapter()
    await source.put(_tenant())
    src_spec = BackendSpec(type="in_memory")
    tgt_spec = BackendSpec(type="sql", dsn="sqlite:///:memory:")

    src = await factory.session_service(src_spec)
    session = await src.create_session(app_name=APP_NAME, user_id="u1", session_id="s1")
    await src.append_event(session, _user_event("hello"))

    assert await adapter.migrate_session(tenant_id="acme", source=src_spec, target=tgt_spec) == 1
    assert await adapter.migrate_session(tenant_id="acme", source=src_spec, target=tgt_spec) == 0

    tgt = await factory.session_service(tgt_spec)
    got = await tgt.get_session(app_name=APP_NAME, user_id="u1", session_id="s1")
    assert len(got.events) == 1
    await factory.close()


async def test_migrate_sql_to_in_memory():
    adapter, source, factory = _make_adapter()
    await source.put(_tenant())
    src_spec = BackendSpec(type="sql", dsn="sqlite:///:memory:")
    tgt_spec = BackendSpec(type="in_memory")

    src = await factory.session_service(src_spec)
    session = await src.create_session(app_name=APP_NAME, user_id="u1", session_id="s1")
    await src.append_event(session, _user_event("hello"))

    await adapter.migrate_session(tenant_id="acme", source=src_spec, target=tgt_spec)

    tgt = await factory.session_service(tgt_spec)
    got = await tgt.get_session(app_name=APP_NAME, user_id="u1", session_id="s1")
    assert got is not None
    assert len(got.events) == 1
    await factory.close()
