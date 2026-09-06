# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Secret store abstraction and implementations."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod

from ._errors import SecretNotFoundError


class SecretStore(ABC):
    """Abstract secret store for IM tokens, model API keys, DB passwords, etc."""

    @abstractmethod
    async def get(self, ref: str) -> str:
        """Return the secret value for the reference; raise SecretNotFoundError if missing."""

    @abstractmethod
    async def put(self, ref: str, value: str) -> None:
        """Store a secret value under the reference."""

    @abstractmethod
    async def delete(self, ref: str) -> None:
        """Delete the secret under the reference."""


class EnvSecretStore(SecretStore):
    """Environment-variable backed secret store."""

    def __init__(self, prefix: str = "TENANT_SECRET_"):
        self._prefix = prefix

    async def get(self, ref: str) -> str:
        value = os.environ.get(self._prefix + ref)
        if value is None:
            raise SecretNotFoundError(ref)
        return value

    async def put(self, ref: str, value: str) -> None:
        os.environ[self._prefix + ref] = value

    async def delete(self, ref: str) -> None:
        os.environ.pop(self._prefix + ref, None)


class InMemorySecretStore(SecretStore):
    """In-memory secret store for development and testing."""

    def __init__(self, secrets: dict[str, str] | None = None):
        self._secrets = dict(secrets or {})

    async def get(self, ref: str) -> str:
        if ref not in self._secrets:
            raise SecretNotFoundError(ref)
        return self._secrets[ref]

    async def put(self, ref: str, value: str) -> None:
        self._secrets[ref] = value

    async def delete(self, ref: str) -> None:
        self._secrets.pop(ref, None)
