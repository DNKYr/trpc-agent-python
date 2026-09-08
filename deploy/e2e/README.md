# Telegram E2E 部署指南

真实 Telegram bot 联调（long polling，无需域名/HTTPS）。**要求境外服务器**（大陆无法访问 api.telegram.org）。

## 一、上传代码（本地执行）

```bash
# 在本机 repo 根目录执行，rsync 到目标服务器（排除 .venv/.git/密钥）
rsync -avz \
  --exclude '.venv' --exclude '.git' --exclude '*.pyc' --exclude '__pycache__' \
  --exclude '.env' \
  ./ ubuntu@<服务器IP>:~/trpc-agent/
```

## 二、服务器端部署

```bash
cd ~/trpc-agent
bash deploy/e2e/setup.sh
```

## 三、填密钥并启动

```bash
# 填入 TELEGRAM_BOT_TOKEN 与 MODEL_API_KEY
vi ~/trpc-agent/examples/telegram_e2e/.env

# 启动
sudo systemctl start telegram-e2e
journalctl -u telegram-e2e -f    # 看日志
```

## 验证

在 Telegram 里给 bot 发消息，应收到 DeepSeek 模型的回复。

## 故障排查

- `journalctl -u telegram-e2e -f` 看运行日志
- 网络：`curl -sS https://api.telegram.org` 应返回非 000（境外服务器正常）
- 模型：`curl -sS https://api.deepseek.com` 返回 401 为正常（未带 key）

---

## WeCom 联调（需域名 + HTTPS + 企业应用凭证）

WeCom 接收消息必须走回调 URL（HTTPS）+ AES 解密，无 polling。

```bash
# 1. 装 Caddy（自动 HTTPS）
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update && sudo apt-get install -y caddy

# 2. 把 deploy/e2e/Caddyfile 里的 <YOUR_DOMAIN> 换成你的域名
sudo caddy run --config ~/trpc-agent/deploy/e2e/Caddyfile

# 3. 填 examples/wecom_e2e/.env（WECOM_* 与 MODEL_API_KEY）

# 4. 启动
sudo systemctl start wecom-e2e
journalctl -u wecom-e2e -f
```

企微应用「接收消息」回调 URL 配为 `https://<域名>/wecom/callback`，Token 与 EncodingAESKey 与 `.env` 一致。
