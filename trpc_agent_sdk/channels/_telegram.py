# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Telegram channel adapter."""

from __future__ import annotations

import hmac

from ._base import ChannelAdapter
from ._messages import InboundMessage


class TelegramAdapter(ChannelAdapter):
    """Telegram channel adapter (protocol logic only)."""

    channel_type = "telegram"

    def verify_signature(self, *, token: str, secret: str, signature: str, timestamp: str, nonce: str,
                         payload: str) -> bool:
        if not secret:
            return False
        return hmac.compare_digest(secret, signature)

    def parse_inbound(self, raw: dict) -> InboundMessage:
        message = raw["message"]
        chat = message["chat"]
        chat_type = "group" if chat.get("type") in ("group", "supergroup") else "single"
        return InboundMessage(
            channel=self.channel_type,
            external_msg_id=str(message["message_id"]),
            chat_type=chat_type,
            chat_id=str(chat["id"]),
            sender_id=str(message["from"]["id"]),
            text=str(message.get("text", "")),
            timestamp=float(message.get("date", 0)),
        )
