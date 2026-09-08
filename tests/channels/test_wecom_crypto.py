# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for WeCom callback cryptography."""

import hashlib

import pytest

from trpc_agent_sdk.channels._wecom_crypto import WeComCrypto

TOKEN = "QDG6eK"
ENCODING_AES_KEY = "jWmYm7qr5nMoAUwZRjGtBxmz3KA1tkAj3ykkR6q2B2C"
CORP_ID = "wx5823bf96d3bd56c7"


def _crypto():
    return WeComCrypto(token=TOKEN, encoding_aes_key=ENCODING_AES_KEY, corp_id=CORP_ID)


def test_verify_signature_ok():
    crypto = _crypto()
    payload = "RypEvHKD8QQKFhvQ6QleEB4J58tiPdvo"
    sig = hashlib.sha1("".join(sorted([TOKEN, "1409659813", "1372623149", payload])).encode("utf-8")).hexdigest()
    assert crypto.verify_signature(sig, "1409659813", "1372623149", payload) is True


def test_verify_signature_bad():
    crypto = _crypto()
    assert crypto.verify_signature("deadbeef", "1409659813", "1372623149", "payload") is False


def test_encrypt_decrypt_roundtrip():
    crypto = _crypto()
    plaintext = "<xml><Content><![CDATA[hello]]></Content></xml>"
    encrypted = crypto.encrypt(plaintext)
    assert encrypted != plaintext
    assert crypto.decrypt(encrypted) == plaintext


def test_decrypt_corp_id_mismatch_raises():
    crypto = _crypto()
    other = WeComCrypto(token=TOKEN, encoding_aes_key=ENCODING_AES_KEY, corp_id="other_corp")
    encrypted = other.encrypt("hi")
    with pytest.raises(ValueError):
        crypto.decrypt(encrypted)


def test_invalid_encoding_aes_key_raises():
    with pytest.raises(ValueError):
        WeComCrypto(token=TOKEN, encoding_aes_key="too_short", corp_id=CORP_ID)
