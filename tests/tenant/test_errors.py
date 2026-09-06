# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for tenant error types."""

import pytest

from trpc_agent_sdk.tenant._errors import SecretNotFoundError
from trpc_agent_sdk.tenant._errors import TenantNotFoundError


def test_tenant_not_found_error():
    err = TenantNotFoundError("acme")
    assert err.tenant_id == "acme"
    assert "acme" in str(err)


def test_secret_not_found_error():
    err = SecretNotFoundError("api_key")
    assert err.ref == "api_key"
    assert "api_key" in str(err)


def test_errors_are_exceptions():
    assert issubclass(TenantNotFoundError, Exception)
    assert issubclass(SecretNotFoundError, Exception)
