# SP4 — IM Channel Adapter 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增顶层包 `trpc_agent_sdk/channels`：归一化 IM 消息模型 + 入站/出站转换、`ChannelAdapter` 抽象、`WecomAdapter`/`TelegramAdapter`（验签/解析/session 规则）、`ChannelRouter`（webhook → 租户绑定 + 验签 + 去重 + session/user 映射）。

**Architecture:** 纯新增，单向依赖 `trpc_agent_sdk.tenant`（`ChannelBinding`/`SecretStore`/`TenantRegistry`/`TenantNotFoundError`）与 SDK（`Content`/`Part`/`Event`）。纯标准库（`hashlib`/`hmac`/`time`）+ pydantic，无网络。验签/去重/映射均为可无网测试的协议逻辑。

**Tech Stack:** Python 3.10+、pydantic v2、pytest + pytest-asyncio（`asyncio_mode=auto`）。

## Global Constraints

- 包位置：顶层 `trpc_agent_sdk/channels/`，测试 `tests/channels/`。
- 验签失败抛 `AuthenticationError`；去重重复抛 `DuplicateMessageError`。
- session 规则：单聊 `chat_{channel}_{chat_id}`，群聊 `group_{channel}_{chat_id}`；`user_id` = `sender_id`。
- 不引入 nanobot/python-telegram-bot/wechatpy；纯标准库 + pydantic。
- 测试命令：`.venv/bin/pytest tests/channels/... -v`（**必须用 `.venv/bin/pytest`**）。
- 行宽 120；flake8 忽略 `E402, W503`；抽象方法用 docstring 体（不要 `...` 一行，E704）。
- 提交信息前缀：`feat(channels):`。

---

## 文件结构

```
trpc_agent_sdk/channels/
    __init__.py       # 公开导出
    _errors.py        # ChannelError / AuthenticationError / DuplicateMessageError
    _messages.py      # InboundMessage / OutboundMessage / content_from_text / event_to_text
    _base.py          # ChannelAdapter ABC（verify_signature/parse_inbound 抽象；render/session/user 通用）
    _wecom.py         # WecomAdapter
    _telegram.py      # TelegramAdapter
    _router.py        # ChannelRouter + InMemoryDedupStore + RoutedMessage

tests/channels/
    __init__.py
    test_messages.py
    test_wecom.py
    test_telegram.py
    test_router.py
```

---

### Task 1: 错误与消息模型

**Files:**
- Create: `trpc_agent_sdk/channels/__init__.py`
- Create: `trpc_agent_sdk/channels/_errors.py`
- Create: `trpc_agent_sdk/channels/_messages.py`
- Create: `tests/channels/__init__.py`
- Create: `tests/channels/test_messages.py`

**Interfaces:**
- Produces: `ChannelError`/`AuthenticationError(channel)`/`DuplicateMessageError(channel, msg_id)`；`InboundMessage`、`OutboundMessage`；`content_from_text(text) -> Content`、`event_to_text(event) -> str`。

- [ ] **Step 1: 写失败测试**

`tests/channels/test_messages.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for channel message models and helpers."""

from trpc_agent_sdk.events import Event
from trpc_agent_sdk.types import Content
from trpc_agent_sdk.types import Part

from trpc_agent_sdk.channels._errors import AuthenticationError
from trpc_agent_sdk.channels._errors import ChannelError
from trpc_agent_sdk.channels._errors import DuplicateMessageError
from trpc_agent_sdk.channels._messages import InboundMessage
from trpc_agent_sdk.channels._messages import content_from_text
from trpc_agent_sdk.channels._messages import event_to_text


def test_content_from_text():
    content = content_from_text("hello")
    assert isinstance(content, Content)
    assert content.parts[0].text == "hello"


def test_content_from_empty_text():
    content = content_from_text("")
    assert content.parts[0].text == ""


def test_event_to_text_concatenates_parts():
    event = Event(invocation_id="i1", author="agent",
                  content=Content(parts=[Part(text="foo"), Part(text="bar")]))
    assert event_to_text(event) == "foobar"


def test_inbound_message_fields():
    msg = InboundMessage(channel="wecom", external_msg_id="m1", chat_type="group",
                         chat_id="room1", sender_id="u1", text="hi", timestamp=1.0)
    assert msg.chat_type == "group"
    assert msg.chat_id == "room1"


def test_channel_errors():
    assert issubclass(AuthenticationError, ChannelError)
    assert issubclass(DuplicateMessageError, ChannelError)
    assert "wecom" in str(AuthenticationError("wecom"))
    assert "wecom/m1" in str(DuplicateMessageError("wecom", "m1"))
```

- [ ] **Step 2: 运行测试验证失败**

Run: `.venv/bin/pytest tests/channels/test_messages.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'trpc_agent_sdk.channels'`）

- [ ] **Step 3: 写最小实现**

`trpc_agent_sdk/channels/__init__.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""IM channel adapters."""
```

`trpc_agent_sdk/channels/_errors.py`:
```python
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
```

`trpc_agent_sdk/channels/_messages.py`:
```python
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
```

`tests/channels/__init__.py`（空）:
```python
```

- [ ] **Step 4: 运行测试验证通过**

Run: `.venv/bin/pytest tests/channels/test_messages.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add trpc_agent_sdk/channels tests/channels
git commit -m "feat(channels): add message models and channel errors"
```

---

### Task 2: ChannelAdapter 抽象 + WecomAdapter

**Files:**
- Create: `trpc_agent_sdk/channels/_base.py`
- Create: `trpc_agent_sdk/channels/_wecom.py`
- Create: `tests/channels/test_wecom.py`

**Interfaces:**
- Consumes: `InboundMessage`/`OutboundMessage`/`event_to_text`（Task 1）。
- Produces: `ChannelAdapter`（`channel_type`；抽象 `verify_signature`/`parse_inbound`；通用 `render_outbound(chat_id, event)`/`session_id(msg)`/`user_id(msg)`）；`WecomAdapter`（sha1 sort 验签、企微 parse、继承通用 render/session/user）。

- [ ] **Step 1: 写失败测试**

`tests/channels/test_wecom.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for WecomAdapter."""

import hashlib

from trpc_agent_sdk.events import Event
from trpc_agent_sdk.types import Content
from trpc_agent_sdk.types import Part

from trpc_agent_sdk.channels._wecom import WecomAdapter


def _signature(token, timestamp, nonce, payload):
    values = sorted([token, timestamp, nonce, payload])
    return hashlib.sha1("".join(values).encode("utf-8")).hexdigest()


def test_verify_signature_ok():
    adapter = WecomAdapter()
    sig = _signature("tok", "123", "abc", "echostr")
    assert adapter.verify_signature(token="tok", secret="", signature=sig,
                                    timestamp="123", nonce="abc", payload="echostr") is True


def test_verify_signature_bad():
    adapter = WecomAdapter()
    assert adapter.verify_signature(token="tok", secret="", signature="wrong",
                                    timestamp="123", nonce="abc", payload="echostr") is False


def test_parse_inbound_single():
    adapter = WecomAdapter()
    msg = adapter.parse_inbound({"msg_id": "m1", "from_user": "u1", "text": "hi", "timestamp": 1})
    assert msg.chat_type == "single"
    assert msg.chat_id == "u1"
    assert msg.external_msg_id == "m1"
    assert msg.text == "hi"


def test_parse_inbound_group():
    adapter = WecomAdapter()
    msg = adapter.parse_inbound({"msg_id": "m2", "roomid": "room1", "from_user": "u2", "text": "yo", "timestamp": 2})
    assert msg.chat_type == "group"
    assert msg.chat_id == "room1"


def test_session_id_rules():
    adapter = WecomAdapter()
    single = adapter.session_id(adapter.parse_inbound({"msg_id": "m", "from_user": "u1", "text": "", "timestamp": 0}))
    group = adapter.session_id(adapter.parse_inbound({"msg_id": "m", "roomid": "r1", "from_user": "u1", "text": "", "timestamp": 0}))
    assert single == "chat_wecom_u1"
    assert group == "group_wecom_r1"


def test_user_id_and_render():
    adapter = WecomAdapter()
    msg = adapter.parse_inbound({"msg_id": "m", "from_user": "u1", "text": "", "timestamp": 0})
    assert adapter.user_id(msg) == "u1"
    event = Event(invocation_id="i1", author="agent", content=Content(parts=[Part(text="answer")]))
    outbound = adapter.render_outbound("u1", event)
    assert outbound[0].channel == "wecom"
    assert outbound[0].chat_id == "u1"
    assert outbound[0].text == "answer"
    assert outbound[0].is_final is True
```

- [ ] **Step 2: 运行测试验证失败**

Run: `.venv/bin/pytest tests/channels/test_wecom.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'trpc_agent_sdk.channels._base'`）

- [ ] **Step 3: 写最小实现**

`trpc_agent_sdk/channels/_base.py`:
```python
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
```

`trpc_agent_sdk/channels/_wecom.py`:
```python
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
```

- [ ] **Step 4: 运行测试验证通过**

Run: `.venv/bin/pytest tests/channels/test_wecom.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
git add trpc_agent_sdk/channels/_base.py trpc_agent_sdk/channels/_wecom.py tests/channels/test_wecom.py
git commit -m "feat(channels): add ChannelAdapter base and WecomAdapter"
```

---

### Task 3: TelegramAdapter

**Files:**
- Create: `trpc_agent_sdk/channels/_telegram.py`
- Create: `tests/channels/test_telegram.py`

**Interfaces:**
- Consumes: `ChannelAdapter`（Task 2）。
- Produces: `TelegramAdapter`（`hmac.compare_digest` 验签、Telegram Update parse、继承通用 render/session/user）。

- [ ] **Step 1: 写失败测试**

`tests/channels/test_telegram.py`:
```python
# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for TelegramAdapter."""

from trpc_agent_sdk.channels._telegram import TelegramAdapter


def test_verify_signature_ok():
    adapter = TelegramAdapter()
    assert adapter.verify_signature(token="", secret="s3cr3t", signature="s3cr3t",
                                    timestamp="", nonce="", payload="") is True


def test_verify_signature_bad():
    adapter = TelegramAdapter()
    assert adapter.verify_signature(token="", secret="s3cr3t", signature="nope",
                                    timestamp="", nonce="", payload="") is False


def test_verify_signature_empty_secret():
    adapter = TelegramAdapter()
    assert adapter.verify_signature(token="", secret="", signature="",
                                    timestamp="", nonce="", payload="") is False


def test_parse_inbound_private():
    adapter = TelegramAdapter()
    raw = {"message": {"message_id": 1, "chat": {"id": 42, "type": "private"},
                       "from": {"id": 7}, "text": "hi", "date": 10}}
    msg = adapter.parse_inbound(raw)
    assert msg.chat_type == "single"
    assert msg.chat_id == "42"
    assert msg.sender_id == "7"
    assert msg.text == "hi"


def test_parse_inbound_group():
    adapter = TelegramAdapter()
    raw = {"message": {"message_id": 2, "chat": {"id": 99, "type": "group"},
                       "from": {"id": 7}, "text": "yo", "date": 11}}
    msg = adapter.parse_inbound(raw)
    assert msg.chat_type == "group"
    assert adapter.session_id(msg) == "group_telegram_99"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `.venv/bin/pytest tests/channels/test_telegram.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'trpc_agent_sdk.channels._telegram'`）

- [ ] **Step 3: 写最小实现**

`trpc_agent_sdk/channels/_telegram.py`:
```python
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

    def verify_signature(self, *, token: str, secret: str,
                         signature: str, timestamp: str, nonce: str, payload: str) -> bool:
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
```

- [ ] **Step 4: 运行测试验证通过**

Run: `.venv/bin/pytest tests/channels/test_telegram.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add trpc_agent_sdk/channels/_telegram.py tests/channels/test_telegram.py
git commit -m "feat(channels): add TelegramAdapter"
```

---

### Task 4: ChannelRouter

**Files:**
- Create: `trpc_agent_sdk/channels/_router.py`
- Create: `tests/channels/test_router.py`

**Interfaces:**
- Consumes: `ChannelAdapter`（Task 2）、`AuthenticationError`/`DuplicateMessageError`（Task 1）、`ChannelBinding`/`SecretStore`/`TenantRegistry`/`Tenant`（`trpc_agent_sdk.tenant`）、`content_from_text`（Task 1）。
- Produces: `InMemoryDedupStore(default_ttl_seconds=300.0)`（`seen(channel, msg_id, ttl_seconds=None) -> bool`）；`RoutedMessage`；`ChannelRouter(registry, secret_store, adapters, dedup)`（`async route(...) -> RoutedMessage`）。

- [ ] **Step 1: 写失败测试**

`tests/channels/test_router.py`:
```python
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
```

- [ ] **Step 2: 运行测试验证失败**

Run: `.venv/bin/pytest tests/channels/test_router.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'trpc_agent_sdk.channels._router'`）

- [ ] **Step 3: 写最小实现**

`trpc_agent_sdk/channels/_router.py`:
```python
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
```

- [ ] **Step 4: 运行测试验证通过**

Run: `.venv/bin/pytest tests/channels/test_router.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add trpc_agent_sdk/channels/_router.py tests/channels/test_router.py
git commit -m "feat(channels): add ChannelRouter and dedup store"
```

---

### Task 5: 公开导出与全量验证

**Files:**
- Modify: `trpc_agent_sdk/channels/__init__.py`

**Interfaces:**
- Produces：公开导出 `ChannelAdapter`、`WecomAdapter`、`TelegramAdapter`、`ChannelRouter`、`InMemoryDedupStore`、`InboundMessage`、`OutboundMessage`、`content_from_text`、`event_to_text`、`AuthenticationError`、`DuplicateMessageError`。

- [ ] **Step 1: 写失败测试**

在 `tests/channels/test_router.py` 末尾追加：
```python
def test_public_exports_channels():
    import trpc_agent_sdk.channels as channels
    for name in ("ChannelAdapter", "WecomAdapter", "TelegramAdapter", "ChannelRouter",
                 "InMemoryDedupStore", "InboundMessage", "OutboundMessage",
                 "content_from_text", "event_to_text", "AuthenticationError", "DuplicateMessageError"):
        assert hasattr(channels, name), f"missing export: {name}"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `.venv/bin/pytest tests/channels/test_router.py::test_public_exports_channels -v`
Expected: FAIL（`AssertionError`）

- [ ] **Step 3: 写最小实现**

`trpc_agent_sdk/channels/__init__.py`:
```python
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
```

- [ ] **Step 4: 运行全量测试**

Run: `.venv/bin/pytest tests/channels/ -v`
Expected: PASS（全部通过）

- [ ] **Step 5: 运行 lint**

Run: `.venv/bin/python -m flake8 trpc_agent_sdk/channels tests/channels --max-line-length=120 --extend-exclude=".git,__pycache__"`
Expected: 无输出（干净）

- [ ] **Step 6: 提交**

```bash
git add trpc_agent_sdk/channels/__init__.py tests/channels/test_router.py
git commit -m "feat(channels): expose channel public API"
```

---

## 自检清单

1. **规格覆盖**：SP4 规格的消息模型（§4）、ABC（§5）、适配器（§6）、Router（§7）、测试（§8）均有对应 Task。
2. **无占位符**：所有 Task 含完整代码与测试、命令与预期输出。
3. **类型/命名一致**：`channel_type`、`session_id`/`user_id`、`content_from_text`/`event_to_text`、`AuthenticationError`/`DuplicateMessageError`、`external_account_id` 与 SP1 `ChannelBinding` 一致。
