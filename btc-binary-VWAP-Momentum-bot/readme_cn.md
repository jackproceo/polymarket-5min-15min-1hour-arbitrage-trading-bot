# BTC Binary — VWAP 和动量交易机器人

用于 **Polymarket BTC 涨/跌** 二元市场（**5 分钟或 15 分钟**窗口；在 `config.json` 中设置 `market.interval_minutes`）的自动化交易机器人。它通过 WebSocket 流式获取 CLOB 数据，计算**强势侧**的 **VWAP**、**偏离值**、**动量**和 **Z-Score**，并在**所有**条件满足时执行 **FAK（Fill-And-Kill）** 订单。可选的 **GTD（Good-Till-Date）** 对侧限价单作为**部分对冲**（高级功能，默认关闭）。

**套件说明：** 本机器人属于 [PolyBullLabs Polymarket 套件](../README.md)。**仓库地址：** [github.com/PolyBullLabs/polymakret-5min-15min-1hour-arbitrage-bot](https://github.com/PolyBullLabs/polymakret-5min-15min-1hour-arbitrage-bot.git) · **Telegram：** [@terauss](https://t.me/terauss)

---

## 该策略为何有效（以及什么情况下会失效）

**核心理念：** 在短周期二元窗口临近结束时，市场通常会将某一侧定价为**强势方**（较高最新价）。机器人并非盲目买入：它等待 **(a)** 强势方价格处于**可调区间**内，**(b)** 在**尾盘**入场，**(c)** 价格**拉伸至短期 VWAP 之上**（`min_deviation_pct`），以及 **(d)** **正向动量**——大致来说，即**市场共识加上该代币近期上涨趋势**。

**盈利来源（如果存在的话）：** 如果强势方获胜的**真实**概率**超过**入场价格（例如支付 0.80 美元，而胜率持续高于 80%），则**期望值**可能为正。这些指标是一个**过滤器**，用于减少在盘口**震荡或均值回归**不利于强势方时入场。

**风险：** 二元市场可能**跳空**或在收盘前**翻转**。**盈亏平衡胜率 ≈ 入场价格**（未计费用）。**滑点**、**部分成交**和**预言机解析**细节可能侵蚀优势。**从小资金开始**；在可用时使用配置中的 **`simulation`** 模式。

**适用场景：** 你只需要 **BTC**，喜欢**透明的数学计算**（参见 [PROJECT_LOGIC.md](PROJECT_LOGIC.md)），并希望使用 **Rich** 终端仪表板。**不适用场景：** 你需要单进程多资产——请使用同一套件中的 **Meridian**（`up-down-spread-bot`）。

---

## 本机器人的功能

在每个周期（例如每 5 或 15 分钟，取决于配置），Polymarket 会开设一个市场，询问 BTC 在该窗口内是涨还是跌。提供两个代币：

- **UP 代币** — BTC 上涨时支付 $1.00，下跌时支付 $0.00
- **DOWN 代币** — BTC 下跌时支付 $1.00，上涨时支付 $0.00

机器人识别"强势方"（概率较高的代币），等待特定的技术条件对齐，然后买入。如果预测正确，代币解析为 $1.00 获利；如果错误，解析为 $0.00 亏损。

### 主要功能

- 使用 Rich 库的实时终端仪表板（订单簿、指标、信号、持仓、盈亏）
- 基于 VWAP 的信号生成，含偏离值和动量过滤器
- 按价格区间和时间段的历史胜率过滤
- FAK 订单执行，带重试逻辑和 WebSocket 成交确认
- 可选的对侧 $0.02 GTD 订单对冲
- 超时恢复：即使在网络超时后也能通过用户 WebSocket 检测成交
- Chainlink BTC/USD 预言机跟踪：实时 BTC 价格及与市场开盘的偏离值
- 自动链上赎回获胜头寸
- Telegram 通知，含交易提醒和权益图表
- 每笔交易的回撤跟踪与日志记录
- 持久化 JSON 格式交易历史（重启后保留）

## 项目结构

```
btc-binary-VWAP-Momentum-bot/
├── main.py                 # 主机器人：仪表板、信号、执行、所有核心逻辑
├── config.json             # 交易参数（策略、入场、对冲等）
├── .env.example            # 环境变量模板（复制为 .env）
├── requirements.txt        # Python 依赖
├── chart_pnl.py            # 盈亏图表生成器（单独运行）
├── CONFIG.md               # config.json 完整参考
├── PROJECT_LOGIC.md        # 详细技术文档（含公式）
├── docs/
│   └── README.md           # 分步入门指南（Windows + Linux）
├── data/
│   └── win_rate.csv        # 历史胜率矩阵（价格区间 x 每分钟时段；5m 使用前 5 个时段）
└── src/
    ├── __init__.py
    ├── config_loader.py    # 加载 config.json + .env，验证设置
    ├── order_executor.py   # FAK 订单下达（含重试逻辑）
    ├── hedge_manager.py    # GTD 对冲订单管理
    ├── market_finder.py    # 通过 Gamma API 发现活跃市场
    ├── position_tracker.py # 持仓和盈亏追踪
    ├── auto_redeemer.py    # 已解析头寸的链上赎回
    ├── telegram_notifier.py# Telegram 提醒和图表发送
    ├── user_websocket.py   # 用户频道 WebSocket（订单/成交追踪）
    └── websocket_client.py # 市场数据 WebSocket（价格、交易、订单簿）
```

## 安装（从头开始）

### 前提条件

- Linux 服务器（推荐 Ubuntu 22.04+）或 macOS
- Python 3.11+
- 已注资 USDC（Polygon 上）的 Polymarket 账户，用于 Gas 费的 POL，以及 API 凭证
- 交易钱包的私钥

### 步骤 1：系统设置

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git
python3 --version
```

### 步骤 2：克隆仓库

```bash
cd ~
git clone https://github.com/PolyBullLabs/polymakret-5min-15min-1hour-arbitrage-bot.git
cd polymakret-5min-15min-1hour-arbitrage-bot/btc-binary-VWAP-Momentum-bot
```

### 步骤 3：创建虚拟环境

```bash
python3 -m venv venv
source venv/bin/activate
```

### 步骤 4：安装依赖

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 步骤 5：配置环境变量

```bash
cp .env.example .env
nano .env
```

填写凭据：

| 变量 | 必填 | 说明 |
|---|---|---|
| PRIVATE_KEY | 是 | Polygon 钱包私钥（0x...） |
| FUNDER_ADDRESS | 使用代理时 | Gnosis Safe 地址（如果使用代理钱包） |
| SIGNATURE_TYPE | 使用代理时 | 0=EOA，1=Poly Proxy，2=Gnosis Safe |
| POLY_API_KEY | 是 | Polymarket CLOB API 密钥 |
| POLY_API_SECRET | 是 | Polymarket CLOB API 密钥 Secret |
| POLY_API_PASSPHRASE | 是 | Polymarket CLOB API 密钥口令 |
| RPC_URL | 推荐 | Alchemy/Infura Polygon RPC（默认：公共 RPC） |
| TELEGRAM_BOT_TOKEN | 可选 | 来自 @BotFather 的 Telegram Bot Token |
| TELEGRAM_CHAT_ID | 可选 | 你的 Telegram 用户/聊天 ID |

**如何获取 Polymarket API 凭据：**
1. 访问 https://polymarket.com 并连接钱包
2. 进入账户设置
3. 生成 API 凭据（key、secret、passphrase）
4. 这些用于 CLOB 上的 L2 身份验证

### 步骤 6：配置交易参数

```bash
nano config.json
```

参数说明见下方的配置章节。

### 步骤 7：创建日志目录

```bash
mkdir -p logs
```

### 步骤 8：运行机器人

```bash
source venv/bin/activate
python3 main.py
```

### 步骤 9：后台运行（生产环境）

```bash
sudo apt install -y tmux
tmux new -s bot

# 在 tmux 内：
source venv/bin/activate
python3 main.py

# 分离：Ctrl+B 然后 D
# 重新连接：tmux attach -t bot
```

## 配置

机器人**高度可配置**——策略、风险管理、执行、对冲和通知的每个方面都可以通过 `config.json` 微调，无需修改代码。你可以调整入场窗口、价格过滤器、指标灵敏度、投注金额等，以匹配你的风险承受能力和交易风格。

**如需完整的逐参数指南（含说明、示例和预设模板（保守/中等/激进）），请参阅 [CONFIG.md](CONFIG.md)。**

最重要的设置快速概览：

| 参数 | 默认值 | 作用 |
|---|---|---|
| `strategy.min_price` | 0.75 | 最低入场代币价格（越低风险越大，潜在利润越高） |
| `strategy.max_price` | 0.88 | 最高入场代币价格（越高越安全，利润越低） |
| `strategy.min_elapsed_sec` | 530 | 入场前等待的秒数 |
| `strategy.min_deviation_pct` | 3 | 触发信号的最低 VWAP 偏离百分比 |
| `strategy.no_entry_before_end_sec` | 335 | 在离结束还有此秒数时停止入场 |
| `entry.bet_amount_usd` | 5 | 每笔交易 USD 金额（从小开始！） |
| `entry.max_entry_price` | 0.88 | 安全硬性价格上限 |
| `hedge.enabled` | false | 对侧自动对冲 |
| `telegram.enabled` | false | 通过 Telegram 发送交易通知 |
| `web_dashboard.enabled` | false | 本地 Web UI（与终端相同的数据；JSON 接口 `/api/state`） |

当 `web_dashboard.enabled` 为 true 时，在同一台机器的浏览器中打开 **http://127.0.0.1:8765/**（或你的 `host`/`port`）。默认仅绑定本地；未经身份验证请勿公开暴露端口。

## 策略原理

### 信号生成

机器人每 250ms 评估 5 个条件。**全部**为真时才触发买入：

1. **价格在区间内**：min_price <= 强势方价格 <= max_price
2. **时间已过**：已过秒数 >= min_elapsed_sec
3. **VWAP 偏离**：min_deviation_pct < 偏离值 < max_deviation_pct
4. **正向动量**：动量 > 0%
5. **剩余时间**：剩余秒数 > no_entry_before_end_sec

### 指标

- **VWAP**（成交量加权平均价）：SUM(价格 * 成交量) / SUM(成交量)（过去 N 秒）
- **偏离值**：(最新价 - VWAP) / VWAP * 100% —— 价格偏离均值的幅度
- **动量**：(当前价格 - N 秒前价格) / N 秒前价格 * 100% —— 价格变动方向
- **Z-Score**：(价格 - 均值) / 标准差（过去 5 秒）—— 统计异常值检测

### 执行流程

```
检测到信号
  -> 下达 FAK 订单
  -> 通过 WebSocket 确认成交
  -> 记录持仓
  -> 下达对冲订单（如启用）
  -> 每 250ms 追踪回撤
  -> 市场结束（到期前 10 秒）
  -> 持仓解析，记录盈亏
  -> 获胜头寸自动链上赎回
```

### 风险

入场价格越高意味着风险越大。盈亏平衡胜率等于入场价格：

- 在 $0.75 入场需要 75% 的胜率才能盈亏平衡
- 在 $0.85 入场需要 85% 的胜率才能盈亏平衡
- 在 $0.88 入场需要 88% 的胜率才能盈亏平衡

从小额投注开始（$1-5），直到你了解行为模式。

## 日志

机器人会创建 `logs/` 目录，包含：

| 文件 | 说明 |
|---|---|
| bot.log | 主应用日志（连接、错误、BTC 价格 tick） |
| signals.log | 每次交易入场和市场结束时的完整指标快照 |
| orders.log | 详细的订单执行日志（价格、重试、成交） |
| hedges.log | 对冲订单下达和成交追踪 |
| trading_log.json | 持久化交易历史（重启后保留） |

## 生成图表

积累交易后，生成盈亏图表：

```bash
source venv/bin/activate
python3 chart_pnl.py
# 输出：logs/pnl_chart.png
```

## 文档

如需深入的技术细节（含所有公式、架构图和完整信号生成逻辑），请参阅 [PROJECT_LOGIC.md](PROJECT_LOGIC.md)。

## 免责声明

本软件仅供**教育和研究目的**使用。预测市场交易涉及**重大风险**；你可能**损失全部本金**。**不保证任何表现。** 作者和贡献者**不对**财务损失、程序错误或交易所规则变更承担责任。请在可用时使用**模拟**模式，保护好 **API 密钥和私钥**，**切勿**使用无法承受亏损的资金进行交易。如需**高级量化策略**（凯利公式、蒙特卡洛、高级 TA、仓位管理系统），请参阅[仓库 README](../README.md) 或联系 [@terauss](https://t.me/terauss)。

## 许可证

MIT
