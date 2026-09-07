# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""WeCom (企业微信) channel adapter."""

from __future__ import annotations

import hashlib

from ._base import ChannelAdapter
from ._messages import InboundMessage


class WecomAdapter(ChannelAdapter):
    """WeCom channel adapter (protocol logic only)."""

    channel_type = "wecom"

    def verify_signature(self, *, token: str, secret: str,
                         signature: str, timestamp: str, nonce: str, payload: str) -> bool:
        values = sorted([token, timestamp, nonce, payload])
        computed = hashlib.sha1("".join(values).encode("utf-8")).hexdigest()
        return computed == signature

    def parse_inbound(self, raw: dict) -> InboundMessage:
        chat_type = "group" if raw.get("roomid") else "single"
        chat_id = str(raw.get("roomid") or raw.get("from_user"))
        return InboundMessage(
            channel=self.channel_type,
            external_msg_id=str(raw["msg_id"]),
            chat_type=chat_type,
            chat_id=chat_id,
            sender_id=str(raw["from_user"]),
            text=str(raw.get("text", "")),
            timestamp=float(raw.get("timestamp", 0)),
        )
