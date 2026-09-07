# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for ChannelRouter."""

import pytest

from trpc_agent_sdk.channels._errors import AuthenticationError
from trpc_agent_sdk.channels._errors import DuplicateMessageError
from trpc_agent_sdk.channels._router import ChannelRouter
from trpc_agent_sdk.channels._router import InMemoryDedupStore
from trpc_agent_sdk.channels._telegram import TelegramAdapter
from trpc_agent_sdk.tenant._registry import TenantRegistry
from trpc_agent_sdk.tenant._secret_store import InMemorySecretStore
from trpc_agent_sdk.tenant._tenant import ChannelBinding
from trpc_agent_sdk.tenant._tenant import ModelConfig
from trpc_agent_sdk.tenant._tenant import Tenant
from trpc_agent_sdk.tenant._tenant_source import InMemoryTenantSource


def _make_router(secret_value="s3cr3t"):
    source = InMemoryTenantSource()
    tenant = Tenant(
        tenant_id="acme",
        model_settings=ModelConfig(provider="openai", model_name="gpt-4o"),
        im_channels=[ChannelBinding(binding_id="b1", channel_type="telegram", webhook_url="https://x.example",
                                    secret_ref="tg-secret", external_account_id="mybot")],
    )
    return source, tenant, secret_value


async def test_route_success():
    source, tenant, secret = _make_router()
    await source.put(tenant)
    store = InMemorySecretStore({"tg-secret": secret})
    router = ChannelRouter(TenantRegistry(source), store, {"telegram": TelegramAdapter()}, InMemoryDedupStore())
    raw = {"message": {"message_id": 1, "chat": {"id": 42, "type": "private"},
                       "from": {"id": 7}, "text": "hi", "date": 10}}
    routed = await router.route(channel_type="telegram", platform_id="mybot", raw=raw,
                                signature=secret, timestamp="", nonce="")
    assert routed.tenant_id == "acme"
    assert routed.user_id == "7"
    assert routed.session_id == "chat_telegram_42"
    assert routed.content.parts[0].text == "hi"


async def test_route_bad_signature():
    source, tenant, _ = _make_router()
    await source.put(tenant)
    store = InMemorySecretStore({"tg-secret": "s3cr3t"})
    router = ChannelRouter(TenantRegistry(source), store, {"telegram": TelegramAdapter()}, InMemoryDedupStore())
    raw = {"message": {"message_id": 1, "chat": {"id": 42, "type": "private"},
                       "from": {"id": 7}, "text": "hi", "date": 10}}
    with pytest.raises(AuthenticationError):
        await router.route(channel_type="telegram", platform_id="mybot", raw=raw,
                           signature="wrong", timestamp="", nonce="")


async def test_route_duplicate():
    source, tenant, secret = _make_router()
    await source.put(tenant)
    store = InMemorySecretStore({"tg-secret": secret})
    router = ChannelRouter(TenantRegistry(source), store, {"telegram": TelegramAdapter()}, InMemoryDedupStore())
    raw = {"message": {"message_id": 1, "chat": {"id": 42, "type": "private"},
                       "from": {"id": 7}, "text": "hi", "date": 10}}
    await router.route(channel_type="telegram", platform_id="mybot", raw=raw,
                       signature=secret, timestamp="", nonce="")
    with pytest.raises(DuplicateMessageError):
        await router.route(channel_type="telegram", platform_id="mybot", raw=raw,
                           signature=secret, timestamp="", nonce="")


async def test_route_unknown_binding():
    source, tenant, secret = _make_router()
    await source.put(tenant)
    store = InMemorySecretStore({"tg-secret": secret})
    router = ChannelRouter(TenantRegistry(source), store, {"telegram": TelegramAdapter()}, InMemoryDedupStore())
    raw = {"message": {"message_id": 1, "chat": {"id": 42, "type": "private"},
                       "from": {"id": 7}, "text": "hi", "date": 10}}
    with pytest.raises(ValueError):
        await router.route(channel_type="telegram", platform_id="unknown", raw=raw,
                           signature=secret, timestamp="", nonce="")


def test_public_exports_channels():
    import trpc_agent_sdk.channels as channels
    for name in ("ChannelAdapter", "WecomAdapter", "TelegramAdapter", "ChannelRouter",
                 "InMemoryDedupStore", "InboundMessage", "OutboundMessage",
                 "content_from_text", "event_to_text", "AuthenticationError", "DuplicateMessageError"):
        assert hasattr(channels, name), f"missing export: {name}"
