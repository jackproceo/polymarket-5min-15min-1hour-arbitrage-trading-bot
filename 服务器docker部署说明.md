# 服务器 Docker 部署说明

> 适用环境：Linux VPS（Ubuntu 22.04 / Debian 12 等）
> 本文档指导你将本仓库完整部署至服务器，使用 Docker 容器化运行三个交易机器人。

---

## 目录

1. [推送代码至 GitHub](#1-推送代码至-github)
2. [服务器环境准备](#2-服务器环境准备)
3. [克隆代码至服务器](#3-克隆代码至服务器)
4. [配置每个机器人](#4-配置每个机器人)
5. [构建并启动容器](#5-构建并启动容器)
6. [验证运行状态](#6-验证运行状态)
7. [日常运维](#7-日常运维)
8. [更新代码](#8-更新代码)
9. [故障排查](#9-故障排查)

---

## 1. 推送代码至 GitHub

在本地仓库根目录执行：

```bash
# 初始化 git 仓库（如尚未初始化）
git init

# 添加远程仓库
git remote add origin https://github.com/jackproceo/polymarket-5min-15min-1hour-arbitrage-trading-bot.git

# 添加所有文件并提交
git add .
git commit -m "Initial commit: 3 Polymarket trading bots (VWAP, Meridian, PTB)"

# 推送到 GitHub（如仓库非空需先 pull）
git branch -M main
git push -u origin main
```

> 推送前请确保 `.gitignore` 已正确配置，避免将 `.env`、`config.json`（含密钥）、`logs/`、`data/` 等敏感或运行时文件提交到仓库。

---

## 2. 服务器环境准备

### 2.1 安装 Docker

```bash
# Ubuntu / Debian
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER   # 将当前用户加入 docker 组（需重新登录生效）
```

验证安装：

```bash
docker --version
docker compose version
```

### 2.2 安装 Git

```bash
sudo apt update && sudo apt install -y git
```

---

## 3. 克隆代码至服务器

```bash
cd /opt
sudo git clone https://github.com/jackproceo/polymarket-5min-15min-1hour-arbitrage-trading-bot.git
sudo chown -R $USER:$USER /opt/polymarket-5min-15min-1hour-arbitrage-trading-bot
cd /opt/polymarket-5min-15min-1hour-arbitrage-trading-bot
```

---

## 4. 配置每个机器人

三个机器人各自需要独立的配置文件和凭据。所有配置文件均通过 Docker bind mount 从宿主机映射到容器内，**密钥不会烧入镜像**。

### 4.1 总览

| 机器人 | 需要创建的配置文件 | 来源模板 |
|--------|-------------------|---------|
| **VWAP bot** | `btc-binary-VWAP-Momentum-bot/.env` | `.env.example` |
|  | `btc-binary-VWAP-Momentum-bot/config.json` | 已内置（模拟模式默认可用） |
| **Meridian** | `up-down-spread-bot/.env` | `.env.example` |
|  | `up-down-spread-bot/config/config.json` | `config/config.example.json` |
| **PTB bot** | `5min-15min-PTB-bot/config.env` | `config.env.example` |

### 4.2 配置 VWAP bot

```bash
cd /opt/polymarket-5min-15min-1hour-arbitrage-trading-bot/btc-binary-VWAP-Momentum-bot

# 创建 .env（钱包密钥、Polymarket API 凭据）
cp .env.example .env
nano .env   # 编辑填入真实凭据
```

`.env` 中需填入：
- `PRIVATE_KEY` — Polygon 钱包私钥
- `POLYMARKET_API_KEY` / `POLYMARKET_API_SECRET` / `POLYMARKET_API_PASSPHRASE`

**config.json** 已内置模拟模式（`simulation.enabled: true`），如需调整策略参数（交易金额、触发条件等）可直接编辑。

### 4.3 配置 Meridian

```bash
cd /opt/polymarket-5min-15min-1hour-arbitrage-trading-bot/up-down-spread-bot

# 创建 .env（钱包密钥）
cp .env.example .env
nano .env

# 创建 config.json（交易参数、风控设置）
cp config/config.example.json config/config.json
nano config/config.json
```

**关键配置项（config.json）：**

```json
{
  "safety": {
    "dry_run": true,       // 首次运行务必保持 true（模拟模式）
    "max_order_size": 10,  // 单笔最大 USDC
    "max_investment": 50   // 总投入上限
  }
}
```

> 首次部署建议保持 `"dry_run": true`，确认信号和下单逻辑正常后再改为 `false`。

### 4.4 配置 PTB bot

```bash
cd /opt/polymarket-5min-15min-1hour-arbitrage-trading-bot/5min-15min-PTB-bot

# 创建 config.env
cp config.env.example config.env
nano config.env
```

**关键配置项：**

```bash
SIMULATION_MODE=true        # 首次务必 true
AUTO_TRADE=false            # 先观察信号，确认后再开启自动交易
BTC_MARKET_MINUTES=5        # 5 或 15
TRADE_AMOUNT=5              # 每笔 USDC
```

### 4.5 创建数据目录（首次部署需要）

```bash
cd /opt/polymarket-5min-15min-1hour-arbitrage-trading-bot

# VWAP bot 日志和数据目录
mkdir -p btc-binary-VWAP-Momentum-bot/logs
mkdir -p btc-binary-VWAP-Momentum-bot/data

# Meridian 日志目录
mkdir -p up-down-spread-bot/logs

# PTB bot 数据目录（含占位文件）
mkdir -p 5min-15min-PTB-bot/data
echo '{}' > 5min-15min-PTB-bot/data/state.json
touch 5min-15min-PTB-bot/data/trading_analysis.jsonl
```

---

## 5. 构建并启动容器

### 5.1 构建镜像

```bash
cd /opt/polymarket-5min-15min-1hour-arbitrage-trading-bot
docker compose build
```

首次构建需要下载基础镜像和安装 Python 依赖，耗时约 2-5 分钟（视网络而定）。

### 5.2 启动所有服务

```bash
docker compose up -d
```

### 5.3 查看启动日志

```bash
# 所有容器日志
docker compose logs -f

# 单独查看某个机器人
docker compose logs -f bot-vwap
docker compose logs -f bot-meridian
docker compose logs -f bot-ptb
```

观察到以下输出表示启动成功：

- **VWAP bot**: `[bold yellow]SIMULATION (no real orders)[/bold yellow]`
- **Meridian**: `🟢 DRY_RUN (SAFE)` 或 `🟢 DRY_RUN`  
- **PTB bot**: `SIMULATION_MODE: paper trading — instant fills, no CLOB orders, no redeem`

### 5.4 健康检查

```bash
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

三个容器均应为 `Up` 状态（含 `(healthy)`）：

```
polymarket-bot-vwap      Up About a minute (healthy)   0.0.0.0:24008->8765/tcp
polymarket-bot-meridian  Up About a minute (healthy)   0.0.0.0:24018->5050/tcp
polymarket-bot-ptb       Up About a minute (healthy)   0.0.0.0:24028->5080/tcp
```

### 5.5 访问 Web 仪表板

在浏览器中访问（将 `<服务器IP>` 替换为实际 IP）：

| 机器人 | 地址 |
|--------|------|
| VWAP 仪表板 | `http://<服务器IP>:24008` |
| Meridian 仪表板 | `http://<服务器IP>:24018` |
| PTB 仪表板 | `http://<服务器IP>:24028` |

> 如需通过域名 + HTTPS 访问，建议前置 Nginx 反向代理并配置 SSL 证书。

---

## 6. 验证运行状态

### 6.1 检查 Web 接口

```bash
# VWAP API
curl http://localhost:24008/api/state

# Meridian health
curl http://localhost:24018/api/health

# PTB health
curl http://localhost:24028/
```

### 6.2 查看本地持久化数据

所有日志和运行数据实时写入宿主机对应目录，容器重启后不会丢失：

```bash
# VWAP 交易记录
ls -la /opt/polymarket-5min-15min-1hour-arbitrage-trading-bot/btc-binary-VWAP-Momentum-bot/logs/

# Meridian 交易记录
ls -la /opt/polymarket-5min-15min-1hour-arbitrage-trading-bot/up-down-spread-bot/logs/

# PTB 运行状态
ls -la /opt/polymarket-5min-15min-1hour-arbitrage-trading-bot/5min-15min-PTB-bot/data/
```

### 6.3 观察一段时间

建议所有机器人在 **模拟模式** 下运行至少 24–48 小时，观察：

- ✔ 信号触发频率是否合理
- ✔ 模拟盈亏趋势
- ✔ 日志无异常报错
- ✔ Web 仪表板数据正常刷新

确认无误后再切换到真实资金模式。

---

## 7. 日常运维

### 7.1 常用命令

```bash
# 查看状态
docker compose ps

# 查看日志（实时）
docker compose logs -f

# 查看日志（最近 100 行）
docker compose logs --tail=100

# 重启某个服务
docker compose restart bot-vwap

# 停止所有服务
docker compose down

# 停止并删除所有数据卷（⚠️ 将丢失日志和状态）
docker compose down -v
```

### 7.2 从模拟切换到实盘

**VWAP bot：**
编辑 `btc-binary-VWAP-Momentum-bot/config.json`，将 `"simulation.enabled": true` 改为 `false`，然后重启：

```bash
docker compose restart bot-vwap
```

**Meridian：**
编辑 `up-down-spread-bot/config/config.json`，将 `"dry_run": true` 改为 `false`：

```bash
docker compose restart bot-meridian
```

**PTB bot：**
编辑 `5min-15min-PTB-bot/config.env`，将 `SIMULATION_MODE=false` 且 `AUTO_TRADE=true`：

```bash
docker compose restart bot-ptb
```

### 7.3 容器资源限制（可选）

如服务器资源有限，可在 `docker-compose.yml` 中为每个服务添加资源限制：

```yaml
services:
  bot-vwap:
    # ... 其他配置
    deploy:
      resources:
        limits:
          memory: 512M
```

### 7.4 日志轮转

Docker Compose 已配置日志轮转（每个容器最多保留 3 个文件，每个 10MB），无需额外配置：

```yaml
x-logging: &default-logging
  driver: "json-file"
  options:
    max-size: "10m"
    max-file: "3"
```

---

## 8. 更新代码

当仓库有更新时，按以下步骤部署新版本：

```bash
cd /opt/polymarket-5min-15min-1hour-arbitrage-trading-bot

# 拉取最新代码
git pull

# 重新构建镜像（只重建有变化的层）
docker compose build

# 重启容器（不中断的滚动更新）
docker compose up -d --force-recreate
```

> 配置文件（`.env`、`config.json`、`config.env`）不会被 `git pull` 覆盖，因为它们已被 `.gitignore` 排除。

---

## 9. 故障排查

### 容器启动后立即退出

```bash
docker compose logs bot-vwap   # 查看具体错误
```

常见原因：
- `.env` 或 `config.json` 格式错误
- 端口被占用（修改 `docker-compose.yml` 中 host 端口映射）

### 健康检查失败

```bash
docker inspect --format='{{json .State.Health}}' polymarket-bot-vwap | jq
```

首次启动有 30 秒 `starting` 期，耐心等待即可。若持续 `unhealthy`，检查 Web 服务是否正常启动。

### 数据目录没有文件

确认 bind mount 路径正确：

```bash
# 检查容器内 /app/logs 是否挂载成功
docker exec polymarket-bot-vwap ls -la /app/logs
```

### 网络问题

三个容器运行在同一 bridge 网络内，通过容器名互相访问（如需要）。如需外部访问 Web 仪表板，请确认服务器防火墙已放行对应端口（24008 / 24018 / 24028）。

---

> **最后提醒**：预测市场交易可能导致本金全部损失。所有机器人在切换到实盘前，请充分测试并理解策略行为。从小资金开始，逐步验证。
