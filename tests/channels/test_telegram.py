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
