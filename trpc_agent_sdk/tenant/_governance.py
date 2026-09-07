# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tenant governance filters."""

from __future__ import annotations

from trpc_agent_sdk.abc import FilterResult
from trpc_agent_sdk.abc import FilterType
from trpc_agent_sdk.filter import BaseFilter
from trpc_agent_sdk.tools import get_tool_var

from ._tenant import BudgetConfig
from ._tenant import Tenant
from ._tenant import ToolPermissions


class ToolPermissionFilter(BaseFilter):
    """Block tool execution for denylisted or non-allowlisted tools."""

    def __init__(self, permissions: ToolPermissions):
        super().__init__()
        self._permissions = permissions
        self._type = FilterType.TOOL
        self._name = "ToolPermissionFilter"

    async def _before(self, ctx, req, rsp: FilterResult):
        tool = get_tool_var()
        if tool is None:
            return
        name = getattr(tool, "name", "")
        if name in self._permissions.denylist:
            rsp.is_continue = False
            rsp.error = PermissionError(f"tool '{name}' is denylisted")
            return
        if not self._permissions.allow_all and self._permissions.allowlist and name not in self._permissions.allowlist:
            rsp.is_continue = False
            rsp.error = PermissionError(f"tool '{name}' is not allowlisted")
            return


class BudgetFilter(BaseFilter):
    """Block when per-request token usage exceeds the configured limit."""

    def __init__(self, budgets: BudgetConfig):
        super().__init__()
        self._budgets = budgets
        self._type = FilterType.AGENT
        self._name = "BudgetFilter"
        self._token_count = 0

    def add_tokens(self, count: int) -> None:
        """Accumulate token usage (called by the outer layer from usage_metadata)."""
        self._token_count += count

    def reset(self) -> None:
        """Reset the token count (call at the start of each request)."""
        self._token_count = 0

    async def _before(self, ctx, req, rsp: FilterResult):
        limit = self._budgets.per_request_token_limit
        if limit is not None and self._token_count >= limit:
            rsp.is_continue = False
            rsp.error = RuntimeError(f"budget exceeded: token_count={self._token_count} >= limit={limit}")
            return


class TenantFilterFactory:
    """Assemble a per-tenant governance filter list from Tenant config."""

    def build_filters(self, tenant: Tenant) -> list[BaseFilter]:
        filters: list[BaseFilter] = []
        permissions = tenant.tool_permissions
        if permissions.allow_all or permissions.allowlist or permissions.denylist:
            filters.append(ToolPermissionFilter(permissions))
        if tenant.budgets.enabled:
            filters.append(BudgetFilter(tenant.budgets))
        return filters
