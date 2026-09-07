# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""ChannelAdapter abstract base class."""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod

from trpc_agent_sdk.events import Event

from ._messages import InboundMessage
from ._messages import OutboundMessage
from ._messages import event_to_text


class ChannelAdapter(ABC):
    """Abstract channel adapter for IM platforms."""

    channel_type: str = ""

    @abstractmethod
    def verify_signature(self, *, token: str, secret: str,
                         signature: str, timestamp: str, nonce: str, payload: str) -> bool:
        """Verify a webhook signature using the resolved token/secret."""

    @abstractmethod
    def parse_inbound(self, raw: dict) -> InboundMessage:
        """Parse a raw platform message into an InboundMessage."""

    def render_outbound(self, chat_id: str, event: Event) -> list[OutboundMessage]:
        """Render an agent event into outbound text message(s)."""
        return [OutboundMessage(channel=self.channel_type, chat_id=chat_id,
                                text=event_to_text(event), is_final=not event.partial)]

    def session_id(self, msg: InboundMessage) -> str:
        """Derive the session id for a message (single vs group chat)."""
        prefix = "group" if msg.chat_type == "group" else "chat"
        return f"{prefix}_{self.channel_type}_{msg.chat_id}"

    def user_id(self, msg: InboundMessage) -> str:
        """Map an external sender to the internal user_id."""
        return msg.sender_id
