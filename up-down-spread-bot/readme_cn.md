# Meridian — Polymarket 多资产加密交易平台

用于 Polymarket **5 分钟或 15 分钟**加密涨/跌市场的自动化执行。从**一个 Polygon 钱包**运行**四个并行交易器**（BTC、ETH、SOL、XRP），使用 **Late Entry V3**（`late_v3`）策略：仅在**最后几分钟**入场，当**价差**和**置信度**（卖盘偏斜）显示出**明显强势方**时，配合**止损**、**翻转止损**和**安全卫士**限制。

| 资源 | 链接 |
|------|------|
| **套件概述** | [仓库 README](../README.md) |
| **完整指南** | [docs/README.md](docs/README.md) |
| **GitHub** | [PolyBullLabs/polymakret-5min-15min-1hour-arbitrage-bot](https://github.com/PolyBullLabs/polymakret-5min-15min-1hour-arbitrage-bot.git) |
| **Telegram** | [@terauss](https://t.me/terauss) |

---

## 该策略为何有效（以及什么情况下会失效）

**核心理念：** 在窗口临近结束时，代币价格通常已**内含**了市场对标的物相对于执行价的看法。Meridian **跟随订单簿的强势方**（较高卖价），但**过滤**噪音：**总卖量紧缩**（流动性合理性）、**最小偏斜**（`min_confidence`）和**最高价格**上限，以避免为最后一刻的确定性支付**过高**代价。

**盈利来源（如果存在的话）：** 如果**强势方胜率**高于其**入场价格**所隐含的概率（例如在 $0.72 买入，而该状态下胜率 >72%），期望值可能为正。相对于早期入场，**更短的持仓时间**可以减少**路径风险**，但通常会导致**平均入场价格上升**。

**风险控制：** **模拟运行**、**每单和每市场上限**、**止损**（固定美元金额或本金百分比）、**翻转止损**（当持仓方失去领先地位时）、**入场冷却**以及退出前的验证价格（新鲜订单簿、卖盘总和在范围内）。精确公式见 [docs/README.md](docs/README.md)。

**适用场景：** 你需要**多个币种**、**一个钱包**、**终端 + 可选 Web 仪表板**以及**明确的退出规则**。**不适用场景：** 你需要**仅 BTC 的 VWAP/动量**过滤器——请使用 `btc-binary-VWAP-Momentum-bot`；或需要 **PTB 与现货价差**规则——请使用 `5min-15min-PTB-bot`。

---

## 功能

- **多市场交易** — 并行交易 4 种加密货币（BTC、ETH、SOL、XRP）
- **尾盘入场策略** — 在市场关闭前最后 4 分钟内入场
- **实时 WebSocket 数据** — 来自 Polymarket 的实时订单簿更新
- **自动赎回** — 市场解析后后台领取收益
- **Telegram 集成** — 用于监控、图表、余额和紧急停止的命令
- **安全卫士** — 包含订单限制和紧急停止的保护层
- **持仓追踪** — 通过 REST API 实时监控持仓
- **止损和翻转止损** — 每个币种可配置的退出策略
- **盈亏图表** — 使用 matplotlib 的可视化绩效追踪

## 架构

```
┌──────────────────────────────────────────────────────────────┐
│                       主交易循环                                │
├──────────────────────────────────────────────────────────────┤
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐         │
│  │   BTC   │  │   ETH   │  │   SOL   │  │   XRP   │         │
│  │ 交易器   │  │ 交易器   │  │ 交易器   │  │ 交易器   │         │
│  └────┬────┘  └────┬────┘  └────┬────┘  └────┬────┘         │
│       └──────────┬─┴───────────┬┘──────────┘                 │
│              ┌───┴───┐    ┌────┴────┐                        │
│              │ 订单   │    │  数据   │                        │
│              │执行器   │    │  馈送   │                        │
│              └───────┘    └─────────┘                        │
└──────────────────────────────────────────────────────────────┘
```

## 系统要求

- Python 3.10 或更高版本
- 持有 USDC（桥接版）的 Polygon 钱包
- 少量 POL/MATIC 用于 Gas 费
- Polymarket API 凭据
- VPN（如因地理限制需要）

## 安装

### 1. 克隆仓库

```bash
git clone https://github.com/PolyBullLabs/polymakret-5min-15min-1hour-arbitrage-bot.git
cd polymakret-5min-15min-1hour-arbitrage-bot/up-down-spread-bot
```

### 2. 创建虚拟环境

**重要：你必须使用虚拟环境（venv）！**

```bash
# 创建 venv
python3 -m venv venv

# 激活 venv
# Linux/macOS：
source venv/bin/activate

# Windows：
.\venv\Scripts\activate
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 配置

```bash
# 复制配置文件
cp .env.example .env
cp config/config.example.json config/config.json

# 编辑 .env 填入凭据
nano .env

# 编辑 config.json 设置交易参数
nano config/config.json
```

## 配置

### 环境变量（.env）

```env
# 钱包（必填）
PRIVATE_KEY=0x...你的私钥...

# Polygon 网络
RPC_URL=https://polygon-rpc.com
CHAIN_ID=137

# Polymarket API（必填）
CLOB_HOST=https://clob.polymarket.com
POLYMARKET_API_KEY=你的_api_key
POLYMARKET_API_SECRET=你的_api_secret
POLYMARKET_API_PASSPHRASE=你的_api_passphrase

# Telegram 通知（可选）
TELEGRAM_BOT_TOKEN=你的_bot_token
TELEGRAM_CHAT_ID=你的_chat_id
```

### 交易配置（config/config.json）

关键参数：

| 部分 | 参数 | 说明 |
|------|------|------|
| `safety.dry_run` | `true/false` | 启用模拟运行模式（不下真实订单） |
| `safety.max_order_size_usd` | `150` | 单笔最大订单金额（USD） |
| `safety.max_total_investment` | `1000` | 每个市场的最大投资额 |
| `trading.btc/eth/sol/xrp.enabled` | `true/false` | 启用/禁用特定币种 |
| `data_sources.polymarket.market_window` | `"15m"` 或 `"5m"` | **Polymarket 周期：** 15 分钟或 5 分钟涨/跌市场 |
| `strategy.entry_window_sec` | `240` | 入场窗口（最后 4 分钟） |
| `strategy.min_confidence` | `0.30` | 入场的最小价格差异 |
| `strategy.price_max` | `0.92` | 最高入场价格 |
| `exit.stop_loss.per_coin.*.value` | `-12` | 止损阈值（USD） |

## 使用方法

### 开始交易

```bash
# 激活虚拟环境
source venv/bin/activate

# 运行交易机器人
cd src
python3 main.py
```

### 键盘控制

| 按键 | 操作 |
|------|------|
| `Q` | 优雅退出 |
| `E` | 紧急停止（阻止所有交易） |

### Telegram 命令

| 命令 | 说明 |
|------|------|
| `/chart` 或 `/pnl` | 生成当前盈亏图表 |
| `/b` 或 `/balance` | 显示钱包余额（USDC + POL） |
| `/t` 或 `/positions` | 显示活跃持仓 |
| `/r` 或 `/redeem` | 赎回已完成的市场（交互式） |
| `/off` 或 `/stop` | 紧急关机（需确认） |
| `/help` | 显示所有可用命令 |

## 项目结构

```
up-down-spread-bot/
├── src/
│   ├── main.py                 # 主入口
│   ├── strategy.py             # Late Entry V3 策略
│   ├── data_feed.py            # WebSocket 数据馈送
│   ├── multi_trader.py         # 多市场交易管理器
│   ├── trader.py               # 单个交易器逻辑
│   ├── order_executor.py       # 订单执行引擎
│   ├── position_tracker.py     # 实时持仓追踪
│   ├── safety_guard.py         # 安全限制和紧急停止
│   ├── simple_redeem_collector.py  # 自动赎回收集
│   ├── telegram_notifier.py    # Telegram 机器人集成
│   ├── dashboard_multi_ab.py   # 终端仪表板
│   ├── polymarket_api.py       # Polymarket API 封装
│   ├── pnl_chart_generator.py  # 盈亏图表生成
│   ├── trade_logger.py         # 交易日志记录
│   └── keyboard_listener.py    # 键盘输入处理
├── config/
│   └── config.json             # 交易配置
├── logs/                       # 日志文件
├── requirements.txt            # Python 依赖
├── .env                        # 环境变量
└── README.md                   # 本文件
```

## 策略：尾盘入场（Late Entry V3）

Meridian 使用 Late Entry V3 / `late_v3` 入场规则：

1. **入场窗口**：仅在市场关闭前最后 4 分钟（240 秒）内入场
2. **强势方检测**：买入卖价较高的一侧（市场共识）
3. **置信度过滤器**：仅当价格差异超过 30% 时入场
4. **基于时间的仓位大小**：
   - 剩余 >180 秒：8 张合约
   - 剩余 >120 秒：10 张合约
   - 剩余 <120 秒：12 张合约
5. **退出策略**：
   - 自然收盘（市场解析）
   - 止损（每个币种可配置）
   - 翻转止损（当前持仓方变为弱势方时）

## 安全功能

- **模拟运行模式**：不下真实订单进行测试
- **订单大小限制**：每单和每市场上限
- **速率限制**：每分钟最大订单数
- **紧急停止**：键盘快捷键停止所有交易
- **投资追踪**：每市场投资限制
- **持仓持久化**：关闭时保存持仓状态

## 日志

日志存储在 `logs/` 目录中：

- `trades.jsonl` — 所有已执行交易（JSON Lines 格式）
- `orders.jsonl` — 订单执行详情
- `safety.log` — 安全卫士事件
- `session.json` — 当前会话状态
- `error.log` — 错误消息

## 故障排除

### "Rate limit exceeded"（超过速率限制）

使用私有 RPC 端点：
```env
RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/YOUR_API_KEY
```

### "Invalid signature"（无效签名）

1. 检查 API 凭据是否正确
2. 确认私钥与 Polymarket 账户匹配
3. 在 Polymarket 上重新生成 API 凭据

### WebSocket 连接断开

机器人会自动重新连接。如果持续断开：
1. 检查网络连接
2. 使用 VPN
3. 将 DNS 更改为 1.1.1.1 或 8.8.8.8

### 头寸未赎回

1. 等待预言机解析（市场关闭后 1-2 分钟）
2. 在 Telegram 中使用 `/r` 命令手动触发
3. 检查 `logs/` 目录中的错误消息

## 重要提示

1. **USDC 类型**：Polymarket 使用 USDC（桥接版），而非 USDC.e（原生版）
2. **Gas 费**：保持足够的 POL/MATIC 余额用于交易
3. **API 限制**：公共 RPC 有速率限制——建议使用私有 RPC 以保证稳定性
4. **风险**：加密货币交易涉及重大风险

## 许可证

MIT License

## 免责声明

本软件仅供**教育和研究目的**使用。预测市场交易涉及**重大风险**；你可能损失**全部**投入资金。**过往结果不保证未来表现。** 作者**不对**损失承担责任。如需**许可**、**定制策略**或**高级量化工具**（凯利公式、蒙特卡洛、马丁/反马丁框架、RSI/MACD/布林带组合、贝叶斯边缘模型等），请联系 [@terauss](https://t.me/terauss)。参见[仓库 README](../README.md)。
