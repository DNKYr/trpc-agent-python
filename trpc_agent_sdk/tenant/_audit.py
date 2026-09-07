# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tenant audit log."""

from __future__ import annotations

import time
from abc import ABC
from abc import abstractmethod
from typing import Optional

from pydantic import BaseModel
from pydantic import Field

from ._masker import Masker


class AuditEvent(BaseModel):
    """A single audit record."""

    tenant_id: str
    channel: str = ""
    user_id: str = ""
    session_id: str = ""
    agent_name: str = ""
    tool_name: str = ""
    decision: str = ""
    latency: float = 0.0
    error_type: str = ""
    cost: float = 0.0
    trace_id: str = ""
    timestamp: float = Field(default_factory=time.time)


class AuditSink(ABC):
    """Abstract audit log sink."""

    @abstractmethod
    async def write(self, event: AuditEvent) -> None:
        """Write one audit event."""

    @abstractmethod
    async def close(self) -> None:
        """Release resources."""


class InMemoryAuditSink(AuditSink):
    """In-memory audit sink for development and testing."""

    def __init__(self, max_entries: int = 10000):
        self._max_entries = max(1, int(max_entries))
        self._events: list[AuditEvent] = []

    async def write(self, event: AuditEvent) -> None:
        self._events.append(event)
        if len(self._events) > self._max_entries:
            self._events = self._events[-self._max_entries:]

    async def query(self, tenant_id: Optional[str] = None) -> list[AuditEvent]:
        if tenant_id is None:
            return list(self._events)
        return [e for e in self._events if e.tenant_id == tenant_id]

    async def close(self) -> None:
        self._events.clear()


_MASKABLE_FIELDS = ("agent_name", "tool_name")


class AuditLogger:
    """Mask content-bearing fields and write audit events to a sink."""

    def __init__(self, sink: AuditSink, masker: Optional[Masker] = None):
        self._sink = sink
        self._masker = masker

    async def log(self, event: AuditEvent) -> None:
        if self._masker is not None:
            updates = {}
            for field in _MASKABLE_FIELDS:
                value = getattr(event, field, None)
                if isinstance(value, str) and value:
                    updates[field] = self._masker.mask(value)
            if updates:
                event = event.model_copy(update=updates)
        await self._sink.write(event)
