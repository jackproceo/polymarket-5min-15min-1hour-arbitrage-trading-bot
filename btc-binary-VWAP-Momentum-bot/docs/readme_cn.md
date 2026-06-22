# BTC 15 分钟 Polymarket 机器人 — 完整新手指南

**套件：** [PolyBullLabs — polymakret-5min-15min-1hour-arbitrage-bot](https://github.com/PolyBullLabs/polymakret-5min-15min-1hour-arbitrage-bot) · **Telegram：** [@terauss](https://t.me/terauss) · **上级概述：** [`../README.md`](../README.md)

本文档将从 **零开始带你到成功运行**，用**数字**解释**交易策略**，并通过**示例**列出**每个重要参数**。  
简短参考：[`CONFIG.md`](../CONFIG.md)（参数列表）、[`PROJECT_LOGIC.md`](../PROJECT_LOGIC.md)（实现细节）。

---

## 目录

1. [你在交易什么](#1-你在交易什么)
2. [交易策略（逻辑 + 公式）](#2-交易策略逻辑--公式)
3. [先决条件清单](#3-先决条件清单)
4. [环境设置 — Windows](#4-环境设置--windows)
5. [环境设置 — Linux / macOS](#5-环境设置--linux--macos)
6. [获取项目并安装依赖](#6-获取项目并安装依赖)
7. [配置 `.env`（密钥）](#7-配置-env密钥)
8. [配置 `config.json`（策略和执行）](#8-配置-configjson策略和执行)
9. [运行机器人](#9-运行机器人)
10. [可选：Telegram](#10-可选telegram)
11. [可选：盈亏图表](#11-可选盈亏图表)
12. [日志和文件](#12-日志和文件)
13. [故障排除](#13-故障排除)
14. [风险总结](#14-风险总结)

---

## 1. 你在交易什么

### 1.1 市场

Polymarket 提供 **15 分钟** BTC 市场（slug 模式如 `btc-updown-15m-<timestamp>`）。每个市场有两个结果代币：

| 代币 | 支付条件 |
|--------|---------|
| **UP** | BTC 收盘价**高于**参考价（Polymarket 市场规则定义精确的预言机） |
| **DOWN** | BTC 收盘价**低于**参考价 |

实际上，机器人读取 Polymarket 的**实时代币价格**（非手动预测）。它会买入**强势方**——即最新成交价**更高**的一侧。

### 1.2 收益计算（简化）

如果你以价格 **P**（每张合约美元，0–1）买入 **N** 张合约：

- **成本** ≈ **N × P**
- 如果你的一方**获胜**，每张合约价值 **$1** → 收益 **N × $1**
- **费用前利润** ≈ 获胜时 **N × (1 − P)**；亏损时你损失成本。

**纯数字示例：**

- 以 **$0.82** 买入 **6** 张 UP → 成本 **6 × 0.82 = $4.92**
- 如果 UP 获胜 → 价值 **6 × $1 = $6.00** → 毛利润 **$6.00 − $4.92 = $1.08**

机器人**不保证**利润；它在其**规则**满足时自动执行入场。

---

## 2. 交易策略（逻辑 + 公式）

### 2.1 强势方

机器人比较 **UP** 和 **DOWN** 的 `last_price`（来自市场 WebSocket）。**强势方**是价格**更高**的一侧。以下所有的偏离和动量计算均使用**强势方**的交易历史和价格。

### 2.2 VWAP（成交量加权平均价）

在过去 **`vwap_window_sec`** 秒内（例如 **30**），取该代币的所有交易，然后：

\[
\text{VWAP} = \frac{\sum (\text{价格} \times \text{数量})}{\sum \text{数量}}
\]

**示例**

| 时间 | 价格 | 数量 |
|------|------|------|
| T1 | 0.78 | 10 |
| T2 | 0.79 | 5 |

\[
\text{VWAP} = \frac{0.78 \times 10 + 0.79 \times 5}{10 + 5} = \frac{11.75}{15} \approx 0.7833
\]

### 2.3 偏离值（%）

比较**最新**成交价与 VWAP：

\[
\text{偏离值（\%）} = \frac{\text{最新价} - \text{VWAP}}{\text{VWAP}} \times 100
\]

**示例**

- 最新价 **0.82**，VWAP **0.78**  
- 偏离值 = \((0.82 - 0.78) / 0.78 × 100 ≈ 5.13\%\)

机器人要求偏离值**严格大于** `min_deviation_pct` 且**严格小于** `max_deviation_pct`（见 [§8.1](#81-strategy-策略块)）。

### 2.4 动量（%）

动量使用 **`momentum_window_sec`**（例如 **60**）的回看窗口。代码选取时间戳落在"现在 − 60s"附近**小范围**内的交易，对其价格取平均，然后将**当前**最新价与该平均值比较：

\[
\text{动量（\%）} = \frac{\text{最新价} - \text{平均价（过去）}}{\text{平均价（过去）}} \times 100
\]

如果该窗口内没有交易，则动量为**缺失**（`None`），信号**无法**触发。

**重要：** 在代码中，动量必须 **> 5%**（当前在 `config.json` 中不可配置）。所以 `momentum_window_sec` 改变的是动量的**衡量方式**，而非 **5%** 的阈值。

**示例**

- 约 60 秒前的平均价格：**0.77**
- 当前最新价：**0.82**
- 动量 = \((0.82 - 0.77) / 0.77 × 100 ≈ 6.5\%\) → **通过** > 5% 规则

### 2.5 入场时间窗口（15 分钟 = 900 秒）

每个市场从开始到结束持续 **900 秒**。

- `min_elapsed_sec` — 在市场开始后至少经过这么多秒**之前****不要**入场。  
  已过时间 = **900 − 剩余时间**（秒）。

- `no_entry_before_end_sec` — 如果**剩余时间** ≤ 此值（太接近到期），**不要**入场。

**实际示例**（与 `CONFIG.md` 一致）：

- `min_elapsed_sec = 530` → 需要已过时间 **≥ 530**  
- `no_entry_before_end_sec = 335` → 需要剩余时间 **> 335** → 已过时间 **< 565**

因此，入场仅当 **530 ≤ 已过时间 < 565** 时才有可能 → 每个市场约 **35 秒**（如果所有其他过滤器均通过）。

| 变量 | 值 |
|------|--------|
| `min_elapsed_sec` | 530 |
| `no_entry_before_end_sec` | 335 |
| 允许的已过时间 | 530 … 564 |
| 允许的剩余时间 | 336 … 370 |

如果你放宽窗口（例如降低 `min_elapsed_sec` 或提高 `no_entry_before_end_sec`），你会得到**更多**机会，通常也意味着**更多**风险。

### 2.6 胜率表（`data/win_rate.csv`）

行是**价格区间**（例如 `0.75-0.79`），列是**分钟**（`min_0` … `min_14`）。仪表板使用此表**显示**当前强势方价格和时间段的**历史胜率**。它本身**不会**在主信号逻辑中阻止交易（硬过滤器是价格、时间、偏离值、动量）。

### 2.7 入场检查清单（全部必须通过）

| # | 规则 | 典型配置 |
|---|------|----------------|
| 1 | 强势方价格在 `[min_price, max_price]` 内 | 例如 0.75–0.88 |
| 2 | `已过时间 ≥ min_elapsed_sec` | 例如 ≥ 530 |
| 3 | `min_deviation_pct < 偏离值 < max_deviation_pct` | 例如 3% < 偏离值 < 100% |
| 4 | 动量**不为** `None` 且 **> 5%** | 代码中固定 |
| 5 | `剩余时间 > no_entry_before_end_sec` | 例如 > 335 |

### 2.8 买入后

1. **FAK** 订单：买入至指定数量；未成交部分被取消。  
2. 可选**对冲**（如果启用）：在**对侧**以 `hedge_price`（通常为 **0.02**）下达 **GTD** 限价单。  
3. 接近市场结束时，机器人使用最新价**关闭**内部持仓以追踪盈亏。  
4. **自动赎回**（如果启用）定期在 Polygon 上赎回获胜头寸。

---

## 3. 先决条件清单

| 项目 | 原因 |
|------|------|
| **Python 3.11+**（3.12 也可以） | 运行机器人 |
| **pip / venv** | 隔离安装包 |
| **Polymarket 账户 + Polygon 上的 USDC** | 交易抵押品 |
| **少量 POL（MATIC）** | 链上赎回的 Gas 费（如果使用自动赎回） |
| **CLOB API 凭据** | 来自 Polymarket 的 key、secret、passphrase |
| **钱包私钥**（`0x…`） | 签署订单和赎回交易；**切勿分享** |

---

## 4. 环境设置 — Windows

### 4.1 安装 Python

1. 从 [https://www.python.org/downloads/](https://www.python.org/downloads/) 下载安装程序（Windows 64-bit）。
2. 运行安装程序。**勾选"Add Python to PATH"**（重要）。
3. 关闭并重新打开 **PowerShell** 或**命令提示符**。

### 4.2 验证

```powershell
python --version
pip --version
```

你应该看到 Python 3.11+ 和 pip。如果找不到 `python`，尝试 `py`（Windows 启动器）：

```powershell
py --version
```

### 4.3 Git Bash / `sudo` / `apt`

本项目在 Windows 上**不是**用 `sudo apt` 安装的。如果需要 Linux 风格的命令，请使用 **Windows 版 Python** 或 **WSL**（Ubuntu）。

### 4.4 执行策略（PowerShell venv）

如果激活失败并显示"running scripts is disabled"：

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

然后再次尝试 `.\venv\Scripts\Activate.ps1`。

---

## 5. 环境设置 — Linux / macOS

### 5.1 Linux（Debian/Ubuntu 示例）

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git
python3 --version
```

### 5.2 macOS

从 [python.org](https://www.python.org/downloads/) 安装 Python 3，或使用 `brew install python`。然后：

```bash
python3 --version
```

---

## 6. 获取项目并安装依赖

### 6.1 进入项目文件夹

如果你已经有文件夹（`btc-binary-VWAP-Momentum-bot`），**cd** 进入：

```bash
cd "path/to/polymakret-5min-15min-1hour-arbitrage-bot/btc-binary-VWAP-Momentum-bot"
```

如果从 git 克隆：

```bash
git clone https://github.com/PolyBullLabs/polymakret-5min-15min-1hour-arbitrage-bot.git
cd polymakret-5min-15min-1hour-arbitrage-bot/btc-binary-VWAP-Momentum-bot
```

### 6.2 创建并激活虚拟环境

**Windows（PowerShell）**

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

**Linux / macOS**

```bash
python3 -m venv venv
source venv/bin/activate
```

你的提示符应显示 `(venv)`。

### 6.3 安装 Python 包

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

等待完成且无错误。

### 6.4 快速检查

```bash
python -c "import rich, aiohttp, websockets; print('OK')"
```

如果看到 `OK`，依赖项已安装。

---

## 7. 配置 `.env`（密钥）

### 7.1 从示例创建 `.env`

**Windows**

```powershell
copy .env.example .env
```

**Linux / macOS**

```bash
cp .env.example .env
```

### 7.2 填写每个变量

| 变量 | 必填 | 示例 / 备注 |
|----------|----------|-----------------|
| `PRIVATE_KEY` | **是** | `0x` + 64 位十六进制字符。**切勿**提交或分享。 |
| `SIGNATURE_TYPE` | **是** | `0` = EOA（普通钱包）。`1` 或 `2` = 代理 / magic — 见 Polymarket 文档。 |
| `FUNDER_ADDRESS` | 使用代理时 | 当 `SIGNATURE_TYPE` 为 1 或 2 时的 Polymarket 代理钱包地址。 |
| `POLY_API_KEY` | **是** | 来自 CLOB API。 |
| `POLY_API_SECRET` | **是** | 来自 CLOB API。 |
| `POLY_API_PASSPHRASE` | **是** | 来自 CLOB API。 |
| `RPC_URL` | 可选 | 默认 `https://polygon-rpc.com`，生产环境推荐 Alchemy/Infura。 |
| `CHAIN_ID` | 可选 | Polygon 主网为 `137`。 |
| `CLOB_HOST` | 可选 | 通常为 `https://clob.polymarket.com`。 |
| `TELEGRAM_BOT_TOKEN` | 可选 | 来自 @BotFather。 |
| `TELEGRAM_CHAT_ID` | 可选 | 你的数字聊天 ID（例如来自 @userinfobot）。 |

### 7.3 如何获取 API 密钥

- 登录 Polymarket，打开 **CLOB API** / 开发者设置，创建 **API 凭证**（key、secret、passphrase）。  
- 仓库中引用的官方 URL：[https://clob.polymarket.com](https://clob.polymarket.com)

### 7.4 示例 `.env` 内容（假值）

```env
PRIVATE_KEY=0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
SIGNATURE_TYPE=0
FUNDER_ADDRESS=

POLY_API_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
POLY_API_SECRET=your_secret_here
POLY_API_PASSPHRASE=your_passphrase_here

RPC_URL=https://polygon-rpc.com
CHAIN_ID=137
CLOB_HOST=https://clob.polymarket.com

TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

保存文件。**确认 `.env` 已被 gitignore**（不要提交）。

---

## 8. 配置 `config.json`（策略和执行）

编辑项目根目录下的 **`config.json`**。以下：**每个模块的作用**、**推荐范围**和**数字示例**。

### 8.1 `strategy` 策略块

| 参数 | 含义 | 示例 |
|-----------|---------|---------|
| `min_price` | 允许入场的最低强势方代币价格 | `0.75` — 忽略 $0.75 以下的强势方 |
| `max_price` | 最高强势方代币价格 | `0.88` — 不买高于 $0.88 的 |
| `min_elapsed_sec` | 市场开盘后多少秒才能入场 | `530` — 等待约 8.8 分钟 |
| `min_deviation_pct` | 偏离值必须 **>** 此值 | `3` — 需要高于 VWAP 超过 3% |
| `max_deviation_pct` | 偏离值必须 **<** 此值 | `100` — 实际上无上限 |
| `no_entry_before_end_sec` | 如果 `剩余时间 ≤` 此值则停止入场 | `335` — 最后约 5.6 分钟内不再新入场 |
| `momentum_window_sec` | 动量的历史秒数 | `60` — 与约 1 分钟前比较 |
| `vwap_window_sec` | VWAP 的交易秒数 | `30` — 短期均值 |
| `win_rate_csv` | 仪表板胜率 CSV 路径 | `"data/win_rate.csv"` |

**偏离值示例**

- VWAP（30s）= **0.78**，最新价 = **0.80** → 偏离值 ≈ **2.56%** → 如果 `min_deviation_pct` 为 **3** 则失败  
- 最新价 = **0.81** → 偏离值 ≈ **3.85%** → 如果 `min_deviation_pct` 为 **3** 则通过

### 8.2 `entry` 入场块

| 参数 | 含义 | 示例 |
|-----------|---------|---------|
| `bet_amount_usd` | 每笔入场的目标花费（受仓位大小规则约束） | `5` → 约 $5 名义金额 |
| `price_offset` | 下达 FAK 时加在价格上的偏移（更具攻击性的成交） | `0.02` → 相对于参考价最多多付 +$0.02 |
| `order_type` | 入场类型 | `"FAK"`（立即成交否则取消） |
| `max_retries` | 如果订单未按预期完成的重试次数 | `3` |
| `retry_delay_ms` | 重试之间的暂停时间 | `300` |
| `fill_timeout_ms` | 执行器/成交逻辑中使用 | `1000` |
| `min_contracts` | Polymarket 最低要求通常为 5 | `5` |
| `min_order_usd` | 最低订单金额（USD） | `1` |
| `max_entry_price` | 执行价格的硬性上限 | `0.88` — 应与 `strategy.max_price` 一致 |
| `ws_recovery_timeout_sec` | HTTP 超时后，观察用户 WebSocket 成交的时间 | `10` |

**仓位大小示例**

- `bet_amount_usd = 5`，最佳卖价 ≈ **0.80** → 大约合约数 = floor(5 / 0.80) = **6**（受最低要求和 API 约束）

### 8.3 `hedge` 对冲块

| 参数 | 含义 | 示例 |
|-----------|---------|---------|
| `enabled` | `true` / `false` | 初学者建议 `false` |
| `hedge_price` | 对侧代币的限价 | `0.02` |
| `order_type` | 通常为 `"GTD"` | 被动限价单 |
| `max_retries` | 下单重试次数 | `3` |
| `retry_delay_ms` | 重试之间的延迟 | `1000` |

**对冲的直觉（非投资建议）**  
在买入 UP 后，一个**廉价**的 DOWN 限价单可以在市场变动使 DOWN 交易接近你的限价时起到部分对冲作用。**成本和风险**是真实的；在了解成交情况之前，从 `enabled: false` 开始。

### 8.4 `redeem` 赎回块

| 参数 | 含义 | 示例 |
|-----------|---------|---------|
| `enabled` | 运行定期链上赎回 | `true` |
| `interval_seconds` | 扫描间隔秒数 | `180` |
| `auto_confirm` | 在代码路径中确认 | `true` |

**注意：** 在 **Windows** 上，某些仅 Unix 的赎回锁定可能会失败；生产环境 **Linux** 或 **WSL** 更安全。

### 8.5 `telegram` 块

| 参数 | 含义 |
|-----------|---------|
| `enabled` | `true` 以发送 Telegram 消息 |
| `chart_every_n_trades` | 用于定期权益图表（见 `TelegramNotifier.send_equity_chart`）；**可能未**在所有版本的 `main.py` 中连接——如果你依赖自动图表，请检查代码 |

Token 和聊天 ID 仍来自 **`.env`**。

### 8.6 `config.json` 中的 `logging` 日志块

仓库可能包含用于文档的 `logging` 部分。**当前的 `main.py` 在代码中设置日志**（例如 `logs/bot.log`，`INFO` 级别）。不要假定 `config.json` 的日志键会改变行为，除非你在代码中连接它们。

### 8.7 预设方案（可直接复制使用的起点）

**保守型（较少交易，较紧的区间）**

```json
"strategy": {
  "min_price": 0.80,
  "max_price": 0.85,
  "min_elapsed_sec": 600,
  "min_deviation_pct": 5,
  "max_deviation_pct": 100,
  "no_entry_before_end_sec": 300,
  "momentum_window_sec": 60,
  "vwap_window_sec": 30,
  "win_rate_csv": "data/win_rate.csv"
},
"entry": { "bet_amount_usd": 2 },
"hedge": { "enabled": false }
```

**激进型（更多交易 — 更高风险）**

```json
"strategy": {
  "min_price": 0.70,
  "max_price": 0.90,
  "min_elapsed_sec": 400,
  "min_deviation_pct": 0,
  "max_deviation_pct": 100,
  "no_entry_before_end_sec": 120,
  "momentum_window_sec": 60,
  "vwap_window_sec": 30,
  "win_rate_csv": "data/win_rate.csv"
},
"entry": { "bet_amount_usd": 5 },
"hedge": { "enabled": false }
```

---

## 9. 运行机器人

1. 激活 **venv**（见 [§6.2](#62-创建并激活虚拟环境)）。  
2. 确保 `.env` 和 `config.json` 已保存。  
3. 从**项目根目录**（包含 `main.py` 的文件夹）：

```bash
python main.py
```

### 9.1 你应该看到的内容

- 启动消息（配置摘要、CLOB 初始化）。  
- **实时 Rich 仪表板**：计时器、UP/DOWN 代币面板、指标、**Strategy** 行、盈亏。  
- 当 **BUY UP** / **BUY DOWN** 信号有效时，机器人开火入场（如果你的密钥是实盘状态，则是真实资金）。

### 9.2 停止机器人

在终端中按 **Ctrl+C**。在 Windows 上，Unix 信号处理程序可能受限；**Ctrl+C** 仍会停止进程。

### 9.3 首次运行建议

- 将 **`bet_amount_usd`** 设小。  
- 在了解行为前，将 **`hedge.enabled`** 设为 **`false`**。  
- 在市场运行时观察 **`logs/`** 目录。

---

## 10. 可选：Telegram

1. **@BotFather** → `/newbot` → 复制 **token** → 填入 `.env` 的 `TELEGRAM_BOT_TOKEN`。  
2. **@userinfobot** → `/start` → 复制 **Id** → 填入 `TELEGRAM_CHAT_ID`。  
3. 在 Telegram 中打开你的机器人并点击 **Start**（必须）。  
4. 在 `config.json` 中设置 `"telegram": { "enabled": true, ... }`。

---

## 11. 可选：盈亏图表

当你在 **`logs/trading_log.json`** 中有交易后：

```bash
python chart_pnl.py
```

输出图片：**`logs/pnl_chart.png`**（见 `chart_pnl.py`）。

---

## 12. 日志和文件

| 文件 / 文件夹 | 内容 |
|----------------|---------|
| `logs/bot.log` | 通用机器人日志 |
| `logs/orders.log` | 订单执行详情 |
| `logs/hedges.log` | 对冲相关日志 |
| `logs/signals.log` | 信号快照 |
| `logs/trading_log.json` | 持久化的交易和统计 |
| `logs/pnl_chart.png` | 由 `chart_pnl.py` 生成 |

---

## 13. 故障排除

| 问题 | 解决方法 |
|--------|-------------|
| `python` 未找到（Windows） | 重新安装 Python 时勾选 **Add to PATH**，或使用 `py -m venv venv` |
| `add_signal_handler` 上的 `NotImplementedError` | Windows 上已在 `main.py` 中修复 — 使用最新代码 |
| 启动时配置错误 | 阅读打印的消息；通常是缺少 `PRIVATE_KEY` 或 API 字段 |
| `python` 工作但导入失败 | 激活 **venv** 并重新运行 `pip install -r requirements.txt` |
| 长时间没有交易 | 策略窗口很窄（见 [§2.5](#25-入场时间窗口15-分钟--900-秒)）；或者市场从未满足所有过滤器 |
| Windows 上的赎回错误 | 建议使用 **WSL** 或 **Linux** 进行自动赎回；或禁用 `redeem.enabled` 并在 Polymarket 上手动赎回 |
| Telegram 未发送 | 机器人 token + 聊天 ID + 用户在机器人上按了 **Start**；`enabled: true` |

---

## 14. 风险总结

- **真实资金** — 你可能损失本金。  
- **不保证任何策略优势** — 此机器人自动化的是规则。  
- **费用、滑点和订单失败**是可能发生的。  
- **保护好你的私钥** — 像对待密码一样对待 `.env`。

如需**多资产尾盘入场**交易，请参见同一仓库中的 **Meridian**（`up-down-spread-bot`）。如需 **PTB / 预言差价**规则和 Web 仪表板，请参见 `5min-15min-PTB-bot`。扩展的**量化**产品（凯利公式、蒙特卡洛、高级 TA、仓位管理系统）在[仓库根目录 README](https://github.com/PolyBullLabs/polymakret-5min-15min-1hour-arbitrage-bot) 中有描述——联系 [@terauss](https://t.me/terauss)。

---

*单页参数列表请参见 [`CONFIG.md`](../CONFIG.md)。内部实现请参见 [`PROJECT_LOGIC.md`](../PROJECT_LOGIC.md)。*
