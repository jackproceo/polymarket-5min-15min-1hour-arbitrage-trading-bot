#!/usr/bin/env bash
# Polymarket BTC 自动交易 — 启动脚本 (Linux / macOS)
set -e

cd "$(dirname "$0")"

echo "================================================"
echo "  Polymarket BTC Auto-Trader"
echo "================================================"

# 检查 Python
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] python3 not found. Install Python 3.8+ first."
    exit 1
fi

# 安装依赖（如果缺失）
if [ ! -f ".deps_installed" ]; then
    echo ""
    echo "[..] Installing dependencies ..."
    python3 -m pip install -r requirements.txt
    touch .deps_installed
    echo "[OK] Dependencies installed."
fi

# 检查 config.env
if [ ! -f "config.env" ]; then
    echo ""
    echo "[WARN] config.env not found."
    echo "       Copy config.env.example to config.env and edit it."
    if [ -f "config.env.example" ]; then
        echo "       Run: cp config.env.example config.env"
    fi
fi

echo ""
echo "[..] Starting bot ..."
echo ""

exec python3 main.py
