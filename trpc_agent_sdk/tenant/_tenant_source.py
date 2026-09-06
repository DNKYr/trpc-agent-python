# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""In-memory and file-backed tenant source implementations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from ._registry import TenantSource
from ._tenant import Tenant
from ._tenant import _TENANT_ID_RE


class InMemoryTenantSource(TenantSource):
    """In-memory tenant source for development and testing."""

    def __init__(self):
        self._tenants: dict[str, Tenant] = {}

    async def get(self, tenant_id: str) -> Optional[Tenant]:
        return self._tenants.get(tenant_id)

    async def put(self, tenant: Tenant) -> None:
        self._tenants[tenant.tenant_id] = tenant

    async def delete(self, tenant_id: str) -> None:
        self._tenants.pop(tenant_id, None)

    async def list(self) -> list[Tenant]:
        return list(self._tenants.values())


class FileTenantSource(TenantSource):
    """File-backed tenant source; one JSON file per tenant."""

    def __init__(self, directory: str, extension: str = ".json"):
        self._directory = directory
        self._extension = extension

    def _path(self, tenant_id: str) -> Path:
        if not _TENANT_ID_RE.match(tenant_id):
            raise ValueError(f"invalid tenant_id: {tenant_id}")
        return Path(self._directory) / f"{tenant_id}{self._extension}"

    async def get(self, tenant_id: str) -> Optional[Tenant]:
        path = self._path(tenant_id)
        if not path.exists():
            return None
        return Tenant.model_validate(json.loads(path.read_text(encoding="utf-8")))

    async def put(self, tenant: Tenant) -> None:
        Path(self._directory).mkdir(parents=True, exist_ok=True)
        self._path(tenant.tenant_id).write_text(tenant.model_dump_json(), encoding="utf-8")

    async def delete(self, tenant_id: str) -> None:
        path = self._path(tenant_id)
        if path.exists():
            path.unlink()

    async def list(self) -> list[Tenant]:
        directory = Path(self._directory)
        if not directory.exists():
            return []
        tenants = []
        for path in directory.iterdir():
            if path.suffix == self._extension:
                tenants.append(Tenant.model_validate(json.loads(path.read_text(encoding="utf-8"))))
        return tenants
