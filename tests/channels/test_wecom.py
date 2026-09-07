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
    group = adapter.session_id(adapter.parse_inbound(
        {"msg_id": "m", "roomid": "r1", "from_user": "u1", "text": "", "timestamp": 0}))
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
