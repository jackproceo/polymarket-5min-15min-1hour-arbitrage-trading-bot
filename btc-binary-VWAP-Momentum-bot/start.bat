@echo off
chcp 65001 >nul
title BTC 实时交易机器人

echo ╔══════════════════════════════════════════╗
echo ║     BTC 5/15-min 实时交易机器人         ║
echo ╚══════════════════════════════════════════╝
echo.

:: ── 切换到脚本所在目录 ──────────────────────────────────────
cd /d "%~dp0"

:: ── 检查 .env 文件 ──────────────────────────────────────────
if not exist ".env" (
    if exist ".env.example" (
        echo [提示] .env 不存在，正在从 .env.example 复制...
        copy ".env.example" ".env" >nul
        echo [提示] 请编辑 .env 填入你的私钥和 API 密钥，然后重新运行。
        echo         默认仪表盘密码: okok
        start notepad .env
        pause
        exit /b 1
    ) else (
        echo [错误] .env.example 文件不存在！
        pause
        exit /b 1
    )
)

:: ── 检查 Python ────────────────────────────────────────────
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 Python！请安装 Python 3.10+ 并添加到 PATH。
    pause
    exit /b 1
)
echo [✓] Python 已检测

:: ── 虚拟环境 ────────────────────────────────────────────────
if not exist "venv\" (
    echo [提示] 正在创建虚拟环境...
    python -m venv venv
    echo [✓] 虚拟环境已创建
)

call venv\Scripts\activate.bat >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 虚拟环境激活失败
    pause
    exit /b 1
)
echo [✓] 虚拟环境已激活

:: ── 检查/安装依赖 ──────────────────────────────────────────
pip show python-dotenv >nul 2>&1
if %errorlevel% neq 0 (
    echo [提示] 正在安装依赖...
    pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [错误] 依赖安装失败
        pause
        exit /b 1
    )
    echo [✓] 依赖安装完成
) else (
    echo [✓] 依赖已就绪
)

:: ── 启动机器人 ──────────────────────────────────────────────
echo.
echo [启动] 正在启动交易机器人...
echo        按 Ctrl+C 停止
echo.
python main.py

:: ── 结束 ────────────────────────────────────────────────────
echo.
echo [停止] 机器人已退出
pause
