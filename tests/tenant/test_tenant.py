# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for the Tenant configuration model."""

import pytest
from pydantic import ValidationError

from trpc_agent_sdk.tenant._tenant import AuditPolicy
from trpc_agent_sdk.tenant._tenant import DataBackendConfig
from trpc_agent_sdk.tenant._tenant import ModelConfig
from trpc_agent_sdk.tenant._tenant import Tenant


def _make_tenant(**overrides):
    data = {
        "tenant_id": "acme",
        "model_settings": ModelConfig(provider="openai", model_name="gpt-4o"),
    }
    data.update(overrides)
    return Tenant(**data)


def test_sdk_app_name_combines_tenant_and_app():
    tenant = _make_tenant()
    assert tenant.sdk_app_name == "acme:default"


def test_sdk_app_name_uses_custom_app_name():
    tenant = _make_tenant(app_config={"app_name": "support"})
    assert tenant.sdk_app_name == "acme:support"


def test_tenant_id_rejects_invalid_chars():
    with pytest.raises(ValidationError):
        _make_tenant(tenant_id="acme:prod")


def test_tenant_id_rejects_empty():
    with pytest.raises(ValidationError):
        _make_tenant(tenant_id="")


def test_tenant_id_rejects_too_long():
    with pytest.raises(ValidationError):
        _make_tenant(tenant_id="a" * 65)


def test_app_name_rejects_separator():
    with pytest.raises(ValidationError):
        _make_tenant(app_config={"app_name": "a:b"})


def test_sample_rate_rejects_out_of_range():
    with pytest.raises(ValidationError):
        AuditPolicy(sample_rate=1.5)


def test_data_backends_default_to_redis():
    backends = DataBackendConfig()
    for name in ("session", "memory", "summary", "artifact", "knowledge", "audit"):
        assert getattr(backends, name).type == "redis"


def test_public_exports():
    import trpc_agent_sdk.tenant as tenant_pkg
    for name in (
        "Tenant", "ModelConfig", "AppConfig", "ToolPermissions", "ChannelBinding",
        "BackendSpec", "DataBackendConfig", "AuditPolicy", "BudgetConfig", "RateLimitConfig",
        "SecretStore", "EnvSecretStore", "InMemorySecretStore", "Masker", "SensitiveDataFilter",
        "TenantSource", "TenantRegistry", "InMemoryTenantSource", "FileTenantSource",
        "SqlTenantSource", "TenantNotFoundError", "SecretNotFoundError",
    ):
        assert hasattr(tenant_pkg, name), f"missing export: {name}"
