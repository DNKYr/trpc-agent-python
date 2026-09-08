# Telegram E2E 联调

用真实 Telegram bot 验证多租户 SDK 的端到端链路（IM 收发 → `ChannelRouter` 去重/映射 → `RunnerPool` 执行 → 模型回复）。

使用 **long polling**（非 webhook），无需公网域名/HTTPS，只需可出网的服务器。

## 前置

1. 在 [@BotFather](https://t.me/BotFather) 创建 bot，拿到 token。
2. 一个模型 API key（OpenAI/DeepSeek/Anthropic 等，`model_name` 需能被 SDK `ModelRegistry` 解析）。

## 运行

```bash
export TELEGRAM_BOT_TOKEN=123456:ABC...
export MODEL_API_KEY=sk-...
export MODEL_NAME=gpt-4o-mini        # 可选，默认 gpt-4o-mini
export TELEGRAM_BOT_USERNAME=my_bot  # 可选，默认 e2e_bot

python examples/telegram_e2e/main.py
```

然后在 Telegram 里给 bot 发消息，等待回复。

## 说明

- 复用 SP4 `TelegramAdapter`（解析）+ `ChannelRouter`（去重/映射，`verify_enabled=False` 因为 polling 无签名）+ SP3 `RunnerPool`（执行）。
- 会话：单聊 `session_id = chat_telegram_{chat_id}`；数据后端 `in_memory`（重启即清，联调够用，可换 Redis）。
- 收到非文本消息（图片/文件）会跳过。
