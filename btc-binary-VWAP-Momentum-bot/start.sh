#!/usr/bin/env bash
set -euo pipefail

# ═══════════════════════════════════════════════════════
# BTC 5/15-min 实时交易机器人 — Linux/macOS 启动脚本
# 用法: chmod +x start.sh && ./start.sh
# ═══════════════════════════════════════════════════════

# ── ANSI 颜色 ─────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# ── 切换到脚本所在目录 ─────────────────────────────────
cd "$(dirname "$0")"

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║     BTC 5/15-min 实时交易机器人         ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""

# ── 检查 .env 文件 ─────────────────────────────────────
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        echo -e "${YELLOW}[提示] .env 不存在，正在从 .env.example 复制...${NC}"
        cp .env.example .env
        echo -e "${YELLOW}[提示] 请编辑 .env 填入你的私钥和 API 密钥，然后重新运行。${NC}"
        echo -e "${YELLOW}       默认仪表盘密码: okok${NC}"
        exit 1
    else
        echo -e "${RED}[错误] .env.example 文件不存在！${NC}"
        exit 1
    fi
fi

# ── 检查 Python ────────────────────────────────────────
if ! command -v python3 &>/dev/null && ! command -v python &>/dev/null; then
    echo -e "${RED}[错误] 未找到 Python！请安装 Python 3.10+。${NC}"
    exit 1
fi

# 优先使用 python3
PYTHON=$(command -v python3 2>/dev/null || command -v python)
echo -e "${GREEN}[✓] Python: $($PYTHON --version)${NC}"

# ── 虚拟环境 ────────────────────────────────────────────
if [ ! -d "venv" ]; then
    echo -e "${YELLOW}[提示] 正在创建虚拟环境...${NC}"
    $PYTHON -m venv venv
    echo -e "${GREEN}[✓] 虚拟环境已创建${NC}"
fi

# 激活虚拟环境
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
elif [ -f "venv/Scripts/activate" ]; then
    # Git Bash on Windows
    source venv/Scripts/activate
else
    echo -e "${RED}[错误] 虚拟环境激活失败${NC}"
    exit 1
fi
echo -e "${GREEN}[✓] 虚拟环境已激活${NC}"

# ── 检查/安装依赖 ──────────────────────────────────────
if ! pip show python-dotenv &>/dev/null; then
    echo -e "${YELLOW}[提示] 正在安装依赖...${NC}"
    pip install -r requirements.txt
    echo -e "${GREEN}[✓] 依赖安装完成${NC}"
else
    echo -e "${GREEN}[✓] 依赖已就绪${NC}"
fi

# ── 启动机器人 ──────────────────────────────────────────
echo ""
echo -e "${BOLD}[启动] 正在启动交易机器人...${NC}"
echo -e "       按 ${RED}Ctrl+C${NC} 停止"
echo ""

$PYTHON main.py

# ── 结束 ────────────────────────────────────────────────
echo ""
echo -e "${GREEN}[停止] 机器人已退出${NC}"
