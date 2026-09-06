# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tenant configuration models."""

from __future__ import annotations

import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

_TENANT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class ModelConfig(BaseModel):
    """Per-tenant model configuration."""

    provider: str
    model_name: str
    endpoint: Optional[str] = None
    api_key_ref: Optional[str] = None
    timeout_seconds: float = 60.0
    temperature: Optional[float] = None
    extra: dict[str, Any] = Field(default_factory=dict)


class AppConfig(BaseModel):
    """Per-tenant application configuration."""

    app_name: str = "default"
    display_name: str = ""
    agent_description: str = ""
    instruction: str = ""


class ToolPermissions(BaseModel):
    """Per-tenant tool permissions."""

    allow_all: bool = False
    allowlist: list[str] = Field(default_factory=list)
    denylist: list[str] = Field(default_factory=list)
    require_confirmation: list[str] = Field(default_factory=list)


class ChannelBinding(BaseModel):
    """IM channel binding (consumed by SP4)."""

    binding_id: str
    channel_type: str
    webhook_url: str
    token_ref: Optional[str] = None
    secret_ref: Optional[str] = None
    verify_enabled: bool = True
    dedup_ttl_seconds: int = 300
    external_account_id: Optional[str] = None


class BackendSpec(BaseModel):
    """A single data backend specification (consumed by SP2)."""

    type: str
    dsn: Optional[str] = None
    secret_ref: Optional[str] = None
    options: dict[str, Any] = Field(default_factory=dict)


class DataBackendConfig(BaseModel):
    """Per-tenant data backend configuration (defaults to Redis)."""

    session: BackendSpec = Field(default_factory=lambda: BackendSpec(type="redis"))
    memory: BackendSpec = Field(default_factory=lambda: BackendSpec(type="redis"))
    summary: BackendSpec = Field(default_factory=lambda: BackendSpec(type="redis"))
    artifact: BackendSpec = Field(default_factory=lambda: BackendSpec(type="redis"))
    knowledge: BackendSpec = Field(default_factory=lambda: BackendSpec(type="redis"))
    audit: BackendSpec = Field(default_factory=lambda: BackendSpec(type="redis"))


class AuditPolicy(BaseModel):
    """Per-tenant audit policy (consumed by SP5)."""

    enabled: bool = True
    sample_rate: float = 1.0
    retention_days: int = 180
    include_content: bool = False

    @field_validator("sample_rate")
    @classmethod
    def _validate_sample_rate(cls, value: float) -> float:
        if not (0.0 <= value <= 1.0):
            raise ValueError("sample_rate must be between 0 and 1")
        return value


class BudgetConfig(BaseModel):
    """Per-tenant budget limits (consumed by SP5 filter)."""

    enabled: bool = False
    monthly_token_limit: Optional[int] = None
    monthly_cost_limit_usd: Optional[float] = None
    per_request_token_limit: Optional[int] = None


class RateLimitConfig(BaseModel):
    """Per-tenant rate limits (consumed by SP3/SP4)."""

    enabled: bool = False
    requests_per_minute: Optional[int] = None
    im_messages_per_minute: Optional[int] = None


class _ModelConfigDescriptor:
    """Expose the ``model_config`` field on instances while keeping pydantic's config on the class.

    Pydantic reserves the ``model_config`` attribute name for model configuration and will never
    treat it as a field (see ``pydantic._internal._fields``). To honor the ``Tenant.model_config``
    interface, the field is declared as ``model_config_`` with a ``model_config`` alias, and this
    descriptor routes instance read access of ``model_config`` to the field while preserving
    class-level access to pydantic's config dict.
    """

    def __init__(self, config: ConfigDict):
        self._config = config

    def __get__(self, instance: Any, owner: type) -> Any:
        if instance is None:
            return self._config
        return instance.model_config_


class Tenant(BaseModel):
    """Full tenant configuration."""

    model_config = ConfigDict(populate_by_name=True)

    tenant_id: str
    app_config: AppConfig = Field(default_factory=AppConfig)
    model_config_: ModelConfig = Field(alias="model_config")
    tool_permissions: ToolPermissions = Field(default_factory=ToolPermissions)
    im_channels: list[ChannelBinding] = Field(default_factory=list)
    data_backends: DataBackendConfig = Field(default_factory=DataBackendConfig)
    audit_policy: AuditPolicy = Field(default_factory=AuditPolicy)
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)
    rate_limits: RateLimitConfig = Field(default_factory=RateLimitConfig)
    status: Literal["active", "disabled"] = "active"
    version: int = 1

    @field_validator("tenant_id")
    @classmethod
    def _validate_tenant_id(cls, value: str) -> str:
        if not _TENANT_ID_RE.match(value):
            raise ValueError("tenant_id must match ^[a-zA-Z0-9_-]{1,64}$")
        return value

    @property
    def sdk_app_name(self) -> str:
        """The app_name namespace used at the SDK layer."""
        return f"{self.tenant_id}:{self.app_config.app_name}"


Tenant.model_config = _ModelConfigDescriptor(Tenant.model_config)
