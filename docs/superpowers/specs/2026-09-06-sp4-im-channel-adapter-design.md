# SP4 — IM Channel Adapter（抽象 + 协议逻辑）详细设计

> 父文档：`2026-09-06-multi-tenant-node-deployment-design.md`
> 状态：待评审
> 范围：`ChannelAdapter` 抽象 + 归一化消息模型 + 入站/出站转换 + 租户绑定/验签/去重/用户映射/session 规则（WeCom + Telegram 两通道，可无网测试）
> 明确**不包含**：真实 IM SDK 收发（python-telegram-bot / wechatpy / nanobot）与网络/webhook 服务器接线（后续 SP）；卡片消息渲染（后续）；工具/审计（SP5）

---

## 1. 目标

为租户提供 IM 通道接入的协议逻辑层：外部 IM 消息 → tRPC-Agent 用户输入（`Content`），Agent `Event` → IM 回复/流式消息，并完成 webhook 验签、消息去重、用户身份映射与 session_id 生成。

## 2. 关键决策（锁定）

1. **归一化消息模型**：定义 `InboundMessage`/`OutboundMessage`，与具体 IM 平台解耦（参考现有 `openclaw` 用 nanobot 的 `InboundMessage`/`OutboundMessage` 思路，但自建、不依赖 nanobot）。

2. **session_id 规则**（父文档 §7.3）：
   - 单聊：`chat_{channel}_{peer_id}`
   - 群聊：`group_{channel}_{group_id}`（群内共用 session，按 `author`=sender 区分用户）
   - 跨租户隔离：靠 SDK 的 `app_name=tenant.sdk_app_name` 命名空间，session_id 本身不带 tenant 前缀。

3. **user_id 映射**：SP4 默认 `user_id = sender_id`（外部 ID 直通）；映射表（外部→内部）留后续 SP。

4. **去重**：`(channel, chat_id, external_msg_id)` 幂等（Telegram `message_id` 是 per-chat 计数器，须含 `chat_id` 才全局唯一），`InMemoryDedupStore` 带 TTL（默认取 `ChannelBinding.dedup_ttl_seconds`，缺省 300s）。生产可用 Redis 替换（接口一致）。

5. **验签算法**：
   - WeCom：GET 回调验证 `msg_signature = sha1(sort(token, timestamp, nonce, echostr))`。
   - Telegram：校验 `X-Telegram-Bot-Api-Secret-Token` 头（与配置 secret 常量时间比较）。
   - 密钥从 `ChannelBinding.token_ref`/`secret_ref` 经 `SecretStore` 解析。

6. **webhook → 租户绑定**：`ChannelRouter` 按 `(channel_type, 平台标识)` 定位 `ChannelBinding` → `tenant_id`。

7. **出站渲染**：`Event` → `OutboundMessage`，文本拼接（`Event.get_text()`）；流式 `partial=True` 累积、`partial=False` 为 final 分片。卡片/图片/文件渲染留后续。

---

## 3. 文件结构

```
trpc_agent_sdk/channels/
    __init__.py         # 公开导出
    _errors.py          # AuthenticationError / DuplicateMessageError
    _messages.py        # InboundMessage / OutboundMessage + 转换辅助
    _base.py            # ChannelAdapter ABC
    _wecom.py           # WecomAdapter
    _telegram.py        # TelegramAdapter
    _router.py          # ChannelRouter + InMemoryDedupStore

tests/channels/
    test_messages.py
    test_wecom.py
    test_telegram.py
    test_router.py
```

> 复用 SP1 `tenant` 包的 `ChannelBinding`/`SecretStore`/`TenantRegistry`/`TenantNotFoundError`（`channels` → `tenant` 单向依赖）；`Content`/`Part`/`Event` 来自 SDK。

---

## 4. 消息模型与转换（`_messages.py`）

```python
class InboundMessage(BaseModel):
    channel: str                         # "wecom" | "telegram"
    external_msg_id: str                 # 平台消息 ID（去重键）
    chat_type: Literal["single", "group"]
    chat_id: str                         # 群 ID 或单聊对方 ID
    sender_id: str                       # 外部用户 ID
    text: str
    timestamp: float

class OutboundMessage(BaseModel):
    channel: str
    chat_id: str
    text: str
    is_final: bool = True                # 流式分片标记

def content_from_text(text: str) -> Content:
    return Content(parts=[Part(text=text)]) if text else Content(parts=[Part(text="")])

def event_to_text(event: Event) -> str:
    return event.get_text()              # 拼接所有 text part
```

---

## 5. `ChannelAdapter` ABC（`_base.py`）

```python
class ChannelAdapter(ABC):
    channel_type: str

    @abstractmethod
    def verify_signature(self, *, token: str, secret: str,
                         signature: str, timestamp: str, nonce: str, payload: str) -> bool:
        """校验 webhook 回调签名。token/secret 为绑定解析后的值。"""

    @abstractmethod
    def parse_inbound(self, raw: dict) -> InboundMessage:
        """平台原始消息 → InboundMessage。"""

    def render_outbound(self, chat_id: str, event: Event) -> list[OutboundMessage]:
        """通用文本渲染（可被子类覆盖，如卡片消息）。"""

    def session_id(self, msg: InboundMessage) -> str:
        """通用规则：chat_{channel}_{chat_id} / group_{channel}_{chat_id}。"""

    def user_id(self, msg: InboundMessage) -> str:
        """SP4 直通 sender_id。"""
```

> `verify_signature` / `parse_inbound` 为平台特定（抽象）；`render_outbound` / `session_id` / `user_id` 提供通用实现（仅依赖 `self.channel_type`，WeCom/Telegram 共用）。

---

## 6. 具体适配器

### 6.1 `WecomAdapter`（`_wecom.py`）

- `verify_signature`：`sha1("".join(sorted([token, timestamp, nonce, payload]))).hexdigest() == signature`。
- `parse_inbound`：从企微回调 JSON 提取 `msg_id`、`chat_id`、`from_user`、`text`；单聊/群聊按 `roomid` 是否存在判定。
- `session_id`：单聊 `chat_wecom_{chat_id}`，群聊 `group_wecom_{chat_id}`。
- `render_outbound`：`event_to_text` → 文本 `OutboundMessage`（流式 final 标记）。

### 6.2 `TelegramAdapter`（`_telegram.py`）

- `verify_signature`：`hmac.compare_digest(secret, signature)`（secret token 常量时间比较）。
- `parse_inbound`：从 Telegram Update JSON 提取 `message_id`、`chat.id`、`chat.type`（private/group）、`from.id`、`text`。
- `session_id`：`chat_telegram_{chat_id}` / `group_telegram_{chat_id}`。
- `render_outbound`：同 WeCom。

---

## 7. `ChannelRouter`（`_router.py`）

```python
class InMemoryDedupStore:
    """(channel, chat_id, external_msg_id) 去重，带 TTL。"""
    def __init__(self, default_ttl_seconds: float = 300.0): ...
    def seen(self, channel: str, chat_id: str, msg_id: str,
             ttl_seconds: float | None = None) -> bool:
        """首次返回 False（记录），重复返回 True。"""


class RoutedMessage(BaseModel):
    """webhook 经绑定、去重、映射后的归一化结果。"""
    tenant_id: str
    user_id: str
    session_id: str
    content: Content
    inbound: InboundMessage


class ChannelRouter:
    """webhook → 租户解析 + 验签 + 去重 + session/user 映射。"""

    def __init__(self, registry: TenantRegistry, secret_store: SecretStore,
                 adapters: dict[str, ChannelAdapter], dedup: InMemoryDedupStore): ...

    async def route(self, *, channel_type: str, platform_id: str, raw: dict,
                    signature: str, timestamp: str, nonce: str) -> RoutedMessage:
        """
        1. 按 (channel_type, platform_id) 定位 ChannelBinding → tenant
        2. adapter = adapters[channel_type]
        3. 解析 token/secret（SecretStore）；若 binding.verify_enabled 则 adapter.verify_signature，失败抛 AuthenticationError
        4. inbound = adapter.parse_inbound(raw)
        5. dedup.seen(channel_type, inbound.chat_id, inbound.external_msg_id, ttl_seconds=binding.dedup_ttl_seconds)，重复抛 DuplicateMessageError
        6. user_id = adapter.user_id(inbound)；session_id = adapter.session_id(inbound)
        7. content = content_from_text(inbound.text)
        8. return RoutedMessage(...)
        """
```

---

## 8. 测试策略

| 测试文件 | 覆盖 |
|----------|------|
| `test_messages.py` | `content_from_text`、`event_to_text`（含多 part 拼接） |
| `test_wecom.py` | `verify_signature`（已知 token/timestamp/nonce/echostr 的 sha1 断言）；`parse_inbound`（单聊/群聊）；`session_id` 规则；`render_outbound` |
| `test_telegram.py` | `verify_signature`（secret 匹配/不匹配）；`parse_inbound`；`session_id`；`render_outbound` |
| `test_router.py` | 绑定→租户解析；验签失败抛错；去重（首次通过、重复拒绝）；session_id/user_id 正确；TTL 过期后重新放行 |

> 全部无网测试（验签为纯算法，消息为 dict 构造）。

---

## 9. 已确认决策（评审结论）

1. **深度**：抽象 + 协议逻辑（验签/去重/映射/session 规则），真实 SDK 收发与网络接线留后续。
2. **通道**：WeCom + Telegram 两个具体适配器。
3. **依赖**：不引入 nanobot/python-telegram-bot/wechatpy，纯标准库（`hashlib`/`hmac`）+ pydantic。
4. **去重**：进程内 `InMemoryDedupStore`（TTL），接口可替换为 Redis；重复消息抛 `DuplicateMessageError`。
5. **验签失败**：抛 `AuthenticationError`。
6. **包位置**：顶层 `trpc_agent_sdk/channels/`（非 `tenant/` 子包），单向依赖 `tenant`。
7. **真机联调**：本 SP 仅协议逻辑，**不做真机联调**；真实 WeCom/Telegram webhook 联调推迟到网络/SDK 接线完成后（收尾阶段）。
