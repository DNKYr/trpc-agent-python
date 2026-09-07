# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for the audit log."""

from trpc_agent_sdk.tenant._audit import AuditEvent
from trpc_agent_sdk.tenant._audit import AuditLogger
from trpc_agent_sdk.tenant._audit import InMemoryAuditSink
from trpc_agent_sdk.tenant._masker import Masker


async def test_in_memory_sink_write_and_query():
    sink = InMemoryAuditSink()
    await sink.write(AuditEvent(tenant_id="acme", decision="allow"))
    await sink.write(AuditEvent(tenant_id="beta", decision="deny"))
    assert len(await sink.query()) == 2
    assert len(await sink.query(tenant_id="acme")) == 1
    assert (await sink.query(tenant_id="acme"))[0].tenant_id == "acme"


async def test_in_memory_sink_evicts_oldest():
    sink = InMemoryAuditSink(max_entries=2)
    await sink.write(AuditEvent(tenant_id="a"))
    await sink.write(AuditEvent(tenant_id="b"))
    await sink.write(AuditEvent(tenant_id="c"))
    events = await sink.query()
    assert [e.tenant_id for e in events] == ["b", "c"]


async def test_audit_logger_masks_string_fields():
    masker = Masker(extra_patterns={"x": r"secret-\d+"})
    sink = InMemoryAuditSink()
    logger = AuditLogger(sink, masker=masker)
    await logger.log(AuditEvent(tenant_id="acme", agent_name="secret-42"))
    written = (await sink.query())[0]
    assert written.agent_name == "[REDACTED]"


async def test_audit_logger_without_masker():
    sink = InMemoryAuditSink()
    logger = AuditLogger(sink)
    await logger.log(AuditEvent(tenant_id="acme", user_id="u1"))
    assert (await sink.query())[0].user_id == "u1"


def test_public_exports_governance():
    import trpc_agent_sdk.tenant as tenant_pkg
    for name in ("AuditEvent", "AuditSink", "InMemoryAuditSink", "AuditLogger",
                 "ToolPermissionFilter", "BudgetFilter", "TenantFilterFactory"):
        assert hasattr(tenant_pkg, name), f"missing export: {name}"
