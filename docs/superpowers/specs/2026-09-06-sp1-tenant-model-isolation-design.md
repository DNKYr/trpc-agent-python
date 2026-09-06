# SP1 — 租户模型与隔离机制 详细设计

> 父文档：`2026-09-06-multi-tenant-node-deployment-design.md`
> 状态：待评审
> 范围：租户配置模型、`SecretStore`、`Masker`、`TenantRegistry`
> 明确**不包含**：多后端数据访问（SP2）、节点拓扑/路由与租户级 `Runner`/`Agent` 构建（SP3）、IM 适配器（SP4）、审计/遥测（SP5）、运维（SP6）

---

## 1. 目标

为 tRPC-Agent-Python SDK 提供租户模型与隔离基础设施，使每个租户拥有独立的应用配置、模型配置、工具权限、IM 通道配置、数据后端配置与审计策略，并保证这些配置、数据、密钥、日志在租户间严格隔离。

## 2. 关键决策（锁定）

1. **命名空间约定（锁定）**：SDK 层 `app_name = f"{tenant_id}:{app_config.app_name}"`。
   - 单一默认 app 的租户 → `app_name = "acme:default"`。
   - 理由：SDK 的 Session/Memory/Storage 已按 `app_name` 命名空间隔离；`app_name` 是纯字符串主键/Redis key 前缀，不被 SDK 解析，冒号安全（见 `user_key`/`session_key`/`app_state_key`/`user_state_key` 均为不透明拼接）。
   - `tenant_id` 约束：`[a-zA-Z0-9_-]{1,64}`（正则校验，避免与 SDK key 分隔符 `:` `/` 冲突）。

2. **密钥不进模型**：`Tenant` 中所有密钥字段只存 `secret_ref`（引用），实际值由 `SecretStore` 运行时解析。

3. **零 SDK 核心改动**：SP1 新增代码全部放在新包 `trpc_agent_sdk/tenant/`，不修改 `Runner`/`Session`/`Event` 等核心。

4. **配置变更可回滚**：`Tenant.version` 单调递增，`TenantRegistry` 保留最近 N 个版本快照（默认 10），支持按 version 回滚。

---

## 3. 文件结构

```
trpc_agent_sdk/tenant/
    __init__.py                  # 公开导出
    _tenant.py                   # Tenant 及子模型（AppConfig/ModelConfig/...）
    _secret_store.py             # SecretStore 抽象 + EnvSecretStore + InMemorySecretStore
    _masker.py                   # Masker 脱敏工具
    _registry.py                 # TenantRegistry + TenantSource 抽象
    _tenant_source.py            # InMemory/File/Sql TenantSource 实现
    _errors.py                   # TenantNotFoundError / SecretNotFoundError

tests/tenant/
    test_tenant.py
    test_secret_store.py
    test_masker.py
    test_registry.py
```

---

## 4. 数据模型（精确字段定义）

### 4.1 `Tenant` 及子模型

```python
from pydantic import BaseModel, Field
from typing import Any, Literal, Optional

class ModelConfig(BaseModel):
    """租户模型配置。"""
    provider: str                                    # "openai" | "anthropic" | "litellm" | ...
    model_name: str                                  # 例如 "gpt-4o"
    endpoint: Optional[str] = None                   # 自定义 base_url
    api_key_ref: Optional[str] = None                # SecretStore 引用
    timeout_seconds: float = 60.0
    temperature: Optional[float] = None
    extra: dict[str, Any] = Field(default_factory=dict)

class AppConfig(BaseModel):
    """租户应用配置。"""
    app_name: str = "default"                        # SDK app_name 后缀
    display_name: str = ""
    agent_description: str = ""
    instruction: str = ""                            # 静态指令（可被 factory 覆盖）

class ToolPermissions(BaseModel):
    """工具权限。"""
    allow_all: bool = False
    allowlist: list[str] = Field(default_factory=list)     # 允许的工具名
    denylist: list[str] = Field(default_factory=list)      # 拒绝的工具名（优先于 allowlist）
    require_confirmation: list[str] = Field(default_factory=list)  # 危险工具二次确认

class ChannelBinding(BaseModel):
    """IM 通道绑定（SP4 消费，SP1 仅建模）。"""
    binding_id: str
    channel_type: str                                # "wecom" | "telegram"
    webhook_url: str
    token_ref: Optional[str] = None
    secret_ref: Optional[str] = None
    verify_enabled: bool = True
    dedup_ttl_seconds: int = 300
    external_account_id: Optional[str] = None

class BackendSpec(BaseModel):
    """单个数据后端规格（SP2 消费）。"""
    type: str                                        # "in_memory"|"redis"|"sql"|"vector"|"object"|"mem0"
    dsn: Optional[str] = None
    secret_ref: Optional[str] = None                 # 例如 DB 密码
    options: dict[str, Any] = Field(default_factory=dict)

class DataBackendConfig(BaseModel):
    """租户数据后端配置（默认 Redis，多节点生产可用）。

    说明：SP1 仅建模；SP2 的 `StorageAdapter`/`BackendFactory` 据此实例化后端。
    生产环境中 artifact/knowledge 通常覆盖为 object/vector 后端。
    """
    session: BackendSpec = Field(default_factory=lambda: BackendSpec(type="redis"))
    memory: BackendSpec = Field(default_factory=lambda: BackendSpec(type="redis"))
    summary: BackendSpec = Field(default_factory=lambda: BackendSpec(type="redis"))
    artifact: BackendSpec = Field(default_factory=lambda: BackendSpec(type="redis"))
    knowledge: BackendSpec = Field(default_factory=lambda: BackendSpec(type="redis"))
    audit: BackendSpec = Field(default_factory=lambda: BackendSpec(type="redis"))

class AuditPolicy(BaseModel):
    """审计策略（SP5 消费）。"""
    enabled: bool = True
    sample_rate: float = 1.0                         # 0..1
    retention_days: int = 180
    include_content: bool = False                    # 是否记录消息内容（脱敏后）

class BudgetConfig(BaseModel):
    """预算限制（SP5 Filter 消费）。"""
    enabled: bool = False
    monthly_token_limit: Optional[int] = None
    monthly_cost_limit_usd: Optional[float] = None
    per_request_token_limit: Optional[int] = None

class RateLimitConfig(BaseModel):
    """频率限制（SP3/SP4 消费）。"""
    enabled: bool = False
    requests_per_minute: Optional[int] = None
    im_messages_per_minute: Optional[int] = None

class Tenant(BaseModel):
    """租户完整配置。"""
    tenant_id: str                                    # 正则 ^[a-zA-Z0-9_-]{1,64}$
    app_config: AppConfig = Field(default_factory=AppConfig)
    model_settings: ModelConfig
    tool_permissions: ToolPermissions = Field(default_factory=ToolPermissions)
    im_channels: list[ChannelBinding] = Field(default_factory=list)
    data_backends: DataBackendConfig = Field(default_factory=DataBackendConfig)
    audit_policy: AuditPolicy = Field(default_factory=AuditPolicy)
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)
    rate_limits: RateLimitConfig = Field(default_factory=RateLimitConfig)
    status: Literal["active", "disabled"] = "active"
    version: int = 1

    @property
    def sdk_app_name(self) -> str:
        """锁定：SDK 层使用的 app_name 命名空间。"""
        return f"{self.tenant_id}:{self.app_config.app_name}"
```

### 4.2 校验规则

- `tenant_id`：正则 `^[a-zA-Z0-9_-]{1,64}$`，非法即拒绝。
- `AuditPolicy.sample_rate`：`0 <= x <= 1`。
- `Tenant.version`：单调递增（`TenantRegistry.put` 时自动 `+1`）。

---

## 5. `SecretStore`

```python
from abc import ABC, abstractmethod

class SecretStore(ABC):
    """密钥存储抽象：IM token、模型 API key、DB 密码等。"""

    @abstractmethod
    async def get(self, ref: str) -> str:
        """按引用取密钥；不存在时抛 KeyError。"""

    @abstractmethod
    async def put(self, ref: str, value: str) -> None:
        """写入/更新密钥。"""

    @abstractmethod
    async def delete(self, ref: str) -> None:
        """删除密钥。"""


class EnvSecretStore(SecretStore):
    """环境变量实现。ref="api_key" -> os.environ["TENANT_SECRET_API_KEY"]。"""
    def __init__(self, prefix: str = "TENANT_SECRET_"): ...


class InMemorySecretStore(SecretStore):
    """测试/开发用内存实现。"""
    def __init__(self, secrets: dict[str, str] | None = None): ...
```

**约束（贯穿 SP1 与后续 SP）：**
- 密钥值永不进入日志、trace 属性、异常消息、审计日志。
- `SecretStore.get` 失败（缺密钥）时抛 `SecretNotFoundError(ref)`，错误消息只含 `ref` 不含值。

---

## 6. `Masker`（日志脱敏）

```python
import logging

DEFAULT_PATTERNS: dict[str, str] = {
    "api_key": r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+",
    "phone": r"\b1[3-9]\d{9}\b",
    "id_card": r"\b\d{17}[\dXx]\b",
    "bearer": r"(?i)bearer\s+[a-z0-9._-]+",
}

class Masker:
    """正则脱敏工具。"""
    def __init__(self, extra_patterns: dict[str, str] | None = None): ...
    def mask(self, text: str) -> str: ...             # 命中替换为 [REDACTED]
    def register(self, name: str, pattern: str, replacement: str = "[REDACTED]") -> None: ...


class SensitiveDataFilter(logging.Filter):
    """挂到 SDK logger，过滤日志记录中的敏感信息。"""
    def __init__(self, masker: Masker): ...
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.masker.mask(str(record.msg))
        record.args = tuple(self.masker.mask(str(a)) for a in record.args) if record.args else record.args
        return True
```

**接入方式**：SDK 日志底层是标准 `logging`（`DefaultLogger` 包装 `logging.getLogger`），因此 `SensitiveDataFilter` 可直接 `logging.getLogger("trpc_agent_sdk").addFilter(...)`。Masker 本身是纯函数工具，SP5 复用它对 trace 属性与审计内容脱敏。

---

## 7. `TenantRegistry` 与 `TenantSource`

```python
from typing import Awaitable, Callable, Optional

class TenantSource(ABC):
    """租户配置持久化来源。"""
    @abstractmethod
    async def get(self, tenant_id: str) -> Optional[Tenant]: ...
    @abstractmethod
    async def put(self, tenant: Tenant) -> None: ...
    @abstractmethod
    async def delete(self, tenant_id: str) -> None: ...
    @abstractmethod
    async def list(self) -> list[Tenant]: ...


class InMemoryTenantSource(TenantSource): ...   # 测试/开发
class FileTenantSource(TenantSource): ...       # JSON/YAML 目录，每租户一个文件
class SqlTenantSource(TenantSource): ...        # 复用 storage/_sql.py；表 schema 见 §8


class TenantRegistry:
    """带缓存 + 变更通知 + 版本回滚的租户注册表。"""

    def __init__(self, source: TenantSource, *, cache_ttl_seconds: float = 30.0,
                 history_size: int = 10): ...

    async def get(self, tenant_id: str) -> Optional[Tenant]:
        """读缓存；未命中回源 source；status=disabled 返回 None。"""

    async def put(self, tenant: Tenant) -> None:
        """写入并 version+1，触发 on_change 通知。"""

    async def delete(self, tenant_id: str) -> None: ...

    async def list(self) -> list[Tenant]: ...

    async def rollback(self, tenant_id: str, version: int) -> Optional[Tenant]:
        """回滚到指定 version 快照。

        SP1 语义：仅从进程内 `_history`（上限 `history_size`）回滚，进程重启后失效。
        持久化跨重启回滚（读取 `tenant_versions` 表）推迟到后续 SP。
        """

    def subscribe(self, cb: Callable[[str, int], Awaitable[None]]) -> None:
        """订阅变更；回调参数 (tenant_id, new_version)。SP3/SP6 用于灰度/回滚。"""
```

---

## 8. 表结构（`SqlTenantSource`）

```sql
tenants (
    tenant_id        VARCHAR(64) PRIMARY KEY,
    config           JSON NOT NULL,      -- 序列化的 Tenant（不含密钥值，仅 secret_ref）
    status           VARCHAR(16) NOT NULL,
    version          INT NOT NULL,
    updated_at       TIMESTAMP NOT NULL
)

tenant_versions (
    tenant_id        VARCHAR(64),
    version          INT,
    config           JSON NOT NULL,      -- 历史快照（持久化供后续跨重启回滚）
    PRIMARY KEY (tenant_id, version)
)
```

密钥不落 `tenants` 表，统一存 `SecretStore`。

> 说明：`tenant_versions` 是持久化快照，SP1 的 `TenantRegistry.rollback` 仅读进程内 `_history`（跨重启回滚留待后续 SP）；`SqlTenantSource.put` 在更新时自动 `version+1`（与 `TenantRegistry.put` 语义一致，二者读同一持久化版本故收敛）。

---

## 9. 测试策略

| 测试文件 | 覆盖 |
|----------|------|
| `test_tenant.py` | 模型校验：tenant_id 正则、sample_rate 范围、`sdk_app_name` 组合、version 单调、DataBackendConfig 默认 redis |
| `test_secret_store.py` | Env/InMemory 的 get/put/delete、缺密钥抛 `SecretNotFoundError` 且消息不含值 |
| `test_masker.py` | 各默认 pattern 脱敏、自定义 pattern、`SensitiveDataFilter` 作用于 LogRecord |
| `test_registry.py` | 缓存命中/失效、version 递增、订阅回调、回滚到历史版本、disabled 租户返回 None |

---

## 10. 已确认决策（评审结论）

1. **包名**：`tenant`（`trpc_agent_sdk/tenant/`）。
2. **`tenant_id` 长度上限**：64 字符（正则 `^[a-zA-Z0-9_-]{1,64}$`）。
3. **默认后端**：`DataBackendConfig` 默认全 `redis`（生产多节点可用）。
4. **`Runner`/`Agent` 构建**：推迟到 SP3（SP1 纯建模 + 基础组件，不构建 Runner）。
5. **字段命名**：`Tenant.model_config` 重命名为 `model_settings` —— pydantic 2.13 硬保留 `model_config` 作为模型配置属性，无法用作字段名。
