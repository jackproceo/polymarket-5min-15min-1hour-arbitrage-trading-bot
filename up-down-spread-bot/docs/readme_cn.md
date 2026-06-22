# Meridian — Polymarket 多资产加密交易平台（完整指南）

**Meridian** 是本 Python 系统的产品名称：它并行交易 **Polymarket 5 分钟或 15 分钟**加密涨/跌市场（BTC、ETH、SOL、XRP），使用**尾盘入场**模型（实现方式：**Late Entry V3** / `late_v3`）。

| 套件 | [github.com/PolyBullLabs/polymakret-5min-15min-1hour-arbitrage-bot](https://github.com/PolyBullLabs/polymakret-5min-15min-1hour-arbitrage-bot) · [@terauss](https://t.me/terauss) · [顶层 README](../../README.md) |

**教育用途：** 本指南解释**机制**、**风险**和**配置**。**非投资建议。不保证任何优势。**

---

## 目录

1. [这个机器人是什么？](#1-这个机器人是什么)
2. [Polymarket 15 分钟市场如何运作](#2-polymarket-15-分钟市场如何运作)
3. [如何运行机器人（逐步）](#3-如何运行机器人逐步)
4. [策略：Late Entry V3（详细）](#4-策略late-entry-v3详细)
5. [退出机制（详细）](#5-退出机制详细)
6. [订单执行（详细）](#6-订单执行详细)
7. [数据馈送和 WebSocket](#7-数据馈送和-websocket)
8. [配置参考](#8-配置参考)
9. [环境变量参考](#9-环境变量参考)
10. [仪表板和终端 UI](#10-仪表板和终端-ui)
11. [Web 仪表板（浏览器）](#11-web-仪表板浏览器)
12. [Telegram 集成](#12-telegram-集成)
13. [安全功能](#13-安全功能)
14. [项目结构](#14-项目结构)
15. [故障排除](#15-故障排除)

---

## 1. 这个机器人是什么？

**Meridian** 运行**四个并行交易器**——每种币一个（BTC、ETH、SOL、XRP）——共享一个 Polygon 钱包。每个交易器独立监控自己的 Polymarket 15 分钟预测市场并做出买卖决策。

**交易对象：** Polymarket 市场上条件性结果代币（UP vs DOWN），slug 如 `btc-updown-15m-1711234500`。

**核心理念：** 等待 15 分钟市场窗口的最后 4 分钟，识别市场偏向哪一侧（UP 或 DOWN），然后买入强势方。然后持有至市场解析（并收取收益），或者如果头寸变差则提前退出。

---

## 2. Polymarket 15 分钟市场如何运作

Polymarket 提供每 15 分钟解析一次的加密预测市场：

- 每 15 分钟开一个新的市场（按 epoch 对齐：整点过 00、15、30、45 分钟）。
- 每个市场有两个结果：**UP**（价格上涨）和 **DOWN**（价格下跌）。
- 每个结果代币在 $0.00 到 $1.00 之间交易。
- 市场解析时，获胜代币支付 **$1.00**，失败代币支付 **$0.00**。

**示例时间线：**

```
12:00 ─── 市场开盘："BTC 会在接下来 15 分钟内上涨吗？"
         UP 卖价：$0.50，DOWN 卖价：$0.50（尚无共识）
         ...
12:11 ─── 尾盘入场窗口开始（收盘前 4 分钟）
         UP 卖价：$0.72，DOWN 卖价：$0.33（市场认为 UP）
         机器人以 $0.72 买入 10 张 UP 代币 → 成本 = $7.20
         ...
12:15 ─── 市场解析
         BTC 上涨 → UP 获胜 → 10 张代币 x $1.00 = $10.00
         利润 = $10.00 - $7.20 = $2.80
```

---

## 3. 如何运行机器人（逐步）

### 先决条件

- Python 3.10 或更高版本
- 持有 **USDC（桥接版）** 的 Polygon 钱包——不是 USDC.e（原生版）
- 少量 **POL/MATIC** 用于 Gas 费
- Polymarket API 凭据（API key、secret、passphrase）
- 如果你所在地区有地理限制，需要 VPN

### 步骤 1：克隆仓库

```bash
git clone https://github.com/PolyBullLabs/polymakret-5min-15min-1hour-arbitrage-bot.git
cd polymakret-5min-15min-1hour-arbitrage-bot/up-down-spread-bot
```

### 步骤 2：创建虚拟环境

```bash
# 创建
python -m venv venv

# 激活（Windows）
.\venv\Scripts\activate

# 激活（Linux/macOS）
source venv/bin/activate
```

### 步骤 3：安装依赖

```bash
pip install -r requirements.txt
```

### 步骤 4：设置环境变量

```bash
# 复制示例文件
cp .env.example .env       # Linux/macOS
copy .env.example .env     # Windows
```

打开 `.env` 并填写你的凭据：

```env
# 必填 — 你的 Polygon 钱包私钥
PRIVATE_KEY=0x你的私钥在此

# Polygon 网络
RPC_URL=https://polygon-rpc.com
CHAIN_ID=137

# 必填 — Polymarket API 凭据
CLOB_HOST=https://clob.polymarket.com
POLYMARKET_API_KEY=你的_api_key
POLYMARKET_API_SECRET=你的_api_secret
POLYMARKET_API_PASSPHRASE=你的_api_passphrase

# 可选 — Telegram 通知
TELEGRAM_BOT_TOKEN=你的_bot_token
TELEGRAM_CHAT_ID=你的_chat_id
```

### 步骤 5：设置配置

```bash
# 复制示例配置
cp config/config.example.json config/config.json       # Linux/macOS
copy config\config.example.json config\config.json     # Windows
```

打开 `config/config.json` 并查看/调整设置。初次用户最重要的设置：

```json
{
  "safety": {
    "dry_run": true    // <-- 保持 TRUE 以在不使用真实资金的情况下测试
  }
}
```

### 步骤 6：运行机器人

```bash
cd src
python main.py
```

带**浏览器仪表板**（设置编辑器 + 实时分析，在 **http://127.0.0.1:5050**）：

```bash
cd src
python main.py --web
```

机器人将：
1. 验证配置和仓位大小公式
2. 连接 Polymarket WebSocket 获取实时订单簿数据
3. 在终端中启动仪表板
4. 开始监控市场并在条件满足时交易

### 步骤 7：切换到实盘交易（准备就绪后）

一旦你确认机器人在模拟运行模式下正常工作：

1. 编辑 `config/config.json`
2. 将 `"dry_run": true` 改为 `"dry_run": false`
3. 重启机器人

---

## 4. 策略：Late Entry V3（详细）

Late Entry V3 策略是一种**动量跟随、受时间约束**的策略。它仅在市场接近到期时入场，押注**市场偏爱的方向**（市场已经倾向的一侧）。

### 为什么要"尾盘入场"？

通过等到最后 4 分钟，机器人：
- 获得更清晰的信号，了解哪一方可能获胜（更高的置信度）
- 减少暴露在风险中的时间
- 避免在结果不确定的市场中期支付过高的价格

### 入场决策流程（逐步）

以下是机器人每次评估是否买入时发生的具体情况：

#### 步骤 1：检查时间窗口

```
是否 距离结束秒数 > 0 且 距离结束秒数 <= 240？
  是 → 继续
  否 → 跳过（太早或市场已关闭）
```

机器人仅在每个 15 分钟窗口的**最后 240 秒**（4 分钟）内交易。

**示例：** 市场在 12:15:00 收盘。入场窗口为 12:11:00 至 12:15:00。

#### 步骤 2：检查入场频率

```
距离上次入场是否已过去至少 7 秒？
  是 → 继续
  否 → 跳过（太快，等待）
```

这防止机器人发送过多订单。默认：每个市场每 7 秒一次入场尝试。

#### 步骤 3：计算价差

```
价差 = up_卖价 + down_卖价
价差是否 <= 1.05 且 价差 > 0？
  是 → 继续
  否 → 跳过（价差太大，订单簿不可靠）
```

**示例：**
- UP 卖价 = $0.72，DOWN 卖价 = $0.30 → 价差 = 1.02（良好）
- UP 卖价 = $0.80，DOWN 卖价 = $0.35 → 价差 = 1.15（太宽，跳过）

在健康市场中，UP 和 DOWN 卖价之和接近 $1.00。价差远高于 $1.05 意味着订单簿稀薄或过时。

#### 步骤 4：计算置信度

```
置信度 = |up_卖价 - down_卖价|
置信度是否 >= 0.30？
  是 → 继续
  否 → 跳过（市场共识不足）
```

**示例：**
- UP 卖价 = $0.72，DOWN 卖价 = $0.30 → 置信度 = 0.42（良好，市场明显偏向 UP）
- UP 卖价 = $0.55，DOWN 卖价 = $0.48 → 置信度 = 0.07（太接近，跳过）

置信度 0.30 意味着市场对一侧的定价至少比另一侧高 30 美分——一个强烈的倾向。

#### 步骤 5：识别强势方

```
如果 up_卖价 > down_卖价 → 强势方 = UP，价格 = up_卖价
如果 down_卖价 > up_卖价 → 强势方 = DOWN，价格 = down_卖价
```

机器人总是买入**卖价更高**的一侧——市场共识认为会获胜的一方。

#### 步骤 6：检查价格上限

```
强势方价格是否 <= 0.92？
  是 → 继续
  否 → 跳过（太贵，风险收益比太差）
```

**示例：**
- UP 卖价 = $0.88 → 买入（如果获胜仍有利润：$1.00 - $0.88 = 每张代币 $0.12 利润）
- UP 卖价 = $0.95 → 跳过（只有 $0.05 的潜在利润，不值得冒险）

#### 步骤 7：检查投资上限

```
该市场总投资是否 < $300？
  是 → 继续
  否 → 跳过（已达到每市场最高投资额）
```

这防止在单个市场中过度集中。

#### 步骤 8：计算仓位大小（合约数）

合约数量取决于剩余时间：

| 剩余时间 | 合约数 |
|----------|--------|
| > 180 秒 | 8 张合约 |
| > 120 秒 | 10 张合约 |
| <= 120 秒 | 12 张合约 |

**为什么越晚仓位越大？** 越接近到期，价格越"确定"，信号越强，因此机器人采取更大的仓位。

**示例：**
- 剩余 200 秒，UP 卖价 = $0.72 → 买入 8 张 UP 合约 → 成本 = 8 x $0.72 = $5.76
- 剩余 100 秒，UP 卖价 = $0.78 → 买入 12 张 UP 合约 → 成本 = 12 x $0.78 = $9.36

### 完整入场示例

```
市场：btc-updown-15m-1711234500（12:15:00 收盘）
时间：12:12:30（剩余 150 秒）

订单簿：
  UP  卖价：$0.72
  DOWN 卖价：$0.30

步骤 1：剩余 150s，在 [0, 240] 内 → 通过
步骤 2：上次入场在 10s 前，>= 7s → 通过
步骤 3：价差 = 0.72 + 0.30 = 1.02 <= 1.05 → 通过
步骤 4：置信度 = |0.72 - 0.30| = 0.42 >= 0.30 → 通过
步骤 5：强势方 = UP（0.72 > 0.30），价格 = $0.72
步骤 6：价格 $0.72 <= $0.92 → 通过
步骤 7：总投资 = $5.76 < $300 → 通过
步骤 8：剩余 150s（> 120s）→ 10 张合约

信号：以 $0.72 买入 10 张 UP 合约
成本：10 x $0.72 = $7.20
```

---

## 5. 退出机制（详细）

机器人退出头寸的三种方式：

### 退出方式 1：自然解析（持有至到期）

这是默认方式。如果没有触发止损或翻转止损，机器人持有至市场解析：

- **获胜方**：代币支付每张 $1.00。机器人自动赎回。
- **失败方**：代币变得一文不值（$0.00）。

**示例：**
```
买入：10 张 UP 合约，价格 $0.72 → 成本 $7.20
市场解析：BTC 上涨
收益：10 x $1.00 = $10.00
利润：$10.00 - $7.20 = $2.80（+38.9%）
```

```
买入：10 张 UP 合约，价格 $0.72 → 成本 $7.20
市场解析：BTC 下跌
收益：10 x $0.00 = $0.00
亏损：$0.00 - $7.20 = -$7.20（-100%）
```

### 退出方式 2：每个币种的止损

每种币有自己的止损阈值。当**未实现盈亏**（按市值计价的亏损）达到阈值时，机器人立即卖出。

**未实现盈亏的计算方式：**

```
总价值 = (up_数量 x up_卖价) + (down_数量 x down_卖价)
未实现盈亏 = 总价值 - 总投资
```

**两种止损类型：**

| 类型 | 配置 | 触发条件 |
|------|------|----------|
| **固定金额** | `"type": "fixed", "value": -12.0` | `未实现盈亏 <= -$12.00` |
| **百分比** | `"type": "percent", "value": 15` | `未实现盈亏 <= -(投资的 15%)` |

**默认配置：** BTC/ETH/SOL 使用固定 -$12，XRP 使用固定 -$11。

**示例（固定止损）：**
```
持仓：15 张 UP 合约，均价 $0.70 → 总投资 = $10.50
当前：UP 卖价 = $0.40，DOWN 卖价 = $0.55
价值：15 x $0.40 + 0 x $0.55 = $6.00
未实现盈亏：$6.00 - $10.50 = -$4.50

止损阈值：-$12.00
-$4.50 > -$12.00 → 尚未触发

随后... UP 卖价跌至 $0.15：
价值：15 x $0.15 = $2.25
未实现盈亏：$2.25 - $10.50 = -$8.25

-$8.25 > -$12.00 → 仍未触发

随后... UP 卖价跌至 $0.05：
价值：15 x $0.05 = $0.75
未实现盈亏：$0.75 - $10.50 = -$9.75

-$9.75 > -$12.00 → 仍未触发

（这说明对于 $10.50 这样的小仓位，
 $12 的固定止损可能在到期前永远不会触发）
```

**示例（百分比止损）：**
```
持仓：20 张 UP 合约，均价 $0.70 → 总投资 = $14.00
止损：15% → 阈值 = -(14.00 x 0.15) = -$2.10

UP 卖价跌至 $0.60：
价值：20 x $0.60 = $12.00
未实现盈亏：$12.00 - $14.00 = -$2.00
-$2.00 > -$2.10 → 未触发

UP 卖价跌至 $0.59：
价值：20 x $0.59 = $11.80
未实现盈亏：$11.80 - $14.00 = -$2.20
-$2.20 <= -$2.10 → 止损触发 → 卖出所有 UP 代币
```

### 退出方式 3：翻转止损（价格反转保护）

翻转止损检测到市场情绪已经**反转**，不利于你的持仓。如果你持有 UP 代币且 UP 价格跌至翻转阈值以下，机器人立即卖出。

**工作原理：**

```
我方方向 = 我们持有更多合约的一侧
我方价格 = 我方方向的当前卖价

如果 我方价格 <= 翻转止损价格（默认 $0.48）→ 触发
```

**为什么是 $0.48？** 如果 UP 在你买入时交易在 $0.72，而现在只有 $0.48，市场不再认为 UP 是强势方。情绪已经翻转。

**示例：**
```
买入：10 张 UP 合约，价格 $0.72

随后：UP 卖价跌至 $0.52，DOWN 卖价升至 $0.50
我方价格（$0.52）> $0.48 → 未触发

随后：UP 卖价跌至 $0.47，DOWN 卖价升至 $0.55
我方价格（$0.47）<= $0.48 → 翻转止损触发
→ 机器人立即卖出所有 UP 代币
→ 保留剩余价值，而不是一路跌到 $0.00
```

### 退出前的价格验证

在检查止损或翻转止损之前，机器人验证价格是否可靠：

1. UP 和 DOWN 卖价必须**新鲜**（在最近 2 秒内更新）
2. UP 和 DOWN 的时间戳必须**在 2 秒内**相互匹配
3. `up_卖价 + down_卖价` 必须在 **$0.95 和 $1.15 之间**

如果任何检查失败，该 tick 跳过退出决策（以避免基于过时/错误数据进行卖出）。

---

## 6. 订单执行（详细）

### 买入：FAK 订单（立即成交否则取消）

FAK（Fill-And-Kill）订单尝试立即对现有订单簿流动性成交。未成交部分被取消。

**买入流程：**

1. 计算激进价格：`卖价 x 1.05`（卖价上浮 5% 作为滑点容忍度）
2. 提交全额合约数量的 FAK 订单
3. 如果部分成交，对剩余数量再提交一个 FAK
4. 最多重复 3 次（可配置）
5. 当请求合约的 >= 98% 已成交，或剩余价值 < $1.00 时停止

**示例：**
```
需求：10 张合约，卖价 $0.72
激进价格：$0.72 x 1.05 = $0.756 → 向上取整至 $0.76

尝试 1：FAK 订单，10 张合约，价格 $0.76
  结果：7 张以 $0.72-$0.74 成交
  剩余：3 张合约

尝试 2：FAK 订单，3 张合约，价格 $0.76
  结果：3 张以 $0.73 成交
  剩余：0

总计：10/10 成交（100%）→ 完成
```

### 卖出：FOK 分块（分块立即成交否则取消）

当机器人需要退出（止损、翻转止损或市场收盘）时，它分块卖出：

1. 从区块链读取实际的 ERC-20 代币余额
2. 分成每块 50 张合约
3. 每个块：以 $0.01 的 FOK 订单（以任意价格卖出）
4. 每个块最多重试 5 次
5. 所有块完成后：扫除任何剩余的零碎

**为什么是 $0.01？** 这是"市价卖出"——接受任何价格以快速退出。在紧急退出中，速度比获取最佳价格更重要。

**示例：**
```
持仓：120 张 UP 代币待卖出

块 1：FOK 卖出 50 张，价格 $0.01 → 成交
块 2：FOK 卖出 50 张，价格 $0.01 → 成交
块 3：FOK 卖出 20 张，价格 $0.01 → 成交
扫除：剩余 0 → 完成

总计卖出：120 张代币
```

### 零碎扫除

卖出后，可能残留微小的零碎余额（例如 0.3 张代币）。机器人运行多阶段扫除：

1. **FOK 扫除**：最多尝试 3 次
2. **FAK 回退**：最多尝试 3 次
3. **GTC 订单**：以 $0.01 下达有效直至取消的订单
4. **延迟扫除**：等待 1 秒，重新检查余额，再次尝试 FOK/FAK

### 自动赎回

市场解析后，获胜代币可以按每张 $1.00 赎回。机器人运行一个后台线程：

- **首次检查**：启动后 8 分钟
- **后续检查**：每 5 分钟
- 查询 Polymarket API 获取可赎回的头寸
- 使用可配置的 Gas 设置自动赎回

---

## 7. 数据馈送和 WebSocket

### 市场数据流转

```
Polymarket Gamma API              Polymarket WebSocket
（REST - 市场元数据）             （实时订单簿）
        │                                  │
        ▼                                  ▼
   _fetch_tokens()                  on_message()
   获取代币 ID、                     解析 "book" 事件，
   条件 ID                           提取最佳卖价/买价
        │                                  │
        └────────────┬─────────────────────┘
                     ▼
              DataFeed 对象
              (up_卖价, down_卖价, 时间戳)
                     │
                     ▼
            on_price_update 回调
            （每次订单簿更新时调用）
                     │
                     ▼
         策略 → 入场/退出决策
```

### 市场时槽计算

市场对齐到固定长度的 epoch 边界。长度为 **`data_sources.polymarket.market_interval_sec`**（默认 **900** = 15 分钟；**300** = 5 分钟）：

```
间隔 = 900   # 5m 为 300
当前时间 = 1711234567（Unix 时间戳）
时槽 = (当前时间 // 间隔) * 间隔
市场_slug = "btc-updown-15m-<时槽>"   # 或 "btc-updown-5m-<时槽>"
市场结束 = 时槽 + 间隔
```

### WebSocket 重连

当一个市场到期时，机器人自动：
1. 计算配置间隔的下一个时槽
2. 从 Gamma API 获取新的代币 ID
3. 使用新的资产 ID 重连 WebSocket
4. 计时器为新的市场窗口重置

---

## 8. 配置参考

所有设置位于 `config/config.json`。以下是每个部分：

### `safety` — 风险控制

| 键 | 默认值 | 说明 |
|-----|---------|-------------|
| `dry_run` | `true` | 如果为 true，模拟交易而不使用真实资金 |
| `max_order_size_usd` | `150` | 单个最大订单金额（合约数 x 价格） |
| `max_orders_per_minute` | `100` | 订单速率限制 |
| `max_total_investment` | `1000` | 每个市场 slug 的累计最大投资额 |

### `trading` — 币种启用/禁用

| 键 | 默认值 | 说明 |
|-----|---------|-------------|
| `btc.enabled` | `true` | 启用 BTC 交易 |
| `eth.enabled` | `true` | 启用 ETH 交易 |
| `sol.enabled` | `true` | 启用 SOL 交易 |
| `xrp.enabled` | `false` | 启用 XRP 交易（默认禁用——流动性较低） |

### `strategy` — 入场参数

| 键 | 默认值 | 说明 |
|-----|---------|-------------|
| `name` | `late_entry_v3` | 策略标识 |
| `entry_window_sec` | `240` | 仅在市场最后 N 秒内入场 |
| `entry_frequency_sec` | `7` | 同一市场中入场之间的最小秒数 |
| `min_confidence` | `0.30` | 入场所需的最小 \|up_卖价 - down_卖价\| |
| `max_spread` | `1.05` | 允许的最大 up_卖价 + down_卖价 |
| `price_max` | `0.92` | 为强势方支付的最高价格 |
| `max_investment_per_market` | `300` | 每市场最大投资额（USD） |
| `sizing.above_180_sec` | `8` | 剩余 > 180s 时的合约数 |
| `sizing.above_120_sec` | `10` | 剩余 > 120s 时的合约数 |
| `sizing.below_120_sec` | `12` | 剩余 <= 120s 时的合约数 |

### `exit.flip_stop` — 翻转止损设置

| 键 | 默认值 | 说明 |
|-----|---------|-------------|
| `enabled` | `true` | 启用翻转止损退出 |
| `price_threshold` | `0.48` | 我方方向卖价跌至此值时卖出 |
| `check_realtime` | `true` | 在每次价格更新时检查 |

### `exit.stop_loss` — 每个币种的止损

| 键 | 默认值 | 说明 |
|-----|---------|-------------|
| `enabled` | `true` | 启用止损 |
| `per_coin.btc.type` | `fixed` | `fixed` = 美元金额，`percent` = 投资的百分比 |
| `per_coin.btc.value` | `-12.0` | 阈值（负值 = 亏损金额） |
| `per_coin.eth.value` | `-12.0` | ETH 止损阈值 |
| `per_coin.sol.value` | `-12.0` | SOL 止损阈值 |
| `per_coin.xrp.value` | `-11.0` | XRP 止损阈值 |

### `execution.buy` — 买单设置

| 键 | 默认值 | 说明 |
|-----|---------|-------------|
| `order_type` | `FAK` | 立即成交否则取消订单类型 |
| `max_fak_attempts` | `3` | 每次买入的重试次数 |
| `retry_delay_sec` | `0.3` | 重试之间的延迟 |
| `min_order_usd` | `1.0` | 继续下单的最低剩余订单价值 |
| `target_fill_percent` | `98.0` | 成交率达到此百分比时停止重试 |

### `execution.sell` — 卖单设置

| 键 | 默认值 | 说明 |
|-----|---------|-------------|
| `strategy` | `FOK_CHUNKED` | 分块 FOK 卖出 |
| `chunk_size` | `50` | 每块合约数 |
| `chunk_delay_sec` | `0.1` | 块之间的延迟 |
| `max_chunk_retries` | `5` | 每块重试次数 |
| `price` | `0.01` | 卖出价格（市价卖出） |
| `min_dust_threshold` | `0.1` | 低于此值的余额忽略 |
| `sweep_max_attempts` | `3` | FOK 扫除重试次数 |
| `sweep_enable_fallback` | `true` | 启用 FAK/GTC 回退 |
| `delayed_sweep_enabled` | `true` | 延迟后重新检查余额 |
| `delayed_sweep_delay_sec` | `1` | 延迟扫除前等待的秒数 |

### `execution.redeem` — 赎回设置

| 键 | 默认值 | 说明 |
|-----|---------|-------------|
| `startup_check_delay_sec` | `60` | 首次赎回检查前的等待时间 |
| `first_check_delay_sec` | `480` | 首次常规检查（启动后 8 分钟） |
| `check_interval_sec` | `300` | 之后每 5 分钟检查一次 |
| `sizeThreshold` | `0.1` | 赎回的最低代币余额 |
| `gas_limit` | `500000` | 赎回交易的 Gas 限制 |
| `gas_price_multiplier` | `1.5` | Gas 价格倍数 |

### `execution.rpc_config` — Polygon RPC

| 键 | 默认值 | 说明 |
|-----|---------|-------------|
| `endpoints` | `["https://polygon-rpc.com"]` | RPC 端点 |
| `retry_attempts` | `10` | RPC 调用的最大重试次数 |
| `enable_parallel_requests` | `true` | 并行查询多个端点 |

### `data_sources.polymarket` — 市场窗口长度

| 键 | 默认值 | 说明 |
|-----|---------|-------------|
| `gamma_api` | `https://gamma-api.polymarket.com` | Gamma REST 基础 URL |
| `ws_url` | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | 订单簿 WebSocket |
| **`market_window`** | **`"15m"`** | **在此选择：** `"5m"` = 5 分钟涨/跌（`{coin}-updown-5m-{slot}`），`"15m"` = 15 分钟（`{coin}-updown-15m-{slot}`） |
| `market_interval_sec` | （派生） | 从 `market_window` 自动设置（300 或 900）。如果你需要原始秒数，可以设置此值代替 `market_window`；如果两者都设置，**`market_window` 优先**。 |

对于 **5 分钟**市场，设置 `"market_window": "5m"`，并根据需要调整 `strategy.entry_window_sec`（例如 **90–120** 秒）。策略会自动将基于时间的大小层级缩放到更短的窗口（例如 5m 的 60s/40s）。

### `display` — 终端仪表板

| 键 | 默认值 | 说明 |
|-----|---------|-------------|
| `width` | `160` | 仪表板宽度（字符数） |
| `update_interval` | `1` | 仪表板刷新间隔（秒） |

### `logging` — 日志文件路径

| 键 | 默认值 | 说明 |
|-----|---------|-------------|
| `trades_file` | `logs/trades.jsonl` | 交易日志（JSON Lines） |
| `session_file` | `logs/session.json` | 会话状态文件 |

---

## 9. 环境变量参考

所有环境变量在项目根目录的 `.env` 文件中设置。

### 实盘交易必填

| 变量 | 示例 | 说明 |
|--------|---------|-------------|
| `PRIVATE_KEY` | `0xabcdef...` | Polygon 钱包私钥（0x 后 64 位十六进制字符） |
| `RPC_URL` | `https://polygon-rpc.com` | Polygon RPC 端点 |
| `CHAIN_ID` | `137` | Polygon 链 ID（始终为 137） |
| `CLOB_HOST` | `https://clob.polymarket.com` | Polymarket CLOB API 主机 |
| `POLYMARKET_API_KEY` | `abc-123-...` | 你的 Polymarket API 密钥 |
| `POLYMARKET_API_SECRET` | `base64string==` | 你的 Polymarket API 密钥 Secret |
| `POLYMARKET_API_PASSPHRASE` | `passphrase` | 你的 Polymarket API 密钥口令 |

### 可选

| 变量 | 说明 |
|--------|-------------|
| `TELEGRAM_BOT_TOKEN` | 用于通知的 Telegram Bot Token |
| `TELEGRAM_CHAT_ID` | 接收消息的 Telegram 聊天 ID |

### 如何获取 Polymarket API 凭据

1. 访问 [Polymarket](https://polymarket.com) 并登录
2. 进入你的账户设置
3. 生成 API 凭据（key、secret、passphrase）
4. 你的私钥是与你的 Polymarket 账户关联的 Polygon 钱包密钥

---

## 10. 仪表板和终端 UI

运行时，机器人显示实时终端仪表板（每秒刷新）：

```
═══════════════════════════════════════════════════════════════════
 运行时间：00:45:23  |  BTC | ETH | SOL | XRP  (Polymarket 订单簿)
═══════════════════════════════════════════════════════════════════

 策略：late_v3
 余额：$487.32  |  交易：12  |  胜/负：8/4  |  胜率：66.7%

 BTC  [15m-1711234500]  时间：142s   UP：$0.72  DN：$0.30  强势方：UP  置信度：0.42
      持仓：10 UP @ $0.72  |  盈亏：+$1.20  |  最大回撤：-$0.80
      如果 UP 赢：+$2.80  |  如果 DN 赢：-$7.20

 ETH  [15m-1711234500]  时间：142s   UP：$0.65  DN：$0.38  强势方：UP  置信度：0.27
      （等待置信度 >= 0.30）

 SOL  [15m-1711234500]  时间：142s   UP：$0.80  DN：$0.22  强势方：UP  置信度：0.58
      持仓：12 UP @ $0.78  |  盈亏：+$0.24  |  最大回撤：-$0.60

 XRP  [已禁用]

 最近交易：
   btc-updown-15m-1711233600  UP  胜  +$3.40
   sol-updown-15m-1711233600  UP  负  -$6.20

 [M] 手动全部赎回  |  [Ctrl+C] 停止
═══════════════════════════════════════════════════════════════════
```

**每行显示的内容：**
- **每种币：** 市场 ID 后缀、剩余时间、UP/DOWN 卖价、强势方方向、置信度分数
- **持仓：** 合约数量、平均入场价格、当前未实现盈亏、最大回撤
- **情景：** UP 赢 vs DOWN 赢分别会发生什么（帮助你了解风险暴露）
- **最近交易：** 最近已平仓的交易，含胜/负结果和盈亏

---

## 11. Web 仪表板（浏览器）

安装依赖（`flask` 已在 `requirements.txt` 中列出）。从 `src` 目录启动启用 Web UI 的机器人：

```bash
cd src
python main.py --web
```

打开 **http://127.0.0.1:5050/**（或根据需要设置 `--web-port` / `--web-host`）。

| 功能 | 说明 |
|--------|-------------|
| **实时分析** | 会话运行时间、模式（模拟运行/实盘）、钱包余额、总盈亏、ROI、每种币的订单簿（UP/DN 卖价、强势方、置信度）、含未实现盈亏和情景盈亏的未平仓头寸 |
| **最近交易** | 各策略最近已平仓交易 |
| **设置** | 在浏览器中加载和编辑 `config/config.json`；**保存会写入文件** ——**重启机器人**以应用更改 |
| **请求停止** | 发送与 **Ctrl+C** 相同的优雅停止（关闭处理程序保存头寸） |

**安全：** 默认情况下，服务器仅绑定到 `127.0.0.1`。仅在受信任的网络上使用 `--web-host 0.0.0.0`（如果暴露到互联网，请添加反向代理和身份验证）。

**API（用于你自己的工具）：** `GET /api/status`（JSON 快照）、`GET/POST /api/config`、`POST /api/bot/stop`、`GET /api/health`。

当 `logs/bot_state.json` 正在更新时，你也可以单独运行 Flask 应用以获得只读视图：`cd src` 然后 `python -m web_dashboard.server`（在机器人至少使用 `--web` 运行过一次后，快照才会出现）。

---

## 12. Telegram 集成

如果已配置，机器人通过 Telegram 发送通知并接受命令。

### 命令

| 命令 | 说明 |
|---------|-------------|
| `/chart` 或 `/pnl` | 生成并发送盈亏图表图片 |
| `/b` 或 `/balance` | 显示钱包 USDC 和 POL 余额 |
| `/t` 或 `/positions` | 显示所有活跃持仓 |
| `/r` 或 `/redeem` | 手动触发已解析市场的赎回 |
| `/off` 或 `/stop` | 紧急关机（需要确认） |
| `/help` | 列出所有可用命令 |

### 自动发送的通知

- 交易入场（币种、方向、合约数、价格）
- 交易退出（原因、盈亏）
- 止损和翻转止损触发
- 市场解析结果
- 错误提醒

---

## 13. 安全功能

### 模拟运行模式

当 `safety.dry_run` 为 `true` 时：
- 所有买单被**模拟**（无真实交易）
- 所有卖单被**模拟**
- 机器人在其他方面行为相同（价格、信号、仪表板）
- 在冒险使用真实资金前使用此模式验证机器人工作正常

### 订单大小限制

- **每单：** `max_order_size_usd`（默认 $150）——拒绝任何超过此金额的订单
- **每市场：** `max_total_investment`（默认 $1000）——追踪每个市场 slug 的累计投资
- **每策略：** `max_investment_per_market`（默认 $300）——策略在发信号前检查

### 速率限制

- 每分钟最多 `max_orders_per_minute`（默认 100）个订单
- 入场频率：每个市场每 7 秒一个信号（防止订单刷屏）

### 紧急停止

- 按 **Ctrl+C** 优雅关闭（将头寸保存为紧急保存）
- 在 Telegram 中使用 `/off` 进行远程关机
- `SafetyGuard.activate_emergency_stop()` 阻止所有未来的订单

### 价格验证

在任何退出决策之前，价格经过验证：
- 必须**新鲜**（< 2 秒旧）
- UP 和 DOWN 时间戳必须**同步**（相差 < 2 秒）
- 卖价之和必须**合理**（在 $0.95 和 $1.15 之间）

这防止机器人基于过时或损坏的数据做出退出决策。

---

## 14. 项目结构

```
up-down-spread-bot/
├── src/
│   ├── main.py                    # 入口点、主循环、回调、配置加载
│   ├── market_config.py           # market_window "5m"/"15m" → market_interval_sec
│   ├── strategy.py                # Late Entry V3 策略逻辑
│   ├── trader.py                  # 每个币种的持仓管理和盈亏追踪
│   ├── multi_trader.py            # 管理多个 Trader 实例（每种币一个）
│   ├── data_feed.py               # Gamma API + WebSocket 订单簿馈送
│   ├── order_executor.py          # CLOB 客户端：FAK 买入、FOK 卖出、扫除、赎回
│   ├── polymarket_api.py          # Gamma API 辅助函数，用于市场解析
│   ├── safety_guard.py            # 模拟运行、订单限制、速率限制、紧急停止
│   ├── position_tracker.py        # 持仓模型（用于 WebSocket 用户频道）
│   ├── trade_logger.py            # 将交易记录到 logs/trades.log
│   ├── dashboard_multi_ab.py      # 终端 UI 渲染
│   ├── telegram_notifier.py       # Telegram 机器人通知和命令
│   ├── simple_redeem_collector.py # 用于自动赎回的后台线程
│   ├── pnl_chart_generator.py     # Matplotlib 盈亏图表生成
│   ├── keyboard_listener.py       # 非阻塞键盘输入（跨平台）
│   ├── web_dashboard_state.py     # 浏览器仪表板的线程安全快照
│   └── web_dashboard/             # Flask 应用：API + 静态 UI（python main.py --web）
├── config/
│   ├── config.json                # 你的交易配置（从示例创建）
│   └── config.example.json        # 示例配置模板
├── logs/                          # 日志文件（自动创建）
│   ├── trades.jsonl               # 交易历史（JSON Lines）
│   ├── safety.log                 # 安全卫士事件
│   ├── bot_state.json             # 使用 --web 时写入（可选的监控文件）
│   └── session.json               # 会话状态
├── docs/                          # 文档
│   └── README.md                  # 本文件
├── requirements.txt               # Python 依赖
├── .env                           # 你的环境变量（从示例创建）
├── .env.example                   # 示例环境模板
├── .gitignore                     # Git 忽略规则
└── README.md                      # 项目概述
```

---

## 15. 故障排除

### `ModuleNotFoundError: No module named 'termios'`

这发生在 Windows 上。`keyboard_listener.py` 使用了仅 Unix 的模块。确保你有最新版本，其中包含通过 `msvcrt` 的 Windows 支持。

### `FileNotFoundError: config/config.json`

你需要从示例创建配置文件：

```bash
cp config/config.example.json config/config.json     # Linux/macOS
copy config\config.example.json config\config.json   # Windows
```

### `UnicodeEncodeError: 'charmap' codec can't encode character`

这发生在 Windows 上，当向日志文件写入 emoji 字符时。确保 `safety_guard.py` 中的所有 `open()` 调用使用 `encoding='utf-8'`。

### "Rate limit exceeded"

公共 Polygon RPC 有速率限制。使用私有 RPC：

```env
RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/YOUR_API_KEY
```

### "Invalid signature"

1. 检查 `.env` 中的 API 凭据是否正确
2. 确保私钥与你的 Polymarket 账户匹配
3. 如有需要，在 Polymarket 上重新生成 API 凭据

### WebSocket 断开

机器人会在市场窗口之间自动重连。如果连接频繁断开：
1. 检查你的互联网连接
2. 尝试 VPN
3. 将 DNS 更改为 `1.1.1.1` 或 `8.8.8.8`

### 头寸未赎回

1. 预言机解析需要市场关闭后 1-2 分钟
2. 在 Telegram 中使用 `/r` 手动触发
3. 检查 `logs/` 目录中的错误详情
4. 机器人每 5 分钟自动检查可赎回的头寸

---

## 免责声明

本软件仅供**教育目的**使用。预测市场交易涉及**重大的资金损失风险**。过往表现**并不**预示未来结果。使用本软件风险自负，切勿使用无法承受亏损的资金进行交易。**扩展策略**（马丁格尔/反马丁格尔/斐波那契仓位管理、完整 TA 组合、贝叶斯优势、Avellaneda–Stoikov 式库存、凯利公式、蒙特卡洛及相关策略）**单独提供**——请参见[仓库 README](../../README.md) 和 Telegram [@terauss](https://t.me/terauss)。
