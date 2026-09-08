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


def test_event_to_text_excludes_thought_parts():
    thought = Part.from_text(text="internal reasoning")
    thought.thought = True
    answer = Part.from_text(text="visible answer")
    answer.thought = False
    event = Event(invocation_id="i1", author="agent",
                  content=Content(parts=[thought, answer]))
    assert event_to_text(event) == "visible answer"


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
