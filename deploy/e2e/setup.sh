#!/usr/bin/env bash
# Telegram E2E 服务器端一键部署（在目标服务器上运行，可重复执行）
# 前置：代码已 rsync 到 ~/trpc-agent（见 deploy/e2e/README.md）
set -euo pipefail

APP_DIR="${HOME}/trpc-agent"

echo "==> 安装系统依赖"
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip build-essential git

echo "==> 创建虚拟环境并安装 SDK"
cd "${APP_DIR}"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
.venv/bin/pip install fastapi   # WeCom 回调服务额外依赖（非 SDK 核心）

echo "==> 生成 .env 模板（请手动填入密钥）"
if [ ! -f examples/telegram_e2e/.env ]; then
    cp examples/telegram_e2e/.env.example examples/telegram_e2e/.env
fi
if [ ! -f examples/wecom_e2e/.env ]; then
    cp examples/wecom_e2e/.env.example examples/wecom_e2e/.env
fi

echo "==> 安装 systemd 服务"
sudo cp deploy/e2e/telegram-e2e.service /etc/systemd/system/telegram-e2e.service
sudo cp deploy/e2e/wecom-e2e.service /etc/systemd/system/wecom-e2e.service
sudo systemctl daemon-reload
sudo systemctl enable telegram-e2e wecom-e2e

echo
echo "完成。下一步："
echo "  Telegram: 编辑 examples/telegram_e2e/.env -> sudo systemctl start telegram-e2e"
echo "  WeCom:    编辑 examples/wecom_e2e/.env -> 配置 Caddy 反代 -> sudo systemctl start wecom-e2e"
echo "  查看日志: journalctl -u telegram-e2e -f / journalctl -u wecom-e2e -f"
