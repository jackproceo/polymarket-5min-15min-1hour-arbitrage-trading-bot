# Polymarket BTC 自动交易器 — PTB、价差和概率（5 分钟 / 15 分钟）

一个用于 Polymarket **BTC 涨/跌**市场（**5 分钟**或 **15 分钟**窗口）的 Python 机器人。它融合了 **Binance** 和 **Polymarket CLOB** WebSocket，将**实时 BTC** 与 Polymarket 针对当前事件的 **PTB（price-to-beat，参考价）** 进行比较，并可在**时间**、**美元价差**和**隐含概率**规则对齐时**自动买入**。成交后，它支持**基于概率的止盈和止损**、可选的**自动赎回**（Builder relayer 流程）以及**浏览器仪表板**。

| 资源 | 链接 |
|------|------|
| **套件概述** | [仓库 README](../README.md) |
| **GitHub** | [PolyBullLabs/polymakret-5min-15min-1hour-arbitrage-bot](https://github.com/PolyBullLabs/polymakret-5min-15min-1hour-arbitrage-bot.git) |
| **Telegram** | [@terauss](https://t.me/terauss) |

---

## 该策略为何有效（以及什么情况下会失效）

**经济逻辑：** 每个市场为窗口内的 BTC 定义了一条**参考路径**（PTB）。**Chainlink**（或机器人配置的现货参考）和**结果代币价格**应随着窗口推进而**协同变动**。如果**现货**在窗口**后期**相对于 PTB**大幅偏向一侧**，而 **UP 或 DOWN 代币**仍**足够便宜**，那么基于规则的买入就能捕捉**实际价差**与**隐含概率**之间的**错位**。

**真正赚钱的因素：** 只有当**扣除费用和滑点后**，胜率和仓位大小组合使得**每笔交易的平均盈亏 > 0**时，才会出现正期望值。这需要**校准的触发条件**（你的 `CONDITION_*` 区间）或**良好的退出纪律**（止盈/止损）。**模拟**有助于评估行为；**实盘**流动性则不同。

**失败模式：** **数据馈送延迟**（Binance vs Chainlink vs Polymarket）、**过时的订单簿**、**PTB 定义细节**（`variant=fifteen` 与网站对齐）、**部分成交**以及**市场状态变化**（趋势 vs 震荡）。**始终**以 `SIMULATION_MODE=true` 开始。

---

## 风险管理

| 层面 | 作用 |
|------|------|
| **`SIMULATION_MODE`** | 使用**即时**模拟成交以订单簿价格运行规则——**无** CLOB 订单，**无**自动赎回。累计模拟盈亏记录在 `state.json` 中。 |
| **`AUTO_TRADE`** | **实盘**下单的总开关（仍受模拟模式约束）。 |
| **`TRADE_AMOUNT`** | 限制**每笔买入的 USDC 金额**——从最小值开始。 |
| **止盈 / 止损** | 入场后，在持仓代币的**概率**（0-1）空间内追踪**止盈**和**止损**——见 `config.env` 中的注释。 |
| **`MARKET_DATA_MAX_LAG_SEC`** | 当数据**过于陈旧**时跳过或保护操作。 |
| **Builder 密钥** | 用于**自动赎回**的可选密钥——像机密一样对待。 |

**操作建议：** 使用**专用钱包**、**私有 RPC**，如果网络屏蔽或限流 Polymarket 则使用**代理**。

---

## 何时使用本机器人

| 使用本机器人当…… | 考虑套件中其他机器人当…… |
|------------------|--------------------------|
| 你关注 **PTB 与 BTC 的价差**以及**明确的触发条件行**（`CONDITION_1` … `CONDITION_4`） | 你需要**多资产**尾盘共识 → **Meridian**（`up-down-spread-bot`） |
| 你想要地址 `http://localhost:5080`（默认）的 **Web 仪表板** | 你想要 **Rich 终端** + **VWAP/动量** → `btc-binary-VWAP-Momentum-bot` |
| 你首先会用 `SIMULATION_MODE=true` **模拟交易** | 你只需要**赎回**/手动工具——相应精简功能 |

---

## 功能

- **实时价格：** Binance 和 Polymarket CLOB 的 BTC 及结果代币价格。
- **自动交易：** 最多**四**组可配置的触发规则；**任一**匹配规则即可触发买入。
- **止盈/止损：** 成交后基于概率的止盈和止损。
- **自动赎回：** 可选的 Polymarket **Builder** relayer 流程用于获胜头寸。
- **仪表板：** 浏览器 UI，显示余额、持仓、历史记录、日志以及 **5m / 15m** 切换。
- **结构化日志：** `TRADING_ANALYSIS_LOG`（默认 `trading_analysis.jsonl`）——JSON Lines 格式，`schema_version: 2`，用于研究和复盘。

---

## 系统要求

- **Python** 3.8+（推荐）。
- **依赖：** `pip install -r requirements.txt`（包含 `py-clob-client`；赎回路径根据情况使用 `web3` / builder 库）。

---

## 配置（`config.env`）

### 钱包和网络

- `PRIVATE_KEY` — 签名密钥（**切勿**提交）。
- `FUNDER_ADDRESS` — 使用签名类型 1 时的代理/出资人地址。
- `POLYGON_RPC_URL` — Polygon RPC（推荐使用**私有**端点）。
- `SIGNATURE_TYPE` — 例如 `1` = Gnosis Safe，`2` = EOA（见你的设置）。

### 代理（可选）

- `HTTP_PROXY` / `HTTPS_PROXY` — 例如 `http://host:port` 或 `http://user:pass@host:port`。

### 交易设置

- `BTC_MARKET_MINUTES` — `5` 或 `15`（选择 Polymarket BTC 窗口）。PTB 使用 Polymarket 的加密货币价格 API，结合 Gamma 的事件起止时间（两个周期均使用 `variant=fifteen`，使 PTB 与网站匹配）。
- `AUTO_TRADE` — `true` / `false`（实盘订单；模拟模式处理下单时忽略此项）。
- `SIMULATION_MODE` — `true` / `false`。模拟模式：**无** CLOB 订单 / 自动赎回；盈亏记录在 `state.json` 中。
- `TRADING_ANALYSIS_LOG` — 可选路径；默认为 `polymarket_auto_trade.py` 旁边的 `trading_analysis.jsonl`。相对路径从该脚本所在目录解析。每行为 JSON，包含稳定键：`slug`、`shares_type`（UP/DOWN）、`share_price`、`share_amount`、`ptb`、`btc_price`、`difference`（BTC−PTB USD）、`status`、`take_profit` / `stop_loss`、`time`、`pnl_trade_usd`、`pnl_total_usd`、`simulation` 等。
- `TRADE_AMOUNT` — 每笔买入的 USDC 金额。

### 触发条件

- `CONDITION_1_*` … `CONDITION_4_*` — 时间窗口、与 PTB 的最小/最大价差、UP/DOWN 的概率区间（详见 `config.env` 中的行内注释）。

### 风险和循环

- `STOP_LOSS_PROB_PCT`、`TAKE_PROFIT_RR`、`TAKE_PROFIT_CAP`、`MARKET_DATA_MAX_LAG_SEC`、`LOOP_INTERVAL_SEC`、`BUY_RETRY_STEP` 等。
- `CHECK_INTERVAL` — 辅助检查间隔（如适用）。
- Builder API 密钥用于自动赎回：`POLY_BUILDER_API_KEY`、`POLY_BUILDER_SECRET`、`POLY_BUILDER_PASSPHRASE`。

---

## 运行

```bash
python polymarket_auto_trade.py
```

使用仪表板的 **Market 5m / 15m** 切换按钮或 `config.env` 中的 `BTC_MARKET_MINUTES` 来切换周期。

---

## Web 仪表板

默认地址：**http://localhost:5080**（如果绑定了外部 IP，则为 `http://<你的IP>:5080`）。

包含余额、实时价格、手动交易面板、历史记录、周期汇总和日志流。

---

## 项目结构（核心文件）

| 路径 | 作用 |
|------|------|
| `polymarket_auto_trade.py` | 主循环、数据馈送、规则、订单、仪表板服务器 |
| `config.env` | 密钥和交易开关（在你的 fork 中**加入 gitignore**） |
| `static/dashboard.html` | 仪表板 UI |
| `state.json` | 持久化的运行时/模拟盈亏状态 |
| `trading_analysis.jsonl` | 仅追加的分析日志（可选路径） |

---

## 扩展策略（联系作者）

此文件夹提供的是 **PTB / 价差 / 概率**设计。同一作者的**独立专业服务**包括高级**风险和仓位管理**（马丁格尔、反马丁格尔、斐波那契）、**技术分析**（RSI、MACD、布林带）以及**量化**工具（贝叶斯信念更新、优势 vs 市场模型、价差建模、Avellaneda–Stoikov 式库存偏斜、凯利/分数凯利、蒙特卡洛）。这些**并非全部**包含在此处——请通过 **[Telegram @terauss](https://t.me/terauss)** 联系。

---

## 免责声明

**仅供教育和研究使用。** 你全权对交易结果负责。**无担保。** 预测市场可能导致你的头寸**归零**。切勿分享**私钥**或**API 密钥**。完整的三机器人地图和风险概述请参见[仓库 README](../README.md)。
