# WeCom E2E 联调

用真实企业微信自建应用验证多租户 SDK 的端到端链路（回调验签 + AES 解密 → `ChannelRouter` → `RunnerPool` → 模型 → 企微 API 回复）。

**WeCom 接收消息必须走回调 URL（HTTPS）+ AES 解密**，无 polling 模式。

## 前置

1. 企业微信管理后台：企业ID（corp_id）、自建应用 AgentId + Secret。
2. 应用「接收消息」回调配置：回调 URL + Token + EncodingAESKey（43 位）。
3. 公网域名 + HTTPS（回调 URL 必须 HTTPS 且域名需加入应用「可信域名」）。
4. 模型 API key。

## 运行

```bash
cp examples/wecom_e2e/.env.example examples/wecom_e2e/.env
# 填 WECOM_* 与 MODEL_API_KEY

pip install fastapi uvicorn   # 额外依赖（非 SDK 核心）
uvicorn examples.wecom_e2e.main:app --host 0.0.0.0 --port 8080
```

回调 URL 配置为 `https://<域名>/wecom/callback`，启动后企微会先 GET 验签（`verify_callback`），通过后开始推送消息。

## 说明

- 验签 + AES 解密：`trpc_agent_sdk/channels/_wecom_crypto.py`（`WeComCrypto`）。
- 链路：POST 回调 → 验签 → 解密 → `WecomAdapter.parse_inbound` → `ChannelRouter`（去重/映射）→ `RunnerPool` → 模型 → `message/send`（≤2048 字节，分片）。
- 会话：单聊 `session_id = chat_wecom_{FromUserName}`；数据后端 in_memory。
