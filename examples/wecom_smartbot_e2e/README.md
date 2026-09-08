# WeCom 智能机器人（长连接）E2E 联调

用官方 `wecom-aibot-python-sdk` 建立 WebSocket 长连接（**无需域名/HTTPS/回调 URL**），接收并回复企业微信智能机器人消息。

## 前置

1. 企业微信后台创建智能机器人，选【API 模式】+【使用长连接】，拿到 `BotID` + `Secret`。
2. 模型 API key。

## 运行

```bash
cp examples/wecom_smartbot_e2e/.env.example examples/wecom_smartbot_e2e/.env
# 填 WECOM_BOT_ID / WECOM_BOT_SECRET / MODEL_API_KEY

pip install wecom-aibot-python-sdk   # 官方 SDK
python examples/wecom_smartbot_e2e/main.py
```

看到「机器人订阅成功」后，单聊发消息或群聊 `@机器人` 即可收到回复。

## 说明

- 链路：WS 长连接 → `message.text` → 排重（msgid）→ `RunnerPool` → DeepSeek → `reply_stream`（≤2000 字符分片）。
- 会话：`chat_wecom_{chatid}`（单聊）/ `group_wecom_{chatid}`（群聊）；数据后端 in_memory。
- 注意：`from.userid` 可能是加密 userid（机器人创建者非企业超管时）；「每个机器人同一时间只能一个有效长连接」。
