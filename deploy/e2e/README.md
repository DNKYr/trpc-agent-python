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
