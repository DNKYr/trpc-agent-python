# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for governance filters."""

from unittest.mock import MagicMock

import pytest

from trpc_agent_sdk.abc import FilterResult
from trpc_agent_sdk.abc import FilterType

from trpc_agent_sdk.tenant._governance import BudgetFilter
from trpc_agent_sdk.tenant._governance import TenantFilterFactory
from trpc_agent_sdk.tenant._governance import ToolPermissionFilter
from trpc_agent_sdk.tenant._tenant import BudgetConfig
from trpc_agent_sdk.tenant._tenant import ModelConfig
from trpc_agent_sdk.tenant._tenant import Tenant
from trpc_agent_sdk.tenant._tenant import ToolPermissions


def _result():
    return FilterResult()


async def test_tool_filter_allow_all():
    flt = ToolPermissionFilter(ToolPermissions(allow_all=True))
    rsp = _result()
    await flt._before(None, None, rsp)
    assert rsp.is_continue is True


async def test_tool_filter_denylist_blocks(monkeypatch):
    flt = ToolPermissionFilter(ToolPermissions(denylist=["danger"]))
    tool = MagicMock()
    tool.name = "danger"
    monkeypatch.setattr("trpc_agent_sdk.tenant._governance.get_tool_var", lambda: tool)
    rsp = _result()
    await flt._before(None, None, rsp)
    assert rsp.is_continue is False


async def test_tool_filter_allowlist_blocks_unknown(monkeypatch):
    flt = ToolPermissionFilter(ToolPermissions(allowlist=["safe"]))
    tool = MagicMock()
    tool.name = "other"
    monkeypatch.setattr("trpc_agent_sdk.tenant._governance.get_tool_var", lambda: tool)
    rsp = _result()
    await flt._before(None, None, rsp)
    assert rsp.is_continue is False


async def test_budget_filter_blocks_over_limit():
    flt = BudgetFilter(BudgetConfig(enabled=True, per_request_token_limit=10))
    flt.add_tokens(11)
    rsp = _result()
    await flt._before(None, None, rsp)
    assert rsp.is_continue is False


async def test_budget_filter_allows_under_limit():
    flt = BudgetFilter(BudgetConfig(enabled=True, per_request_token_limit=10))
    flt.add_tokens(5)
    rsp = _result()
    await flt._before(None, None, rsp)
    assert rsp.is_continue is True


def test_filter_factory_assembles():
    tenant = Tenant(tenant_id="acme",
                    model_settings=ModelConfig(provider="openai", model_name="gpt-4o"),
                    tool_permissions=ToolPermissions(denylist=["danger"]),
                    budgets=BudgetConfig(enabled=True, per_request_token_limit=10))
    filters = TenantFilterFactory().build_filters(tenant)
    assert len(filters) == 2
    assert isinstance(filters[0], ToolPermissionFilter)
    assert isinstance(filters[1], BudgetFilter)


def test_filter_factory_empty():
    tenant = Tenant(tenant_id="acme",
                    model_settings=ModelConfig(provider="openai", model_name="gpt-4o"))
    assert TenantFilterFactory().build_filters(tenant) == []
