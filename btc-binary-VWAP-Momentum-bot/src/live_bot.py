"""
实时交易机器人主类：
初始化、市场发现、入场执行、对冲管理、会话循环。
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp
from rich.console import Console
from rich.live import Live

from .auto_redeemer import AsyncAutoRedeemer
from .chainlink_ws_client import ChainlinkPriceClient
from .config_loader import load_config, validate_config
from .core_types import MarketState, TokenData
from .dashboard import Dashboard
from .database import Database
from .hedge_manager import (
    HedgeConfig as HedgeManagerConfig,
    HedgeManager,
    HedgeResult,
)
from .market_ws_client import WebSocketClient
from .order_executor import ExecutionConfig, OrderExecutor
from .simulation_history import SimulationHistoryLogger
from .telegram_notifier import TelegramNotifier
from .trading_stats import TradingStats
from .user_websocket import UserWebSocket
from .web_dashboard import WebSnapshotHolder, start_web_dashboard

logger = logging.getLogger("btc_live")
signal_logger = logging.getLogger("btc_live.signals")

# Gamma API 地址（用于市场发现）
GAMMA_API = "https://gamma-api.polymarket.com"


class LiveTradingBot:
    """BTC 涨跌实时交易机器人"""

    def __init__(self, console: Console):
        self.config = None
        self.state = MarketState()
        self._db: Optional[Database] = None
        self.stats: TradingStats = None  # 在 initialize() 中创建
        self.dashboard: Dashboard = None
        self.console = console

        # 交易组件
        self.executor: OrderExecutor = None
        self.hedge_mgr: HedgeManager = None
        self.redeemer: Optional[AsyncAutoRedeemer] = None
        self.telegram: TelegramNotifier = None
        self.user_ws = None
        self._user_ws_task: Optional[asyncio.Task] = None

        # WebSocket
        self.ws_client: WebSocketClient = None

        # Chainlink BTC 价格
        self.chainlink_client: ChainlinkPriceClient = None
        self._chainlink_task: Optional[asyncio.Task] = None

        # 控制
        self.running = False
        self.tasks = []
        self._sim_history: Optional[SimulationHistoryLogger] = None
        self._web_snapshot_holder: Optional[WebSnapshotHolder] = None

    # ════════════════════════════════════════════════════════════════════════
    # 初始化
    # ════════════════════════════════════════════════════════════════════════

    async def initialize(self) -> bool:
        """加载配置、验证、初始化所有组件"""
        # 加载配置
        self.config = load_config()
        errors = validate_config(self.config)
        if errors:
            for err in errors:
                self.console.print(f"[red]配置错误: {err}[/red]")
            return False

        im = self.config.market.interval_minutes
        self.console.print(f"[bold cyan]🚀 BTC {im}-Min 实时交易机器人[/bold cyan]")
        if self.config.simulation.enabled:
            self.console.print(
                "[bold yellow]   模拟模式 — 无 CLOB 订单，无自动赎回[/bold yellow]\n"
            )
        else:
            self.console.print("[bold cyan]   实盘交易 + 仪表盘[/bold cyan]\n")

        self.console.print(
            f"[green]✓ 市场: BTC 涨/跌 {im}m "
            f"(slug btc-updown-{im}m-*)[/green]"
        )
        self.console.print(
            f"[green]✓ 配置: P {self.config.strategy.min_price}-"
            f"{self.config.strategy.max_price}, "
            f"T≥{self.config.strategy.min_elapsed_sec}s, "
            f"偏差 {self.config.strategy.min_deviation_pct}%-"
            f"{self.config.strategy.max_deviation_pct}%[/green]"
        )
        self.console.print(
            f"[green]✓ 下注: ${self.config.entry.bet_amount_usd}, "
            f"对冲: {'ON' if self.config.hedge.enabled else 'OFF'}[/green]"
        )

        # ── 初始化 SQLite 数据库 ──────────────────────────────────────
        self.console.print("[yellow]正在初始化数据库...[/yellow]")
        self._db = Database()
        self._db.initialize()

        # 确定运行模式
        sim = self.config.simulation.enabled
        mode = "simulation" if sim else "live"

        # 创建对应模式的交易统计
        self.stats = TradingStats(db=self._db, mode=mode)

        # 初始化资金账户
        db_initial_capital = float(os.getenv("INITIAL_CAPITAL", "1000"))
        self._db.init_account(initial_capital=db_initial_capital, mode=mode)
        account = self._db.get_account(mode)
        if account:
            self.console.print(
                f"[green]✓ 数据库: data/trading.db | "
                f"模式={mode} | "
                f"初始资金=${account['initial_capital']:.0f} | "
                f"现有交易={self.stats.trade_count} 笔[/green]"
            )

        # 初始化交易组件
        self.console.print("[yellow]正在初始化交易组件...[/yellow]")

        # Telegram
        self.telegram = TelegramNotifier(
            bot_token=self.config.telegram.bot_token,
            chat_id=self.config.telegram.chat_id,
            enabled=self.config.telegram.enabled,
        )

        sim = self.config.simulation.enabled

        if sim:
            self.user_ws = None
            self._user_ws_task = None
            # 模拟模式下使用虚拟凭证——CLOB 不会真的被初始化
            pk = (
                self.config.polymarket.private_key
                or "0x0000000000000000000000000000000000000000000000000000000000000001"
            )
            ak = self.config.polymarket.api_key or "sim"
            sec = self.config.polymarket.api_secret or "sim"
            ph = self.config.polymarket.api_passphrase or "sim"
            self.executor = OrderExecutor(
                private_key=pk,
                api_key=ak,
                api_secret=sec,
                api_passphrase=ph,
                clob_host=self.config.polymarket.clob_host,
                chain_id=self.config.polymarket.chain_id,
                signature_type=self.config.polymarket.signature_type,
                funder_address=self.config.polymarket.funder_address or None,
                user_ws=None,
                simulation_mode=True,
            )
            self.console.print("[green]✓ 订单执行器: 模拟 (无 CLOB)[/green]")
        else:
            # 用户 WebSocket（用于订单追踪——对成交确认至关重要！）
            self.user_ws = UserWebSocket(
                api_key=self.config.polymarket.api_key,
                api_secret=self.config.polymarket.api_secret,
                api_passphrase=self.config.polymarket.api_passphrase,
            )
            self._user_ws_task = None

            self.executor = OrderExecutor(
                private_key=self.config.polymarket.private_key,
                api_key=self.config.polymarket.api_key,
                api_secret=self.config.polymarket.api_secret,
                api_passphrase=self.config.polymarket.api_passphrase,
                clob_host=self.config.polymarket.clob_host,
                chain_id=self.config.polymarket.chain_id,
                signature_type=self.config.polymarket.signature_type,
                funder_address=self.config.polymarket.funder_address or None,
                user_ws=self.user_ws,
                simulation_mode=False,
            )

            if not await self.executor.initialize():
                self.console.print("[red]订单执行器初始化失败[/red]")
                return False

            self.console.print("[yellow]正在启动用户 WebSocket 进行订单追踪...[/yellow]")
            self._user_ws_task = asyncio.create_task(self.user_ws.connect())
            await asyncio.sleep(1)
            if self.user_ws.connected:
                self.console.print(
                    "[green]用户 WebSocket 已连接 - 订单追踪已激活[/green]"
                )
                logger.info("用户 WebSocket 已连接，用于订单成交追踪")
            else:
                self.console.print(
                    "[yellow]用户 WebSocket 正在连接... (将重试)[/yellow]"
                )
                logger.warning("用户 WebSocket 尚未连接")

        # 对冲管理器
        hedge_config = HedgeManagerConfig(
            enabled=self.config.hedge.enabled,
            hedge_price=self.config.hedge.hedge_price,
            order_type=self.config.hedge.order_type,
            max_retries=self.config.hedge.max_retries,
            retry_delay_ms=self.config.hedge.retry_delay_ms,
            simulation_mode=sim,
        )
        self.hedge_mgr = HedgeManager(self.executor, hedge_config)

        # 自动赎回（仅实盘）
        if sim:
            self.redeemer = None
            self.console.print("[yellow]✓ 自动赎回: 模拟模式下已禁用[/yellow]")
        else:
            self.redeemer = AsyncAutoRedeemer(
                private_key=self.config.polymarket.private_key,
                rpc_url=self.config.polymarket.rpc_url,
                funder_address=self.config.polymarket.funder_address or None,
                signature_type=self.config.polymarket.signature_type,
                interval_seconds=self.config.redeem.interval_seconds,
                telegram_notifier=self.telegram,
            )

        if sim:
            jl = (self.config.simulation.history_jsonl_path or "").strip()
            self._sim_history = SimulationHistoryLogger(
                db=self._db,
                mode="simulation",
                csv_path=self.config.simulation.history_csv_path,
                jsonl_path=jl if jl else None,
                summary_path=self.config.simulation.history_summary_path,
            )
            if self.stats.trades:
                self._sim_history.write_summary(
                    self._db.get_all_trades_as_dicts(mode="simulation"),
                    self.stats.summary_dict(),
                )
            csv_p = self.config.simulation.history_csv_path or "(已禁用)"
            sum_p = self.config.simulation.history_summary_path or "(已禁用)"
            jl_p = jl or "(已禁用)"
            self.console.print(
                f"[green]✓ 模拟分析: CSV={csv_p} | JSONL={jl_p} | 摘要={sum_p}[/green]"
            )
        else:
            self._sim_history = None

        # Chainlink BTC 价格客户端
        self.chainlink_client = ChainlinkPriceClient(
            self.state, self.config.market.duration_sec
        )
        self._chainlink_task = asyncio.create_task(self.chainlink_client.connect())
        self.console.print("[green]✓ Chainlink BTC/USD 价格流启动中...[/green]")

        # 仪表盘
        self.dashboard = Dashboard(self.state, self.stats, self.config, db=self._db)

        wd = self.config.web_dashboard
        if wd.enabled:
            self._web_snapshot_holder = WebSnapshotHolder()
            dashboard_password = wd.password or ""
            ok = start_web_dashboard(
                wd.host, wd.port, self._web_snapshot_holder, dashboard_password, db=self._db
            )
            # 0.0.0.0 不是有效的浏览器 URL；显示时使用 loopback
            if wd.host in ("0.0.0.0", ""):
                open_url = f"http://127.0.0.1:{wd.port}/"
            elif wd.host in ("::", "[::]"):
                open_url = f"http://[::1]:{wd.port}/"
            else:
                open_url = f"http://{wd.host}:{wd.port}/"
            if ok:
                self.console.print(
                    f"[green]✓ Web 仪表盘:[/green] [bold]{open_url}[/bold]"
                )
                if dashboard_password:
                    self.console.print(
                        "[yellow]  🔒 认证已启用 — 需要密码[/yellow]"
                    )
                self.console.print(
                    "[dim]  使用 http:// 而非 https://。在 Windows 上，"
                    "如果页面无法打开，请直接打开这个完整 URL（避免只输入 "
                    "\"localhost\"，它可能解析为 IPv6）。[/dim]"
                )
            else:
                self.console.print(
                    f"[yellow]⚠ Web 仪表盘在端口 {wd.port} 上未启动 "
                    f"(被其他应用占用或绑定失败)。请检查日志。[/yellow]"
                )

        self.console.print("[green]✓ 所有组件已初始化[/green]\n")
        return True

    # ════════════════════════════════════════════════════════════════════════
    # 市场发现
    # ════════════════════════════════════════════════════════════════════════

    async def find_market(self) -> bool:
        """搜索活跃的 BTC 涨跌市场"""
        d = self.config.market.duration_sec
        sfx = self.config.market.slug_infix
        self.console.print(
            f"[yellow]正在搜索活跃的 BTC {self.config.market.interval_minutes}-min 市场...[/yellow]"
        )

        async with aiohttp.ClientSession() as session:
            now = int(time.time())
            current_window = (now // d) * d

            for offset in [0, d, -d, 2 * d]:
                target_ts = current_window + offset
                expected_slug = f"btc-updown-{sfx}-{target_ts}"

                try:
                    async with session.get(
                        f"{GAMMA_API}/markets?slug={expected_slug}"
                    ) as resp:
                        if resp.status == 200:
                            markets = await resp.json()
                            if markets:
                                market = markets[0]
                                returned_slug = market.get("slug", "")

                                # 关键：验证 API 返回的是我们请求的市场
                                if returned_slug != expected_slug:
                                    logger.warning(
                                        f"API slug 不匹配！请求的是 {expected_slug}，"
                                        f"收到的是 {returned_slug}"
                                    )
                                    continue

                                if not market.get("closed", True):
                                    return await self._setup_market(market)
                except Exception as e:
                    logger.debug(f"查找市场 {expected_slug} 时出错: {e}")
                    continue

        return False

    async def _setup_market(self, market: dict) -> bool:
        """配置新发现的市场"""
        self.console.print(f"[green]已找到: {market.get('slug')}[/green]")

        outcomes = market.get("outcomes", [])
        tokens = market.get("clobTokenIds", [])

        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(tokens, str):
            tokens = json.loads(tokens)

        up_token_id = None
        down_token_id = None

        # 使用精确索引查找（与参考实现一致）
        try:
            up_index = outcomes.index("Up") if "Up" in outcomes else None
            down_index = outcomes.index("Down") if "Down" in outcomes else None

            if up_index is not None and up_index < len(tokens):
                up_token_id = tokens[up_index]
            if down_index is not None and down_index < len(tokens):
                down_token_id = tokens[down_index]
        except (ValueError, IndexError):
            pass

        # 回退到基于包含的匹配
        if not up_token_id or not down_token_id:
            for i, outcome in enumerate(outcomes):
                if i < len(tokens):
                    outcome_lower = str(outcome).lower()
                    if not up_token_id and "up" in outcome_lower:
                        up_token_id = tokens[i]
                    elif not down_token_id and "down" in outcome_lower:
                        down_token_id = tokens[i]

        # 最终回退
        if not up_token_id and len(tokens) >= 1:
            up_token_id = tokens[0]
        if not down_token_id and len(tokens) >= 2:
            down_token_id = tokens[1]

        if not up_token_id or not down_token_id:
            return False

        end_str = market.get("end_date_iso") or market.get("endDate", "")
        try:
            end_time = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            end_timestamp = end_time.timestamp()
        except Exception:
            end_timestamp = time.time() + self.config.market.duration_sec

        slug = market.get("slug", "")

        self.state.market_id = market.get("id", "")
        self.state.condition_id = market.get("conditionId", "")
        self.state.slug = slug
        self.state.end_time = end_timestamp
        self.state.up_token = TokenData(token_id=up_token_id, name="Up")
        self.state.down_token = TokenData(token_id=down_token_id, name="Down")
        self.state.connected = False

        # 记录代币分配（用于调试）
        logger.info("市场代币已分配:")
        logger.info(f"  Slug: {slug}")
        logger.info(f"  结束时间: {end_str} (时间戳: {end_timestamp})")
        logger.info(f"  UP 代币: {up_token_id[:40]}...")
        logger.info(f"  DOWN 代币: {down_token_id[:40]}...")

        self.stats.new_market(self.state.slug)
        self.hedge_mgr.clear()  # 重置对冲状态
        if self.user_ws:
            self.user_ws.clear_token_fills()  # 重置 WS 成交缓冲区

        # BTC 锚定价格现由 ChainlinkPriceClient 自动管理
        # 它独立使用 Chainlink 时间戳检测时间间隔边界

        return True

    # ════════════════════════════════════════════════════════════════════════
    # 模拟历史记录
    # ════════════════════════════════════════════════════════════════════════

    def _simulation_log_entry(
        self,
        token_name: str,
        avg_price: float,
        contracts: int,
        total_cost: float,
    ) -> None:
        """记录模拟入场到历史文件"""
        if not self._sim_history or not self.config.simulation.enabled:
            return
        pos = self.stats.position
        hedged = bool(pos and pos.hedged)
        self._sim_history.log_open(
            market_slug=self.state.slug,
            token_name=token_name,
            contracts=contracts,
            avg_price=avg_price,
            total_cost=total_cost,
            cumulative_realized_pnl=self.stats.total_pnl,
            hedged=hedged,
            trade_number=len(self.stats.trades) + 1,
        )
        signal_logger.info(
            f"  [模拟] 历史 OPEN 已记录 | 平仓前已实现盈亏: ${self.stats.total_pnl:+.4f}"
        )

    def _simulation_log_close(self, record, hedged_was: bool) -> None:
        """记录模拟平仓到历史文件"""
        if not self._sim_history or not self.config.simulation.enabled:
            return
        n = len(self.stats.trades)
        self._sim_history.log_close(
            record,
            cumulative_pnl=self.stats.total_pnl,
            total_closed=n,
            win_rate_pct=self.stats.win_rate,
            hedged=hedged_was,
        )
        self._sim_history.write_summary(
            self._db.get_all_trades_as_dicts(mode="simulation"),
            self.stats.summary_dict(),
        )
        # 更新数据库资金账户
        self._db.update_account(
            realized_pnl=self.stats.total_pnl, mode="simulation"
        )
        s = self.stats.summary_dict()
        signal_logger.info(
            f"  [模拟] 历史 CLOSE 已记录 | 交易盈亏 ${record.pnl:+.4f} | "
            f"累计 ${s['total_pnl_usd']:+.4f} | 胜率 {s['win_rate_pct']:.2f}% ({n} 已平仓)"
        )

    # ════════════════════════════════════════════════════════════════════════
    # 入场执行
    # ════════════════════════════════════════════════════════════════════════

    async def execute_entry(self, side: str):
        """执行入场订单（实盘 CLOB 或模拟）"""
        if not self.stats.can_enter():
            signal_logger.info(
                f"信号已忽略: {side} - 无法入场（已有持仓）"
            )
            return

        # 防御性时间截止检查（竞态条件保护）
        time_left = max(0, self.state.end_time - time.time())
        no_entry_cutoff = self.config.strategy.no_entry_before_end_sec
        if time_left < no_entry_cutoff:
            signal_logger.info(
                f"信号已阻止: {side} - 距离市场结束太近 "
                f"({time_left:.0f}s 剩余 < {no_entry_cutoff}s 截止)"
            )
            logger.warning(
                f"入场已阻止: {time_left:.0f}s 剩余 < {no_entry_cutoff}s 截止"
            )
            return

        if side == "BUY_UP":
            token = self.state.up_token
            token_name = "UP"
            opposite_token = self.state.down_token
        else:
            token = self.state.down_token
            token_name = "DOWN"
            opposite_token = self.state.up_token

        if not token or not opposite_token:
            signal_logger.warning(f"信号已忽略: {side} - 代币数据缺失")
            return

        # 记录完整的信号快照
        mode_str = "模拟" if self.config.simulation.enabled else ""
        signal_logger.info("=" * 60)
        signal_logger.info(
            f"交易信号触发{mode_str}"
        )
        signal_logger.info(f"  时间: {datetime.now().isoformat()}")
        signal_logger.info(f"  市场: {self.state.slug}")
        signal_logger.info(f"  信号: {side}")
        signal_logger.info(f"  代币: {token_name}")

        time_left = max(0, self.state.end_time - time.time())
        dur = self.config.market.duration_sec
        span = self.config.market.interval_minutes
        elapsed_sec = dur - time_left
        time_bin = int((span - 1) - time_left / 60)
        time_bin = max(0, min(time_bin, span - 1))
        signal_logger.info(
            f"  已过: {elapsed_sec:.0f}s | 剩余: {time_left:.0f}s | 分箱: {time_bin}"
        )

        # 计算两个代币的所有指标
        calc = self.dashboard.calc
        vwap_window = self.config.strategy.vwap_window_sec
        mom_window = self.config.strategy.momentum_window_sec

        for label, tk in [
            ("UP", self.state.up_token),
            ("DOWN", self.state.down_token),
        ]:
            if not tk:
                signal_logger.info(f"  {label}: 无数据")
                continue

            vwap = calc.calc_vwap(
                calc.get_trades_in_window(tk.trades, vwap_window)
            )
            dev = calc.calc_deviation(tk.last_price, vwap)
            zscore = calc.calc_zscore(tk.trades, tk.last_price, window=5)
            mom = calc.calc_momentum(tk.trades, tk.last_price, window=mom_window)
            mom_str = f"{mom:+.2f}%" if mom is not None else "N/A"

            signal_logger.info(f"  --- {label} ---")
            signal_logger.info(
                f"    价格:   LAST={tk.last_price:.4f}  "
                f"BID={tk.best_bid:.4f}  ASK={tk.best_ask:.4f}"
            )
            signal_logger.info(
                f"    VWAP {vwap_window}s: {vwap:.4f}  |  偏差: {dev:+.2f}%"
            )
            signal_logger.info(
                f"    Z-Score 5s: {zscore:+.2f}  |  动量 {mom_window}s: {mom_str}"
            )
            signal_logger.info(
                f"    成交笔数: {tk.trade_count}  |  成交量: {tk.volume_total:.0f}"
            )
            signal_logger.info(
                f"    买入量: {tk.volume_buy:.0f}  |  卖出量: {tk.volume_sell:.0f}"
            )

        # 胜率
        up = self.state.up_token
        down = self.state.down_token
        if up and down:
            fav_price = (
                up.last_price if up.last_price > down.last_price else down.last_price
            )
            wr = self.dashboard.winrate_table.get_winrate(
                fav_price, time_bin, self.config.market.interval_minutes
            )
            signal_logger.info(
                f"  胜率: {wr:.1f}%" if wr else "  胜率: N/A"
            )

        # 策略条件快照
        signal_logger.info(
            f"  配置: min_price={self.config.strategy.min_price}, "
            f"max_price={self.config.strategy.max_price}, "
            f"min_elapsed={self.config.strategy.min_elapsed_sec}s, "
            f"dev_range={self.config.strategy.min_deviation_pct}%-"
            f"{self.config.strategy.max_deviation_pct}%, "
            f"no_entry_cutoff={self.config.strategy.no_entry_before_end_sec}s"
        )

        # Chainlink BTC/USD
        s = self.state
        if s.btc_current_price > 0 and s.btc_anchor_price > 0:
            btc_dev_abs = s.btc_current_price - s.btc_anchor_price
            btc_dev_pct = (btc_dev_abs / s.btc_anchor_price) * 100
            signal_logger.info(
                f"  BTC Chainlink: ${s.btc_current_price:,.2f} "
                f"(锚定: ${s.btc_anchor_price:,.2f})"
            )
            signal_logger.info(
                f"  BTC 偏离: ${btc_dev_abs:+,.2f} ({btc_dev_pct:+.4f}%)"
            )
        else:
            signal_logger.info("  BTC Chainlink: N/A")

        signal_logger.info("=" * 60)

        logger.info(f"正在执行入场: {token_name}")

        exec_config = ExecutionConfig(
            bet_amount_usd=self.config.entry.bet_amount_usd,
            price_offset=self.config.entry.price_offset,
            max_retries=self.config.entry.max_retries,
            retry_delay_ms=self.config.entry.retry_delay_ms,
            fill_timeout_ms=self.config.entry.fill_timeout_ms,
            min_contracts=self.config.entry.min_contracts,
            min_order_usd=self.config.entry.min_order_usd,
            max_entry_price=self.config.entry.max_entry_price,
        )

        result = await self.executor.execute_entry(
            token_id=token.token_id,
            config=exec_config,
            websocket_price=token.best_ask,  # 买入用 ASK！我们向卖方付款。
        )

        if result.success:
            self.stats.record_entry(
                token_name=token_name,
                token_id=token.token_id,
                opposite_token_id=opposite_token.token_id,
                price=result.avg_price,
                contracts=result.contracts_filled,
                market_slug=self.state.slug,
                order_id=getattr(result, "order_id", ""),
            )
            self._simulation_log_entry(
                token_name, result.avg_price, result.contracts_filled, result.total_cost
            )

            self.dashboard.entry_flash = True

            # 记录成功入场
            sim_tag = "（模拟）" if self.config.simulation.enabled else ""
            signal_logger.info(f"入场执行成功{sim_tag}")
            signal_logger.info(f"  代币: {token_name}")
            signal_logger.info(f"  张数: {result.contracts_filled}")
            signal_logger.info(f"  均价: {result.avg_price:.4f}")
            signal_logger.info(f"  总成本: ${result.total_cost:.2f}")
            signal_logger.info(f"  尝试次数: {result.attempts}")
            signal_logger.info("-" * 40)

            await self.telegram.notify_entry(
                side=token_name,
                price=result.avg_price,
                contracts=result.contracts_filled,
                cost=result.total_cost,
                retries=result.attempts,
                interval_minutes=self.config.market.interval_minutes,
                simulation=self.config.simulation.enabled,
            )

            logger.info(
                f"入场完成: {result.contracts_filled} @ {result.avg_price:.3f}"
            )

            # === 下 GTD 对冲订单 ===
            if self.config.hedge.enabled:
                self.hedge_mgr.set_position(
                    opposite_token_id=opposite_token.token_id,
                    contracts=result.contracts_filled,
                )

                hedge_result = await self.hedge_mgr.place_gtd_hedge()

                if hedge_result.success:
                    self.dashboard.hedge_flash = True
                    hedge_cost = hedge_result.contracts * hedge_result.price

                    hsim = (
                        "🎮 <b>[模拟]</b>\n"
                        if self.config.simulation.enabled
                        else ""
                    )
                    await self.telegram.send_message(
                        f"{hsim}"
                        f"🛡️ <b>对冲订单已下 (GTD)</b>\n"
                        f"📦 {hedge_result.contracts} 张 @ ${hedge_result.price}\n"
                        f"💰 成本: ${hedge_cost:.2f}\n"
                        f"🔖 订单 ID: {hedge_result.order_id[:20]}...\n"
                        f"📋 状态: LIVE (被动)\n"
                        f"🔄 尝试次数: {hedge_result.attempts}"
                    )

                    # 注册 WebSocket 处理器追踪对冲成交
                    self._register_hedge_ws_handler()

                    logger.info(
                        f"GTD 对冲已下: {hedge_result.contracts} @ ${hedge_result.price}"
                    )
                else:
                    await self.telegram.send_message(
                        f"⚠️ <b>对冲失败</b>\n"
                        f"❌ {hedge_result.error}\n"
                        f"🔄 尝试次数: {hedge_result.attempts}"
                    )
                    logger.error(f"对冲失败: {hedge_result.error}")
        else:
            signal_logger.error(f"入场失败: {result.error}")
            signal_logger.info(f"  尝试次数: {result.attempts}")
            signal_logger.info("-" * 40)
            logger.error(f"入场失败: {result.error}")

            # ═══════════════════════════════════════════════════════════════
            # 关键：如果超时 — 不重试（防止重复买入！）
            # 转而通过 WebSocket 检查订单是否已执行
            # ═══════════════════════════════════════════════════════════════
            if result.was_timeout:
                signal_logger.error("🛑 超时: 正在通过 WebSocket 检查成交...")
                logger.warning("检测到超时 — 启动 WS 恢复")

                recovered = False

                if self.user_ws and self.user_ws.connected:
                    recovery_timeout = self.config.entry.ws_recovery_timeout_sec

                    signal_logger.info(
                        f"  正在检查 WS 上 {token.token_id[:30]} 的成交..."
                    )
                    signal_logger.info(f"  恢复超时: {recovery_timeout}s")

                    fill_data = await self.user_ws.wait_for_fills_on_token(
                        token_id=token.token_id,
                        timeout=recovery_timeout,
                    )

                    if fill_data and fill_data["contracts"] > 0:
                        # ═══════════════════════════════
                        # 恢复：订单确实已执行！
                        # ═══════════════════════════════
                        recovered = True
                        rec_contracts = fill_data["contracts"]
                        rec_price = fill_data["avg_price"]
                        rec_cost = fill_data["total_cost"]

                        signal_logger.info("=" * 60)
                        signal_logger.info(
                            "✅ 超时恢复: 通过 WebSocket 发现持仓！"
                        )
                        signal_logger.info(f"  张数: {rec_contracts}")
                        signal_logger.info(f"  均价: {rec_price:.4f}")
                        signal_logger.info(f"  总成本: ${rec_cost:.2f}")
                        signal_logger.info(
                            f"  成交数: {len(fill_data['fills'])}"
                        )
                        signal_logger.info("=" * 60)

                        logger.info(
                            f"超时恢复: {rec_contracts} @ {rec_price:.4f}"
                        )

                        # 记录持仓（如同入场成功）
                        self.stats.record_entry(
                            token_name=token_name,
                            token_id=token.token_id,
                            opposite_token_id=opposite_token.token_id,
                            price=rec_price,
                            contracts=rec_contracts,
                            market_slug=self.state.slug,
                            order_id="recovered",  # 超时恢复，无原始订单号
                        )
                        self._simulation_log_entry(
                            token_name, rec_price, rec_contracts, rec_cost
                        )

                        self.dashboard.entry_flash = True

                        await self.telegram.send_message(
                            f"🔄 <b>超时恢复!</b>\n"
                            f"尽管 HTTP 超时，订单已成交。\n"
                            f"📊 {token_name} {rec_contracts} @ ${rec_price:.4f}\n"
                            f"💰 成本: ${rec_cost:.2f}\n"
                            f"市场: {self.state.slug}"
                        )

                        await self.telegram.notify_entry(
                            side=token_name,
                            price=rec_price,
                            contracts=rec_contracts,
                            cost=rec_cost,
                            retries=result.attempts,
                            interval_minutes=self.config.market.interval_minutes,
                            simulation=self.config.simulation.enabled,
                        )

                        # 下对冲订单（正常流程）
                        if self.config.hedge.enabled:
                            self.hedge_mgr.set_position(
                                opposite_token_id=opposite_token.token_id,
                                contracts=rec_contracts,
                            )

                            hedge_result = await self.hedge_mgr.place_gtd_hedge()

                            if hedge_result.success:
                                self.dashboard.hedge_flash = True
                                hedge_cost = (
                                    hedge_result.contracts * hedge_result.price
                                )
                                hsim2 = (
                                    "🎮 <b>[模拟]</b>\n"
                                    if self.config.simulation.enabled
                                    else ""
                                )
                                await self.telegram.send_message(
                                    f"{hsim2}"
                                    f"🛡️ <b>对冲订单已下 (GTD)</b>\n"
                                    f"📦 {hedge_result.contracts} 张 @ ${hedge_result.price}\n"
                                    f"💰 成本: ${hedge_cost:.2f}\n"
                                    f"🔖 订单 ID: {hedge_result.order_id[:20]}...\n"
                                    f"📋 状态: LIVE (被动)\n"
                                    f"🔄 尝试次数: {hedge_result.attempts}"
                                )

                                self._register_hedge_ws_handler()
                                logger.info(
                                    f"GTD 对冲已下（恢复后）: "
                                    f"{hedge_result.contracts} @ ${hedge_result.price}"
                                )
                            else:
                                await self.telegram.send_message(
                                    f"⚠️ <b>对冲失败（恢复后）</b>\n"
                                    f"❌ {hedge_result.error}"
                                )
                    else:
                        signal_logger.info("  WS 恢复: 未找到成交")
                else:
                    signal_logger.warning("  WS 未连接 — 无法恢复")

                if not recovered:
                    # 未找到成交 — 封锁入场（原始行为）
                    self.stats.block_entry(
                        "网络超时 - 通过 WS 未检测到成交。封锁再次入场。"
                    )
                    signal_logger.error(
                        "🛑 入场已封锁: 超时 + 未检测到 WS 成交"
                    )
                    await self.telegram.send_message(
                        f"⚠️ <b>超时 — 未检测到成交</b>\n"
                        f"超时后订单状态未知。\n"
                        f"WebSocket 恢复未发现任何成交。\n"
                        f"再次入场已封锁。\n"
                        f"市场: {self.state.slug}"
                    )

    def _register_hedge_ws_handler(self):
        """注册 WebSocket 处理器追踪对冲订单成交"""
        if not self.user_ws:
            logger.warning("用户 WebSocket 不可用，无法追踪对冲")
            return

        hedge_order_id = self.hedge_mgr.hedge_order_id
        if not hedge_order_id:
            return

        original_on_trade = self.user_ws._on_trade

        async def _hedge_trade_handler(data: dict):
            """处理交易事件并检查对冲成交"""
            # 先调用原始处理器
            if original_on_trade:
                await original_on_trade(data)

            # 检查此交易是否针对我们的对冲订单
            # GTD 订单是 maker 订单，所以检查 maker_order_id
            trade_order_id = data.get("maker_order_id", "") or data.get(
                "taker_order_id", ""
            )
            status = data.get("status", "")

            if trade_order_id == hedge_order_id and status == "MATCHED":
                size = int(float(data.get("size", 0)))
                price = float(data.get("price", 0))

                self.hedge_mgr.on_hedge_fill(size, price)

                filled = (
                    self.hedge_mgr._position.hedge_contracts_filled
                    if self.hedge_mgr._position
                    else 0
                )
                total = (
                    self.hedge_mgr._position.contracts
                    if self.hedge_mgr._position
                    else 0
                )

                if self.hedge_mgr.is_hedged:
                    # 完全成交
                    self.stats.record_hedge(filled, price)
                    self.dashboard.hedge_flash = True

                    await self.telegram.send_message(
                        f"✅ <b>对冲完全成交!</b>\n"
                        f"📦 {filled} 张 @ ${price}\n"
                        f"🛡️ 持仓已完全保护"
                    )
                    logger.info(f"对冲完全成交: {filled} 张")
                else:
                    # 部分成交
                    await self.telegram.send_message(
                        f"🛡️ <b>对冲部分成交</b>\n"
                        f"📦 +{size} 张 @ ${price}\n"
                        f"📊 进度: {filled}/{total}"
                    )
                    logger.info(
                        f"对冲部分成交: +{size}, 累计 {filled}/{total}"
                    )

        self.user_ws._on_trade = _hedge_trade_handler
        logger.info(
            f"已注册对冲成交处理器，订单 {hedge_order_id[:20]}..."
        )

    # ════════════════════════════════════════════════════════════════════════
    # 市场结束检查
    # ════════════════════════════════════════════════════════════════════════

    async def check_market_end(self):
        """在市场结束时平仓"""
        pos = self.stats.position
        if not pos:
            return

        time_left = self.state.end_time - time.time()
        if time_left <= 10:  # 结束前 10 秒
            hedged_was = pos.hedged
            if pos.token_name == "UP" and self.state.up_token:
                final_price = self.state.up_token.last_price
            elif pos.token_name == "DOWN" and self.state.down_token:
                final_price = self.state.down_token.last_price
            else:
                final_price = 0.5

            # 记录市场结束详情
            signal_logger.info("=" * 60)
            signal_logger.info("市场结束 - 正在平仓")
            signal_logger.info(f"  时间: {datetime.now().isoformat()}")
            signal_logger.info(f"  市场: {self.state.slug}")
            signal_logger.info(f"  持仓: {pos.token_name}")
            signal_logger.info(f"  入场价: {pos.entry_price:.4f}")
            signal_logger.info(f"  最终价: {final_price:.4f}")
            signal_logger.info(f"  张数: {pos.contracts}")
            signal_logger.info(f"  已对冲: {pos.hedged}")

            record = self.stats.close_position(final_price)
            if record:
                # 更新数据库资金账户
                self._db.update_account(
                    realized_pnl=self.stats.total_pnl, mode=self.stats.mode
                )
                self._simulation_log_close(record, hedged_was)
                status = "✅ WIN" if record.won else "❌ LOSS"

                signal_logger.info(
                    f"  结果: {'WIN' if record.won else 'LOSS'}"
                )
                signal_logger.info(f"  盈亏: ${record.pnl:+.2f}")
                signal_logger.info(
                    f"  最大回撤: -{record.max_drawdown_abs:.4f} "
                    f"(-{record.max_drawdown_pct:.2f}%)"
                )
                dd_usd = record.max_drawdown_abs * record.contracts
                signal_logger.info(
                    f"  最大回撤 ($): -${dd_usd:.2f} "
                    f"(最低价: {record.entry_price - record.max_drawdown_abs:.4f})"
                )
                signal_logger.info(
                    f"  总交易数: {len(self.stats.trades)}"
                )
                wins = sum(1 for r in self.stats.trades if r.won)
                losses = sum(1 for r in self.stats.trades if not r.won)
                signal_logger.info(
                    f"  会话统计: W={wins} / L={losses}"
                )
                signal_logger.info(
                    f"  总盈亏: ${sum(r.pnl for r in self.stats.trades):+.2f}"
                )
                signal_logger.info("=" * 60)

                logger.info(
                    f"持仓已平: {status}, 盈亏: ${record.pnl:+.2f}"
                )

    # ════════════════════════════════════════════════════════════════════════
    # 会话循环
    # ════════════════════════════════════════════════════════════════════════

    async def run_session(self):
        """运行单市场会话（含仪表盘）"""
        # 启动 WebSocket
        self.ws_client = WebSocketClient(self.state)
        ws_task = asyncio.create_task(self.ws_client.connect())

        await asyncio.sleep(1)

        # 追踪正在运行的订单任务（用于非阻塞执行）
        order_task: Optional[asyncio.Task] = None

        try:
            with Live(
                self.dashboard.render(),
                refresh_per_second=4,
                console=self.console,
            ) as live:
                while self.running:
                    # 更新仪表盘（永不阻塞）
                    live.update(self.dashboard.render())
                    if self._web_snapshot_holder:
                        self._web_snapshot_holder.set(
                            self.dashboard.build_web_snapshot()
                        )

                    # 检查入场信号 — 在独立 task 中启动
                    if self.stats.can_enter() and self.dashboard.last_signal:
                        if order_task is None or order_task.done():
                            signal = self.dashboard.last_signal
                            self.dashboard.last_signal = ""
                            order_task = asyncio.create_task(
                                self._safe_execute_entry(signal)
                            )

                    # 检查订单是否完成
                    if order_task and order_task.done():
                        try:
                            order_task.result()  # 若有异常则抛出
                        except Exception as e:
                            logger.error(f"订单任务错误: {e}")
                        order_task = None

                    # 持仓期间追踪回撤
                    if self.stats.position:
                        pos = self.stats.position
                        if (
                            pos.token_name == "UP"
                            and self.state.up_token
                        ):
                            self.stats.update_drawdown(
                                self.state.up_token.last_price
                            )
                        elif (
                            pos.token_name == "DOWN"
                            and self.state.down_token
                        ):
                            self.stats.update_drawdown(
                                self.state.down_token.last_price
                            )

                    # 检查市场结束（快速操作 — 不放入 task）
                    await self.check_market_end()

                    # 市场已结束？
                    if time.time() > self.state.end_time:
                        self.console.print("\n[yellow]市场已结束![/yellow]")
                        break

                    await asyncio.sleep(0.25)
        finally:
            # 取消所有正在运行的订单任务
            for task in [order_task]:
                if task and not task.done():
                    task.cancel()
                    try:
                        await task
                    except Exception:
                        pass

            # 优雅关闭 WebSocket
            await self.ws_client.stop_graceful()
            try:
                ws_task.cancel()
                await ws_task
            except Exception:
                pass

            # 停止用户 WebSocket（订单追踪）
            if self.user_ws:
                await self.user_ws.disconnect()
            if self._user_ws_task:
                try:
                    self._user_ws_task.cancel()
                    await self._user_ws_task
                except Exception:
                    pass

    async def _safe_execute_entry(self, signal: str):
        """在独立 task 中执行入场，带错误处理"""
        try:
            await self.execute_entry(signal)
        except Exception as e:
            logger.error(f"入场执行错误: {e}")
            signal_logger.error(f"入场错误: {e}")

    # GTD 对冲在入场后立即下单（无需轮询）
    # 成交通过 WebSocket _register_hedge_ws_handler() 追踪

    # ════════════════════════════════════════════════════════════════════════
    # 主运行循环
    # ════════════════════════════════════════════════════════════════════════

    async def run(self):
        """主运行循环"""
        if not await self.initialize():
            return

        self.running = True

        redeemer_task = None
        if self.redeemer is not None:
            redeemer_task = asyncio.create_task(self.redeemer.run_loop())

        sim_note = ""
        if self.config.simulation.enabled:
            sim_note = "🎮 <b>模拟模式</b> — 无真实订单\n"
        await self.telegram.send_message(
            f"{sim_note}"
            f"🤖 <b>机器人已启动</b>\n"
            f"策略: ${self.config.entry.bet_amount_usd} / 笔\n"
            f"对冲: {'已启用' if self.config.hedge.enabled else '已禁用'}"
        )

        try:
            while self.running:
                # 寻找市场
                if not await self.find_market():
                    self.console.print(
                        "[red]未找到市场。等待 30 秒...[/red]"
                    )
                    await asyncio.sleep(30)
                    continue

                self.console.print(
                    "\n[bold green]正在启动会话...[/bold green]\n"
                )
                await self.run_session()

                self.console.print(
                    "[yellow]等待 5 秒以进入下一个市场...[/yellow]"
                )
                await asyncio.sleep(5)

        except KeyboardInterrupt:
            self.console.print("\n[yellow]正在停止...[/yellow]")
        finally:
            self.running = False
            if self.redeemer is not None:
                self.redeemer.stop()
            if redeemer_task is not None:
                try:
                    redeemer_task.cancel()
                    await redeemer_task
                except Exception:
                    pass

            # 优雅关闭 Chainlink RTDS WebSocket
            if self.chainlink_client:
                await self.chainlink_client.disconnect()
            if self._chainlink_task:
                try:
                    self._chainlink_task.cancel()
                    await self._chainlink_task
                except Exception:
                    pass

            await self.telegram.send_message("🛑 机器人已停止")
            await self.telegram.close()

            self.console.print("[green]机器人已停止。[/green]")
