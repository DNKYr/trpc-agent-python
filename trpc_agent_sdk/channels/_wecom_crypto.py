# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""WeCom (企业微信) callback cryptography.

Implements the AES-CBC scheme used by WeCom self-built-app callbacks:

- AES key = base64_decode(EncodingAESKey + "=") -> 32 bytes
- IV = first 16 bytes of the AES key
- plaintext layout: random(16 bytes) + msg_len(4 bytes, big-endian) + msg + corp_id
- PKCS#7 padding (16-byte block)

Signature verification: sha1(sort([token, timestamp, nonce, payload])).
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct

from cryptography.hazmat.primitives.ciphers import Cipher
from cryptography.hazmat.primitives.ciphers import algorithms
from cryptography.hazmat.primitives.ciphers import modes

_BLOCK_SIZE = 16


class WeComCrypto:
    """Signature verification plus AES encrypt/decrypt for WeCom callbacks."""

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str):
        self.token = token
        self.corp_id = corp_id
        self.aes_key = base64.b64decode(encoding_aes_key + "=")
        if len(self.aes_key) != 32:
            raise ValueError("EncodingAESKey must decode to a 32-byte AES key")

    def verify_signature(self, signature: str, timestamp: str, nonce: str, payload: str) -> bool:
        """Verify a callback signature against the sorted sha1 of (token, timestamp, nonce, payload)."""
        values = sorted([self.token, timestamp, nonce, payload])
        computed = hashlib.sha1("".join(values).encode("utf-8")).hexdigest()
        return computed == signature

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a plaintext message into the WeCom wire format (base64)."""
        random_bytes = os.urandom(16)
        msg = plaintext.encode("utf-8")
        data = random_bytes + struct.pack(">I", len(msg)) + msg + self.corp_id.encode("utf-8")
        padded = self._pkcs7_pad(data)
        encryptor = Cipher(algorithms.AES(self.aes_key), modes.CBC(self.aes_key[:16])).encryptor()
        return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode("utf-8")

    def decrypt(self, encrypted: str) -> str:
        """Decrypt a WeCom wire-format message and return the plaintext.

        Raises ValueError on corp_id mismatch or malformed padding.
        """
        decryptor = Cipher(algorithms.AES(self.aes_key), modes.CBC(self.aes_key[:16])).decryptor()
        data = decryptor.update(base64.b64decode(encrypted)) + decryptor.finalize()
        data = self._pkcs7_unpad(data)
        msg_len = struct.unpack(">I", data[16:20])[0]
        msg = data[20:20 + msg_len].decode("utf-8")
        corp_id = data[20 + msg_len:].decode("utf-8")
        if corp_id != self.corp_id:
            raise ValueError(f"corp_id mismatch: expected {self.corp_id}, got {corp_id}")
        return msg

    @staticmethod
    def _pkcs7_pad(data: bytes) -> bytes:
        pad = _BLOCK_SIZE - (len(data) % _BLOCK_SIZE)
        return data + bytes([pad]) * pad

    @staticmethod
    def _pkcs7_unpad(data: bytes) -> bytes:
        pad = data[-1]
        if pad < 1 or pad > _BLOCK_SIZE:
            raise ValueError("invalid PKCS#7 padding")
        return data[:-pad]
