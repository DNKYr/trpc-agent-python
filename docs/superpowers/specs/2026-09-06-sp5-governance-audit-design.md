# SP5 — 治理与审计（Filter 链 + 审计日志）详细设计

> 父文档：`2026-09-06-multi-tenant-node-deployment-design.md`
> 状态：待评审
> 范围：`AuditEvent`/`AuditSink`/`AuditLogger`（审计日志）+ 治理 Filter（`ToolPermissionFilter`/`BudgetFilter`）+ `TenantFilterFactory`（从 `Tenant` 配置组装）
> 明确**不包含**：新建 OTel 指标/tracing（SDK 已具备 `gen_ai.*` 指标与 span，仅补 `tenant_id` 维度辅助）；IM 投递成功率等新指标（后续）；真机联调（收尾阶段）

---

## 1. 目标

在 SP1（`ToolPermissions`/`BudgetConfig`/`AuditPolicy`/`Masker`）与 SP3（`TenantAgentFactory`）之上，补齐治理与审计：

- **审计日志**：以 `AuditEvent` 记录租户操作（含 `tenant_id/channel/user_id/session_id/agent_name/tool_name/decision/latency/error_type/cost/trace_id`），经 `AuditSink` 落库，`AuditLogger` 脱敏后写入。
- **治理 Filter 链**：`ToolPermissionFilter`（工具白名单/黑名单运行时拦截）、`BudgetFilter`（token/成本预算限制）；`TenantFilterFactory` 从 `Tenant` 配置组装 per-tenant Filter 列表。

## 2. 关键决策（锁定）

1. **复用 SDK Filter 体系**：治理 Filter 是 `BaseFilter` 子类（`FilterType.TOOL`/`FilterType.AGENT`），经 SP3 的 `TenantAgentFactory` 在构建 Agent 时通过 `filters=[...]` 注入（不改 SDK 核心）。

2. **审计落库走 `AuditSink` 抽象**：`AuditSink`（ABC，`write`/`close`）+ `InMemoryAuditSink`（测试/开发）；生产 SQL/Redis 实现后续接（接口一致，对应 SP2 `DataBackendConfig.audit` 槽位）。

3. **脱敏**：`AuditLogger` 内置 `Masker`（SP1），写入前对 `AuditEvent` 的文本字段脱敏，密钥/敏感信息不落审计。

4. **`decision` 语义**：`allow`（放行）/`deny`（拦截）/`confirm`（需二次确认）；由治理 Filter 的拦截行为体现，审计 `decision` 字段由外层（Gateway）统一写入（Filter 与审计解耦）。

5. **包位置**：`trpc_agent_sdk/tenant/`（租户级治理，消费 `Tenant`/`ToolPermissions`/`BudgetConfig`/`AuditPolicy`），新增 `_audit.py` + `_governance.py`。

---

## 3. 文件结构

```
trpc_agent_sdk/tenant/
    _audit.py         # AuditEvent / AuditSink(ABC) / InMemoryAuditSink / AuditLogger
    _governance.py    # ToolPermissionFilter / BudgetFilter / TenantFilterFactory

tests/tenant/
    test_audit.py
    test_governance.py
```

> 复用 SP1 的 `Tenant`/`ToolPermissions`/`BudgetConfig`/`Masker`，SDK 的 `BaseFilter`/`FilterResult`/`FilterType`。

---

## 4. 审计日志（`_audit.py`）

```python
class AuditEvent(BaseModel):
    tenant_id: str
    channel: str = ""            # "wecom" | "telegram" | ""（直连）
    user_id: str = ""
    session_id: str = ""
    agent_name: str = ""
    tool_name: str = ""
    decision: str = ""           # allow | deny | confirm
    latency: float = 0.0
    error_type: str = ""
    cost: float = 0.0            # 成本（USD）
    trace_id: str = ""
    timestamp: float = Field(default_factory=time.time)


class AuditSink(ABC):
    @abstractmethod
    async def write(self, event: AuditEvent) -> None:
        """写入一条审计事件。"""

    @abstractmethod
    async def close(self) -> None:
        """释放资源。"""


class InMemoryAuditSink(AuditSink):
    """测试/开发用内存 sink，可 query。"""
    def __init__(self, max_entries: int = 10000): ...
    async def write(self, event): ...     # 追加（超限淘汰最旧）
    async def query(self, tenant_id: str | None = None) -> list[AuditEvent]: ...
    async def close(self): ...


class AuditLogger:
    """脱敏后写入 sink。"""
    def __init__(self, sink: AuditSink, masker: Masker | None = None): ...
    async def log(self, event: AuditEvent) -> None:
        """对文本字段脱敏（masker 提供时），再 sink.write。"""
```

---

## 5. 治理 Filter（`_governance.py`）

```python
class ToolPermissionFilter(BaseFilter):
    """TOOL 类型 Filter：按 ToolPermissions 白名单/黑名单在工具执行前拦截。"""
    def __init__(self, permissions: ToolPermissions): ...
    async def _before(self, ctx, req, rsp): ...
    # 不在白名单/在黑名单 → rsp.is_continue=False（仅拦截，不落审计）


class BudgetFilter(BaseFilter):
    """AGENT 类型 Filter：按 BudgetConfig 限制 token/成本。"""
    def __init__(self, budgets: BudgetConfig): ...
    async def _before(self, ctx, req, rsp): ...
    # 超预算 → rsp.is_continue=False（仅拦截，不落审计）


class TenantFilterFactory:
    """从 Tenant 配置组装 per-tenant 治理 Filter 列表。"""
    def build_filters(self, tenant: Tenant) -> list[BaseFilter]:
        filters = []
        if tenant.tool_permissions.allow_all or tenant.tool_permissions.allowlist or tenant.tool_permissions.denylist:
            filters.append(ToolPermissionFilter(tenant.tool_permissions))
        if tenant.budgets.enabled:
            filters.append(BudgetFilter(tenant.budgets))
        return filters
```

**决策（解耦）**：治理 Filter 只负责「拦截」（`rsp.is_continue=False`），**不注入/调用 `AuditLogger`**。`decision`（allow/deny）由外层（Gateway）在 Filter 返回后统一读取并写入审计——Filter 与审计日志完全解耦。

**集成（SP3 扩展）**：`TenantAgentFactory.build_runner` 增加可选 `filter_factory: TenantFilterFactory | None`，构建 `LlmAgent` 时 `filters=filter_factory.build_filters(tenant)`（若有）。

---

## 6. 测试策略

| 测试文件 | 覆盖 |
|----------|------|
| `test_audit.py` | `AuditEvent` 字段默认值；`InMemoryAuditSink` write/query/超限淘汰/close；`AuditLogger` 脱敏后写入（敏感文本被 `Masker` 替换）、无 masker 时原样写入 |
| `test_governance.py` | `ToolPermissionFilter`：白名单放行/黑名单拦截/allow_all；`BudgetFilter`：超预算拦截/未启用不拦截；`TenantFilterFactory.build_filters` 按配置组装（空配置→空列表） |

---

## 7. 已确认决策（评审结论）

1. **范围**：审计日志 + 治理 Filter 链；指标/tracing 仅补 `tenant_id` 维度（SDK 已具备 `gen_ai.*`）。
2. **包位置**：`trpc_agent_sdk/tenant/`（`_audit.py` + `_governance.py`）。
3. **落库**：`AuditSink` 抽象 + `InMemoryAuditSink`（生产实现后续）。
4. **集成**：SP3 `TenantAgentFactory.build_runner` 增加可选 `filter_factory` 注入。
