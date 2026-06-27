@echo off
chcp 65001 >nul
title Polymarket BTC Auto-Trader
cd /d "%~dp0"

echo ================================================
echo   Polymarket BTC Auto-Trader
echo ================================================

:: 检查 Python
where python >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Python not found. Install Python 3.8+ first.
    pause
    exit /b 1
)

:: 安装依赖（如果缺失）
if not exist ".deps_installed" (
    echo.
    echo [..] Installing dependencies ...
    python -m pip install -r requirements.txt
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] pip install failed.
        pause
        exit /b 1
    )
    echo. > .deps_installed
    echo [OK] Dependencies installed.
)

:: 检查 config.env
if not exist "config.env" (
    echo.
    echo [WARN] config.env not found.
    echo        Copy config.env.example to config.env and edit it.
    if exist "config.env.example" (
        echo        Run: copy config.env.example config.env
    )
)

echo.
echo [..] Starting bot ...
echo.

python main.py

pause
