# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""IM channel adapters."""

from ._base import ChannelAdapter
from ._errors import AuthenticationError
from ._errors import ChannelError
from ._errors import DuplicateMessageError
from ._messages import InboundMessage
from ._messages import OutboundMessage
from ._messages import content_from_text
from ._messages import event_to_text
from ._router import ChannelRouter
from ._router import InMemoryDedupStore
from ._router import RoutedMessage
from ._telegram import TelegramAdapter
from ._wecom import WecomAdapter

__all__ = [
    "AuthenticationError",
    "ChannelAdapter",
    "ChannelError",
    "ChannelRouter",
    "DuplicateMessageError",
    "InMemoryDedupStore",
    "InboundMessage",
    "OutboundMessage",
    "RoutedMessage",
    "TelegramAdapter",
    "WecomAdapter",
    "content_from_text",
    "event_to_text",
]
