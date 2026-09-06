# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tenant-specific exceptions."""

from __future__ import annotations


class TenantNotFoundError(Exception):
    """Raised when a tenant is not found in the registry."""

    def __init__(self, tenant_id: str):
        super().__init__(f"tenant not found: {tenant_id}")
        self.tenant_id = tenant_id


class SecretNotFoundError(Exception):
    """Raised when a referenced secret is missing from the secret store."""

    def __init__(self, ref: str):
        super().__init__(f"secret not found: {ref}")
        self.ref = ref
