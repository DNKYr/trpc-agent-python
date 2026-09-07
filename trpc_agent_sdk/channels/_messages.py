# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Normalized IM message models and conversion helpers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from trpc_agent_sdk.events import Event
from trpc_agent_sdk.types import Content
from trpc_agent_sdk.types import Part


class InboundMessage(BaseModel):
    """A normalized inbound IM message."""

    channel: str
    external_msg_id: str
    chat_type: Literal["single", "group"]
    chat_id: str
    sender_id: str
    text: str
    timestamp: float


class OutboundMessage(BaseModel):
    """A normalized outbound IM message."""

    channel: str
    chat_id: str
    text: str
    is_final: bool = True


def content_from_text(text: str) -> Content:
    """Build a Content from plain text."""
    return Content(parts=[Part(text=text or "")])


def event_to_text(event: Event) -> str:
    """Concatenate all text parts of an event."""
    return event.get_text()
