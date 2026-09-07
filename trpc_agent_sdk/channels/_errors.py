# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Channel-specific exceptions."""

from __future__ import annotations


class ChannelError(Exception):
    """Base class for channel errors."""


class AuthenticationError(ChannelError):
    """Raised when webhook signature verification fails."""

    def __init__(self, channel: str):
        super().__init__(f"signature verification failed for channel: {channel}")
        self.channel = channel


class DuplicateMessageError(ChannelError):
    """Raised when a duplicate IM message is detected."""

    def __init__(self, channel: str, msg_id: str):
        super().__init__(f"duplicate message: {channel}/{msg_id}")
        self.channel = channel
        self.msg_id = msg_id
