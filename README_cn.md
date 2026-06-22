### 项目简介
PTB bot	BTC 现货价 vs PTB（基准价） 的差值	以核心数据源命名
PTB = Price-To-Beat（参考价/基准价）
它不是"BTC bot"而是"PTB bot"，因为策略核心不是交易 BTC，而是比较 BTC 与 PTB 之间的价差来决策。
PTB 是什么？
每个 Polymarket 的 BTC 涨/跌市场都会定义一个 PTB（Price-To-Beat）——一个针对该时间窗口的 BTC 参考路径价格。机器人从 Polymarket 的 crypto-price API 获取这个值：

三种机器人的本质区别
机器人	核心决策依据	命名逻辑
VWAP bot	价格 vs VWAP、动量、z-score	以技术指标命名
Meridian	尾盘订单簿价差、置信度	以产品名命名
PTB bot	BTC 现货价 vs PTB（基准价） 的差值	以核心数据源命名



本仓库是 **PolyBullLabs** 旗下的 **Polymarket 5 分钟 / 15 分钟 / 1 小时**相关 **Python 交易机器人**套件（公开部分为 **三个** 可运行子项目），面向加密 **Up/Down** 短期预测市场：含 REST/WebSocket、下单与风控范式，适合学习、二次开发与**小资金模拟**。**不构成投资建议**；请务必先 **dry-run / 模拟**，并自行承担全部风险。

- **仓库地址：** [github.com/PolyBullLabs/polymarket-5min-15min-1hour-arbitrage-trading-bot](https://github.com/PolyBullLabs/polymarket-5min-15min-1hour-arbitrage-trading-bot)  
- **联系：** [Telegram @terauss](https://t.me/terauss)

### 本仓库内的机器人（公开目录）

| 目录 | 侧重点 | 市场 | 核心思路 |
|------|--------|------|----------|
| [`btc-binary-VWAP-Momentum-bot/`](btc-binary-VWAP-Momentum-bot/) | VWAP、偏离、动量、z-score | BTC **5m** 或 **15m** | 在**较晚的窄窗口**内，当价格**站上 VWAP** 且**动量为正**时倾向**强势侧**；强调“共识 + 短线延续”过滤。 |
| [`up-down-spread-bot/`](up-down-spread-bot/)（**Meridian**） | Late Entry V3（`late_v3`） | BTC、ETH、SOL、XRP — **5m** 或 **15m** | **尾盘**若点差与**置信度（卖盘偏斜等）**过关，则跟随订单簿已偏向的一侧；**止损 / 翻转止损**在到期前截断劣路径。 |
| [`5min-15min-PTB-bot/`](5min-15min-PTB-bot/) | PTB 差值 + 概率触发 | BTC **5m** 或 **15m** | 对比**链上/现货 BTC** 与 Polymarket **PTB（price-to-beat）**；在**时间、价差、隐含概率**共振时触发；可用 **TP/SL** 管理仓位。 |

### 附加策略目录（与上表一致 · EN/中文）

与英文章节 **[Additional strategy catalog](#additional-strategy-catalog)** 相同，便于中文读者直接阅读：

| Strategy (EN) | 策略（中文） | Idea (EN) | 思路（中文） |
|----------------|-------------|-----------|-------------|
| **1c buy** | **1 美分买入** | Seek **ultra-cheap** bids (near **$0.01**) when microstructure or book updates create dislocations; **high variance**, needs **hard notional caps** and kill switches. | 在盘口/流动性短暂失衡时捕捉**极低价**（约 **1 美分**）一侧；**方差极大**，必须配合**严格资金上限**与熔断。 |
| **99c sniper** | **99 美分狙击** | Target **near-resolution** asks around **$0.99** when you believe the outcome is effectively settled but liquidity still prints; **tail risk** if the market flips. | 在临近结算、认为结果已高度确定时参与**约 99 美分**一侧；若结果反转则**尾部风险**显著。 |
| **Low-side dual reversion** | **弱势双边均值回归** | Work **both** underdog sides when prices are **compressed**, betting on **mean reversion** or late **volatility expansion** with paired risk controls. | 在两侧价格**受压**时，对**弱势/低价**双边做**均值回归**或博弈尾盘**波动放大**，需配对风控。 |
| **Pre-order market** | **预挂单 / 盘前布局** | Place **limits ahead** of the active window (or refresh ladders early) to **shape queue position** before competing flow arrives. | 在窗口正式激烈博弈**之前**挂出限价或刷新阶梯，以**排队与价位**占据主动。 |
| **Cross-market bot** | **跨市场机器人** | Link **two or more** related markets (same asset, different horizons, or correlated events) for **hedge**, **spread**, or **arbitrage-style** books. | 将**多个相关市场**（同资产不同周期、或相关事件）联动，做**对冲、价差或类套利**组合。 |
| **Martingale & anti-martingale @ ~45c** | **约 45¢ 马丁 / 反马丁** | Around **mid prices (~45¢)**, either **add on adverse moves** (martingale-style, very dangerous) or **pyramid into strength / cut into weakness** (anti-martingale); must be **regime-gated**. | 在**中段价位（约 45 美分）**附近，按规则做**亏损加仓（马丁，极高风险）**或**顺势加码 / 逆势减仓（反马丁）**；必须有**行情过滤**与**爆仓防护**。 |
| **Fibonacci strategy bot** | **斐波那契策略机器人** | Size entries or grids using **Fibonacci retracement/extension** levels relative to a swing anchor; combines **TA structure** with prediction-market **binary payoffs**. | 以波段锚点计算**斐波那契回撤/扩展**位，用于**分批建仓或网格**；将**技术结构**与二元**盈亏不对称**结合。 |
| **Binary momentum (MACD, RSI, VWAP)** | **二元动量（MACD / RSI / VWAP）** | **Momentum** stack on binary Up/Down: **MACD** for trend impulse, **RSI** for stretch / mean-revert filter, **VWAP** for intraday fair value—similar spirit to the shipped VWAP bot but **multi-indicator**. | 在二元 Up/Down 上叠加**动量**：**MACD** 看趋势动能，**RSI** 看过热/回调过滤，**VWAP** 看日内公允；理念接近本仓库 VWAP 机器人但为**多指标融合**。 |
| **Dump-hedge** | **急跌对冲** | Detect a **sharp dump**, leg in, then **hedge** the other side when **combined pair cost** clears your edge threshold (see also the **Rust 15m** repo in [Related Rust Polymarket bots](#related-rust-polymarket-bots-poly-tutor)). | 识别**急跌**后先进一侧，再在**组合成本**达标时**对冲**另一侧锁定结构（亦可对照英文区 **Rust 15m** 仓库的 **dump-and-hedge** 描述）。 |

### 克隆与安装（摘要）

```bash
git clone https://github.com/PolyBullLabs/polymarket-5min-15min-1hour-arbitrage-trading-bot.git
cd polymarket-5min-15min-1hour-arbitrage-trading-bot
cd btc-binary-VWAP-Momentum-bot   # 或: up-down-spread-bot / 5min-15min-PTB-bot
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

各子目录另有 **README** 与示例环境变量，请**勿将密钥提交到 Git**。

### 相关 Rust 仓库（Poly-Tutor）

英文主文档见 **[Related Rust Polymarket bots (Poly-Tutor)](#related-rust-polymarket-bots-poly-tutor)**：含 **5m / 15m / 1h** 的 **pair-cost、dump-hedge、限价+merge** 等 Rust 实现链接与 `git clone` 命令。

### 免责声明（中文）

预测市场交易可能导致**本金全部亏损**。本仓库内容仅供**教育与实验**；作者与贡献者**不对**因使用代码产生的交易亏损、程序错误、交易所或协议规则变更、以及您所在司法辖区的合规问题承担责任。**非投资建议。无担保。** 在充分理解行为与风险前，请持续使用**模拟 / 小资金**。

### 联系（中文）

- **Telegram：** [@terauss](https://t.me/terauss)

---

**If this Polymarket trading bot toolkit is useful, please star the repo** to improve discoverability for other builders and traders—and open issues if you want to contribute improvements to docs or code.

**若本仓库对您有帮助，欢迎点 Star**，方便其他开发者与交易者发现本项目；也欢迎通过 Issue 贡献文档与代码改进。
