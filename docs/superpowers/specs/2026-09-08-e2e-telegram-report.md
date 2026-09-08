# Telegram 真机联调记录

> 日期：2026-09-08
> 状态：完成
> 关联：`examples/telegram_e2e/`（harness）、`deploy/e2e/`（部署脚本）、SP4 `trpc_agent_sdk/channels`

---

## 1. 目标

用真实 Telegram bot 验证多租户 SDK 的端到端链路：IM 收发 → `ChannelRouter`（去重/映射）→ `RunnerPool`（执行）→ 模型回复 → 分片投递。

## 2. 部署拓扑

| 组件 | 说明 |
|------|------|
| 服务器 | 阿里云境外地域（`129.226.144.137`，Ubuntu 22.04，Python 3.12） |
| 通道 | Telegram **long polling**（`getUpdates`，无需公网域名/HTTPS） |
| 模型 | DeepSeek `deepseek-v4-flash`（OpenAI 兼容，SDK `OpenAIModel` + `DeepSeekAdapter`） |
| 数据后端 | InMemory（Session/Memory/Artifact，联调够用） |
| 常驻 | systemd `telegram-e2e.service` |
| 密钥 | `.env`（`load_dotenv`），不入库（`.gitignore` 忽略 `.env`） |

**链路**：`getUpdates` → `TelegramAdapter.parse_inbound` → `ChannelRouter.route`（`verify_enabled=False`，去重 `(channel,chat_id,msg_id)` + session/user 映射）→ `RunnerPool.get_runner` → `Runner.run_async` → `event_reply_text`（过滤 thought）→ `split_text`（4096 上限分片）→ `sendMessage`。

## 3. 验证结果

- ✅ 模型连通：`deepseek-v4-flash` 正常回复。
- ✅ 1-on-1 单聊：`session_id = chat_telegram_{chat_id}`，多轮对话正常。
- ✅ 日志链路：`message user=... session=... chat=...` → `HTTP Request POST /chat/completions 200` → `replied chat=... (N chars)`。
- ⚠️ 群聊、图片/文件、多租户路由未在此轮验证（后续）。

## 4. 发现的 bug 与修复

| # | Bug | 根因 | 修复 |
|---|-----|------|------|
| 1 | 超长回复丢失（10349 字符） | Telegram `sendMessage` 单条 4096 字符上限，超限报错但未检查响应 | `split_text()` 按 4000 字符分片 + `raise_for_status()` |
| 2 | 思维链泄漏进回复 | DeepSeek v4 返回 `reasoning_content`，SDK 标为 `thought=True` part，但 `event.get_text()` 拼接全部 part | `event_reply_text()` 过滤 `part.thought`；同源修复 SDK `channels._messages.event_to_text()` |

## 5. 复现步骤（WeCom 联调可复用）

```bash
# 1. 境外服务器（大陆无法访问 api.telegram.org）
# 2. 上传代码
rsync -avz -e "ssh -i ~/.ssh/tencent" --exclude '.venv' --exclude '.git' --exclude '.env' ./ ubuntu@<IP>:~/trpc-agent/
# 3. 部署
ssh ubuntu@<IP> 'cd ~/trpc-agent && bash deploy/e2e/setup.sh'
# 4. 填 .env（TELEGRAM_BOT_TOKEN / MODEL_API_KEY）
# 5. 启动
sudo systemctl start telegram-e2e
journalctl -u telegram-e2e -f
```

## 6. 后续

- WeCom 联调（需企业应用凭证 + 回调，`WecomAdapter` 验签 + AES 解密待接线）。
- 群聊 session 规则、图片/文件消息、多租户隔离的真机验证。
- `event_to_text` 已修复 thought 泄漏；如需保留思维链展示，可参考 AG-UI 的 `<trace_think>` 包装。
