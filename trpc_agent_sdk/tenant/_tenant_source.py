# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""In-memory and file-backed tenant source implementations."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import Integer, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from trpc_agent_sdk.storage import DEFAULT_MAX_KEY_LENGTH
from trpc_agent_sdk.storage import DEFAULT_MAX_VARCHAR_LENGTH
from trpc_agent_sdk.storage import DynamicJSON
from trpc_agent_sdk.storage import PreciseTimestamp
from trpc_agent_sdk.storage import SqlCondition
from trpc_agent_sdk.storage import SqlKey
from trpc_agent_sdk.storage import SqlStorage
from trpc_agent_sdk.storage import UTF8MB4String

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


class TenantStorageBase(DeclarativeBase):
    """Base for SqlTenantSource tables only."""


class StorageTenant(TenantStorageBase):
    """A tenant row stored in SQL."""

    __tablename__ = "tenants"

    tenant_id: Mapped[str] = mapped_column(UTF8MB4String(DEFAULT_MAX_KEY_LENGTH), primary_key=True)
    config: Mapped[dict[str, Any]] = mapped_column(DynamicJSON, default=dict)
    status: Mapped[str] = mapped_column(UTF8MB4String(DEFAULT_MAX_VARCHAR_LENGTH))
    version: Mapped[int] = mapped_column(Integer)
    update_time: Mapped[datetime] = mapped_column(PreciseTimestamp, default=func.now(), onupdate=func.now())

    def to_tenant(self) -> Tenant:
        return Tenant.model_validate(self.config)

    @classmethod
    def from_tenant(cls, tenant: Tenant) -> "StorageTenant":
        return cls(
            tenant_id=tenant.tenant_id,
            config=tenant.model_dump(mode="json"),
            status=tenant.status,
            version=tenant.version,
        )


class StorageTenantVersion(TenantStorageBase):
    """An immutable snapshot of a tenant config version, for rollback."""

    __tablename__ = "tenant_versions"

    tenant_id: Mapped[str] = mapped_column(UTF8MB4String(DEFAULT_MAX_KEY_LENGTH), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    config: Mapped[dict[str, Any]] = mapped_column(DynamicJSON, default=dict)


class SqlTenantSource(TenantSource):
    """SQL-backed tenant source."""

    def __init__(self, db_url: str, is_async: bool = False, **kwargs: Any):
        self._sql_storage = SqlStorage(is_async=is_async, db_url=db_url, metadata=TenantStorageBase.metadata, **kwargs)

    async def get(self, tenant_id: str) -> Optional[Tenant]:
        async with self._sql_storage.create_db_session() as sql_session:
            key = SqlKey(key=(tenant_id, ), storage_cls=StorageTenant)
            row: Optional[StorageTenant] = await self._sql_storage.get(sql_session, key)
            if row is None:
                return None
            return row.to_tenant()

    async def put(self, tenant: Tenant) -> None:
        async with self._sql_storage.create_db_session() as sql_session:
            key = SqlKey(key=(tenant.tenant_id, ), storage_cls=StorageTenant)
            existing: Optional[StorageTenant] = await self._sql_storage.get(sql_session, key)
            if existing is None:
                version = tenant.version or 1
            else:
                version = existing.version + 1
            stored = tenant.model_copy(update={"version": version})
            if existing is None:
                await self._sql_storage.add(sql_session, StorageTenant.from_tenant(stored))
            else:
                existing.config = stored.model_dump(mode="json")
                existing.status = stored.status
                existing.version = version
            version_row = StorageTenantVersion(
                tenant_id=stored.tenant_id,
                version=version,
                config=stored.model_dump(mode="json"),
            )
            await self._sql_storage.add(sql_session, version_row)
            await self._sql_storage.commit(sql_session)

    async def delete(self, tenant_id: str) -> None:
        async with self._sql_storage.create_db_session() as sql_session:
            conditions = SqlCondition(filters=[StorageTenant.tenant_id == tenant_id])
            await self._sql_storage.delete(sql_session, SqlKey(key=(tenant_id, ), storage_cls=StorageTenant),
                                           conditions)
            version_conditions = SqlCondition(filters=[StorageTenantVersion.tenant_id == tenant_id])
            await self._sql_storage.delete(sql_session, SqlKey(key=(tenant_id, ), storage_cls=StorageTenantVersion),
                                           version_conditions)
            await self._sql_storage.commit(sql_session)

    async def list(self) -> list[Tenant]:
        async with self._sql_storage.create_db_session() as sql_session:
            key = SqlKey(key=tuple(), storage_cls=StorageTenant)
            rows = await self._sql_storage.query(sql_session, key, SqlCondition())
            return [row.to_tenant() for row in rows]

    async def close(self) -> None:
        await self._sql_storage.close()
