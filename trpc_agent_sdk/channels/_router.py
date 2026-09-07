# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Channel router: webhook -> tenant + verify + dedup + session/user mapping."""

from __future__ import annotations

import time
from typing import Optional

from pydantic import BaseModel

from trpc_agent_sdk.types import Content

from trpc_agent_sdk.tenant._registry import TenantRegistry
from trpc_agent_sdk.tenant._secret_store import SecretStore
from trpc_agent_sdk.tenant._tenant import ChannelBinding
from trpc_agent_sdk.tenant._tenant import Tenant

from ._base import ChannelAdapter
from ._errors import AuthenticationError
from ._errors import DuplicateMessageError
from ._messages import InboundMessage
from ._messages import content_from_text


class InMemoryDedupStore:
    """Deduplicate (channel, external_msg_id) pairs with a TTL."""

    def __init__(self, default_ttl_seconds: float = 300.0):
        self._default_ttl_seconds = default_ttl_seconds
        self._seen: dict[tuple[str, str], float] = {}

    def seen(self, channel: str, msg_id: str, ttl_seconds: Optional[float] = None) -> bool:
        """Return True if the message was already seen; otherwise record it and return False."""
        key = (channel, msg_id)
        now = time.monotonic()
        expiry = self._seen.get(key)
        if expiry is not None and now < expiry:
            return True
        ttl = self._default_ttl_seconds if ttl_seconds is None else ttl_seconds
        self._seen[key] = now + ttl
        return False


class RoutedMessage(BaseModel):
    """The normalized result of routing an inbound webhook message."""

    tenant_id: str
    user_id: str
    session_id: str
    content: Content
    inbound: InboundMessage


class ChannelRouter:
    """Resolve a webhook to a tenant, verify, dedupe, and map session/user."""

    def __init__(self, registry: TenantRegistry, secret_store: SecretStore,
                 adapters: dict[str, ChannelAdapter], dedup: InMemoryDedupStore):
        self._registry = registry
        self._secret_store = secret_store
        self._adapters = adapters
        self._dedup = dedup

    async def route(self, *, channel_type: str, platform_id: str, raw: dict,
                    signature: str, timestamp: str, nonce: str, payload: str = "") -> RoutedMessage:
        adapter = self._adapters.get(channel_type)
        if adapter is None:
            raise ValueError(f"unsupported channel type: {channel_type}")
        tenant, binding = await self._resolve_binding(channel_type, platform_id)
        token = await self._secret_store.get(binding.token_ref) if binding.token_ref else ""
        secret = await self._secret_store.get(binding.secret_ref) if binding.secret_ref else ""
        if not adapter.verify_signature(token=token, secret=secret, signature=signature,
                                        timestamp=timestamp, nonce=nonce, payload=payload):
            raise AuthenticationError(channel_type)
        inbound = adapter.parse_inbound(raw)
        if self._dedup.seen(channel_type, inbound.external_msg_id):
            raise DuplicateMessageError(channel_type, inbound.external_msg_id)
        return RoutedMessage(tenant_id=tenant.tenant_id,
                             user_id=adapter.user_id(inbound),
                             session_id=adapter.session_id(inbound),
                             content=content_from_text(inbound.text),
                             inbound=inbound)

    async def _resolve_binding(self, channel_type: str, platform_id: str) -> tuple[Tenant, ChannelBinding]:
        for tenant in await self._registry.list():
            if tenant.status != "active":
                continue
            for binding in tenant.im_channels:
                if binding.channel_type == channel_type and binding.external_account_id == platform_id:
                    return tenant, binding
        raise ValueError(f"no channel binding for {channel_type}/{platform_id}")
