"""
终端仪表盘：基于 Rich 的实时 TUI 面板 + Web 快照 JSON 构建器。
"""

import time
from pathlib import Path
from typing import Any, Dict, Optional

from rich.layout import Layout
from rich.panel import Panel

from .core_types import MarketState, TokenData
from .database import Database
from .indicator_calculator import IndicatorCalculator
from .trading_stats import TradingStats
from .win_rate_table import WinRateTable


class Dashboard:
    """Rich 终端仪表盘 + Web 仪表盘数据构建器"""

    def __init__(self, state: MarketState, stats: TradingStats, config: Any, db: Optional[Database] = None):
        self.state = state
        self.stats = stats
        self.config = config
        self._db = db  # 数据库实例（用于 Web 快照查询）
        self.calc = IndicatorCalculator()

        win_rate_path = Path(__file__).parent.parent / config.strategy.win_rate_csv
        self.winrate_table = WinRateTable(str(win_rate_path))

        self.last_signal = ""
        self.entry_flash = False
        self.hedge_flash = False

    # ── 格式化辅助方法 ────────────────────────────────────────────────────

    def _fmt_price(self, price: float) -> str:
        if price >= 0.6:
            return f"[green]{price:.3f}[/green]"
        elif price <= 0.4:
            return f"[red]{price:.3f}[/red]"
        return f"[yellow]{price:.3f}[/yellow]"

    def _fmt_dev(self, dev: float) -> str:
        if dev > 5:
            return f"[bold green]+{dev:.1f}%[/bold green]"
        elif dev > 0:
            return f"[green]+{dev:.1f}%[/green]"
        elif dev < -5:
            return f"[bold red]{dev:.1f}%[/bold red]"
        elif dev < 0:
            return f"[red]{dev:.1f}%[/red]"
        return f"{dev:+.1f}%"

    def _fmt_zscore(self, z: float) -> str:
        if z > 2:
            return f"[bold magenta]+{z:.2f}[/bold magenta] ⚡"
        elif z > 1:
            return f"[magenta]+{z:.2f}[/magenta]"
        elif z < -2:
            return f"[bold cyan]{z:.2f}[/bold cyan] ⚡"
        elif z < -1:
            return f"[cyan]{z:.2f}[/cyan]"
        return f"{z:+.2f}"

    def _fmt_momentum(self, m: Optional[float]) -> str:
        if m is None:
            return "[dim]N/A[/dim]"
        if m > 0:
            return f"[green]+{m:.2f}%[/green]"
        elif m < 0:
            return f"[red]{m:.2f}%[/red]"
        return f"[cyan]0.00%[/cyan]"

    # ── 终端面板构建 ──────────────────────────────────────────────────────

    def create_header(self) -> Panel:
        """创建顶部状态栏面板"""
        now = time.time()
        time_left = max(0, self.state.end_time - now)
        minutes = int(time_left // 60)
        seconds = int(time_left % 60)

        if time_left < 60:
            timer = f"[bold red]⏱️ {seconds}s[/bold red]"
        elif time_left < 180:
            timer = f"[yellow]⏱️ {minutes}:{seconds:02d}[/yellow]"
        else:
            timer = f"[green]⏱️ {minutes}:{seconds:02d}[/green]"

        status = (
            "[green]● LIVE[/green]" if self.state.connected
            else "[red]○ DISCONNECTED[/red]"
        )
        if getattr(self.config, "simulation", None) and self.config.simulation.enabled:
            mode = "[bold yellow]SIMULATION (no real orders)[/bold yellow]"
        else:
            mode = "[bold cyan]REAL TRADING[/bold cyan]"

        header = f"{timer}  |  {self.state.slug}  |  {status}  |  {mode}"
        im = self.config.market.interval_minutes
        return Panel(header, title=f"[bold]BTC {im}-Min Live Bot[/bold]")

    def create_token_panel(self, token: TokenData, label: str) -> Panel:
        """创建单个代币的订单簿面板"""
        if not token:
            return Panel("无数据", title=label)

        lines = []
        if token.best_ask > 0:
            lines.append(f"[red]ASK  {token.best_ask:.3f}[/red] | {token.best_ask_size:.0f}")
        else:
            lines.append("[red]ASK  ---[/red]")

        lines.append("─" * 20)
        lines.append(f"[bold white]LAST {token.last_price:.3f}[/bold white]")

        if token.best_ask > 0 and token.best_bid > 0:
            spread = token.best_ask - token.best_bid
            lines.append(f"[dim]Spread: {spread:.3f}[/dim]")

        lines.append("─" * 20)

        if token.best_bid > 0:
            lines.append(f"[green]BID  {token.best_bid:.3f}[/green] | {token.best_bid_size:.0f}")
        else:
            lines.append("[green]BID  ---[/green]")

        return Panel(
            "\n".join(lines),
            title=f"[bold]{label}[/bold] - {self._fmt_price(token.last_price)}",
            border_style="green" if "Up" in label else "red",
        )

    def create_indicators_panel(self, token: TokenData, label: str) -> Panel:
        """创建单个代币的指标面板"""
        if not token or not token.trades:
            return Panel("等待数据...", title=f"{label} 指标")

        mom_window = self.config.strategy.momentum_window_sec
        vwap_window = self.config.strategy.vwap_window_sec

        vwap = self.calc.calc_vwap(
            self.calc.get_trades_in_window(token.trades, vwap_window)
        )
        deviation = self.calc.calc_deviation(token.last_price, vwap)
        zscore = self.calc.calc_zscore(token.trades, token.last_price, window=5)
        momentum = self.calc.calc_momentum(
            token.trades, token.last_price, window=mom_window
        )

        def fmt_vol(v):
            if v >= 1_000_000:
                return f"{v / 1_000_000:.1f}M"
            elif v >= 1_000:
                return f"{v / 1_000:.1f}K"
            return f"{v:.0f}"

        lines = [
            f"VWAP {vwap_window}s:   {vwap:.4f}",
            f"偏差:   {self._fmt_dev(deviation)}",
            f"Z-Score 5s:  {self._fmt_zscore(zscore)}",
            f"动量 {mom_window}s:   {self._fmt_momentum(momentum)}",
            "",
            f"成交笔数:      {token.trade_count}",
            f"成交量:      {fmt_vol(token.volume_total)}",
            f"  买入:  [green]{fmt_vol(token.volume_buy)}[/green]",
            f"  卖出: [red]{fmt_vol(token.volume_sell)}[/red]",
        ]

        return Panel("\n".join(lines), title=f"{label} 指标", border_style="blue")

    def create_strategy_panel(self) -> Panel:
        """创建策略信号面板"""
        if not self.state.up_token or not self.state.down_token:
            return Panel("等待数据...", title="策略信号")

        up = self.state.up_token
        down = self.state.down_token

        vwap_window = self.config.strategy.vwap_window_sec
        up_vwap = self.calc.calc_vwap(
            self.calc.get_trades_in_window(up.trades, vwap_window)
        )
        down_vwap = self.calc.calc_vwap(
            self.calc.get_trades_in_window(down.trades, vwap_window)
        )

        up_dev = self.calc.calc_deviation(up.last_price, up_vwap)
        down_dev = self.calc.calc_deviation(down.last_price, down_vwap)

        mom_window = self.config.strategy.momentum_window_sec
        up_mom = self.calc.calc_momentum(up.trades, up.last_price, window=mom_window)
        down_mom = self.calc.calc_momentum(down.trades, down.last_price, window=mom_window)

        time_left = max(0, self.state.end_time - time.time())
        time_minutes = time_left / 60
        span = self.config.market.interval_minutes
        time_bin = int((span - 1) - time_minutes)
        time_bin = max(0, min(time_bin, span - 1))

        if up.last_price > down.last_price:
            fav_name = "UP"
            fav_price = up.last_price
            fav_dev = up_dev
            fav_mom = up_mom
        else:
            fav_name = "DOWN"
            fav_price = down.last_price
            fav_dev = down_dev
            fav_mom = down_mom

        base_wr = self.winrate_table.get_winrate(fav_price, time_bin, span)
        wr_str = f"{base_wr:.1f}%" if base_wr else "N/A"

        min_price = self.config.strategy.min_price
        max_price = self.config.strategy.max_price
        min_elapsed = self.config.strategy.min_elapsed_sec
        min_dev = self.config.strategy.min_deviation_pct
        max_dev = self.config.strategy.max_deviation_pct
        no_entry_cutoff = self.config.strategy.no_entry_before_end_sec

        elapsed_sec = self.config.market.duration_sec - time_left

        price_ok = min_price <= fav_price <= max_price
        time_ok = elapsed_sec >= min_elapsed
        dev_ok = fav_dev > min_dev and fav_dev < max_dev
        mom_ok = fav_mom is not None and fav_mom > 5
        time_cutoff_ok = time_left > no_entry_cutoff

        signal = "⏳ WAIT"
        signal_color = "yellow"

        if not time_cutoff_ok:
            signal = f"🚫 NO ENTRY (< {no_entry_cutoff}s 剩余)"
            signal_color = "red"
            self.last_signal = ""
        elif price_ok and time_ok and dev_ok and mom_ok:
            signal = f"✅ BUY {fav_name}"
            signal_color = "bold green"
            self.last_signal = f"BUY_{fav_name}"
        elif fav_price >= 0.70 and time_ok:
            if not mom_ok:
                signal = "🟡 ALMOST (需要动量>0%)"
            elif fav_dev >= max_dev:
                signal = f"🟡 ALMOST (偏差≥{max_dev}%)"
            else:
                signal = "🟡 ALMOST (需要偏差)"
            self.last_signal = ""
        else:
            self.last_signal = ""
            if not time_ok:
                signal = f"⏳ WAIT (已过<{min_elapsed}s)"
            elif not price_ok:
                signal = "⏳ WAIT (价格不在范围内)"
            elif not dev_ok:
                if fav_dev >= max_dev:
                    signal = f"⏳ WAIT (偏差≥{max_dev}%)"
                else:
                    signal = f"⏳ WAIT (偏差<{min_dev}%)"
            elif not mom_ok:
                signal = "⏳ WAIT (动量≤0%)"

        lines = [
            f"偏好:    [{signal_color}]{fav_name} ({fav_price:.3f})[/{signal_color}] — 胜率: [cyan]{wr_str}[/cyan]",
            f"信号:      [{signal_color}][bold]{signal}[/bold][/{signal_color}]",
            "",
            f"价格:       {self._fmt_price(fav_price)} (范围: {min_price}-{max_price})",
            f"偏差:   {self._fmt_dev(fav_dev)} (需要 {min_dev}%–{max_dev}%)",
            f"动量:    {self._fmt_momentum(fav_mom)}",
            f"已过:     {int(elapsed_sec)}s (需要 ≥{min_elapsed}s)  [分箱 {time_bin}]",
            "",
            f"Up:          {self._fmt_price(up.last_price)} | 偏差: {self._fmt_dev(up_dev)} | 动量: {self._fmt_momentum(up_mom)}",
            f"Down:        {self._fmt_price(down.last_price)} | 偏差: {self._fmt_dev(down_dev)} | 动量: {self._fmt_momentum(down_mom)}",
        ]

        title = (
            f"[bold]策略: P {min_price}-{max_price}, "
            f"T≥{min_elapsed}s, 偏差 {min_dev}%-{max_dev}%[/bold]"
        )
        border = "green" if signal_color == "bold green" else "magenta"
        return Panel("\n".join(lines), title=title, border_style=border)

    def create_trading_panel(self) -> Panel:
        """创建交易状态面板"""
        s = self.stats
        bet = self.config.entry.bet_amount_usd

        wr_str = f"{s.win_rate:.1f}%" if s.trade_count > 0 else "N/A"
        stats_line = f"📊 市场: {s.markets_seen} | 交易: {s.trade_count} | 胜率: {wr_str}"

        pnl_color = "green" if s.total_pnl >= 0 else "red"
        pnl_line = f"💰 盈亏: [{pnl_color}]${s.total_pnl:+.2f}[/{pnl_color}]"

        if s.position:
            pos = s.position
            if pos.token_name == "UP" and self.state.up_token:
                current_price = self.state.up_token.best_bid or self.state.up_token.last_price
            elif pos.token_name == "DOWN" and self.state.down_token:
                current_price = self.state.down_token.best_bid or self.state.down_token.last_price
            else:
                current_price = pos.entry_price

            unrealized = (pos.contracts * current_price) - (pos.contracts * pos.entry_price)
            ur_color = "green" if unrealized >= 0 else "red"

            hedge_str = " [cyan]🛡️ 已对冲[/cyan]" if pos.hedged else ""
            flash = "🔔 " if self.entry_flash else ""
            self.entry_flash = False

            pos_line = (
                f"{flash}🟢 做多 {pos.token_name} @ {pos.entry_price:.3f} "
                f"({pos.contracts} 张){hedge_str}"
            )
            ur_line = (
                f"   未实现: [{ur_color}]${unrealized:+.2f}[/{ur_color}] "
                f"(价格: {current_price:.3f})"
            )

            # 实时回撤
            dd_price = max(0, pos.entry_price - pos.min_price_seen)
            dd_pct = (dd_price / pos.entry_price * 100) if pos.entry_price > 0 else 0
            dd_usd = dd_price * pos.contracts
            if dd_price > 0:
                ur_line += (
                    f"\n   最大回撤: [red]-${dd_usd:.2f} (-{dd_pct:.1f}%)[/red] "
                    f"(最低: {pos.min_price_seen:.3f})"
                )
        else:
            pos_line = "⏳ 无持仓（等待信号）"
            ur_line = ""

        last_trades_lines = []
        for trade in s.trades[-3:][::-1]:
            icon = "✅" if trade.won else "❌"
            last_trades_lines.append(
                f"  {icon} {trade.token_name} @ {trade.entry_price:.2f} → ${trade.pnl:+.2f}"
            )

        lines = [stats_line, pnl_line, "", pos_line]
        if ur_line:
            lines.append(ur_line)
        if last_trades_lines:
            lines.append("")
            lines.append("最近交易:")
            lines.extend(last_trades_lines)

        border = "bold yellow" if self.entry_flash or self.hedge_flash else "cyan"
        self.hedge_flash = False
        return Panel(
            "\n".join(lines),
            title=f"[bold]💰 实盘交易 (${bet:.0f}/笔)[/bold]",
            border_style=border,
        )

    def create_btc_price_panel(self) -> Panel:
        """显示 Chainlink BTC/USD 价格及偏离面板"""
        s = self.state

        if s.btc_current_price <= 0:
            status = "[green]● LIVE[/green]" if s.btc_connected else "[red]○ OFF[/red]"
            return Panel(
                f"Chainlink {status}\n等待价格...",
                title="[bold]₿ BTC/USD (Chainlink)[/bold]",
                border_style="dim",
            )

        # 连接状态
        status = "[green]●[/green]" if s.btc_connected else "[red]○[/red]"

        # 新鲜度指示器
        age = time.time() - s.btc_last_update if s.btc_last_update > 0 else 999
        if age < 5:
            fresh = "[green]LIVE[/green]"
        elif age < 30:
            fresh = f"[yellow]{int(age)}秒前[/yellow]"
        else:
            fresh = f"[red]{int(age)}秒前[/red]"

        lines = [
            f"价格:       [bold white]${s.btc_current_price:,.2f}[/bold white]  {status} {fresh}",
        ]

        if s.btc_anchor_price > 0:
            dev_abs = s.btc_current_price - s.btc_anchor_price
            dev_pct = (dev_abs / s.btc_anchor_price) * 100 if s.btc_anchor_price else 0

            if dev_abs > 0:
                dev_abs_str = f"[green]+${dev_abs:,.2f}[/green]"
                dev_pct_str = f"[green]+{dev_pct:.3f}%[/green]"
            elif dev_abs < 0:
                dev_abs_str = f"[red]-${abs(dev_abs):,.2f}[/red]"
                dev_pct_str = f"[red]{dev_pct:.3f}%[/red]"
            else:
                dev_abs_str = "$0.00"
                dev_pct_str = "0.000%"

            lines.append(f"锚定:      [dim]${s.btc_anchor_price:,.2f}[/dim]")
            lines.append(f"偏离:   {dev_abs_str}  ({dev_pct_str})")
        else:
            lines.append("[dim]锚定: 等待市场开始...[/dim]")

        return Panel(
            "\n".join(lines),
            title="[bold]₿ BTC/USD (Chainlink)[/bold]",
            border_style="yellow",
        )

    def render(self) -> Layout:
        """渲染完整的终端布局"""
        layout = Layout()

        layout.split_column(
            Layout(self.create_header(), name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=16),
            Layout(self.create_btc_price_panel(), name="btc_price", size=6),
        )

        layout["body"].split_row(
            Layout(name="left"),
            Layout(name="right"),
        )

        layout["left"].split_column(
            Layout(self.create_token_panel(self.state.up_token, "⬆️ UP"), name="up_book"),
            Layout(self.create_indicators_panel(self.state.up_token, "UP"), name="up_ind"),
        )

        layout["right"].split_column(
            Layout(self.create_token_panel(self.state.down_token, "⬇️ DOWN"), name="down_book"),
            Layout(self.create_indicators_panel(self.state.down_token, "DOWN"), name="down_ind"),
        )

        layout["footer"].split_row(
            Layout(name="strategy"),
            Layout(name="trading"),
        )
        layout["strategy"].update(self.create_strategy_panel())
        layout["trading"].update(self.create_trading_panel())

        return layout

    # ── Web 快照构建器 ────────────────────────────────────────────────────

    def build_web_snapshot(self) -> dict:
        """构建 HTTP 仪表盘用的纯数据字典（与终端面板数据相同，不含 Rich 标记）"""
        now = time.time()
        time_left = max(0.0, self.state.end_time - now)
        sim = bool(
            getattr(self.config, "simulation", None)
            and self.config.simulation.enabled
        )
        header = {
            "slug": self.state.slug or "—",
            "time_left_sec": time_left,
            "elapsed_sec": max(0.0, self.config.market.duration_sec - time_left),
            "ws_connected": bool(self.state.connected),
            "simulation": sim,
            "interval_minutes": self.config.market.interval_minutes,
        }

        def token_block(token: Optional[TokenData]) -> Optional[dict]:
            if not token:
                return None
            book = {
                "best_bid": token.best_bid,
                "best_bid_size": token.best_bid_size,
                "best_ask": token.best_ask,
                "best_ask_size": token.best_ask_size,
                "last_price": token.last_price,
                "trade_count": token.trade_count,
                "volume_total": token.volume_total,
                "volume_buy": token.volume_buy,
                "volume_sell": token.volume_sell,
            }
            ind = None
            if token.trades:
                vw = self.config.strategy.vwap_window_sec
                mw = self.config.strategy.momentum_window_sec
                vwap = self.calc.calc_vwap(
                    self.calc.get_trades_in_window(token.trades, vw)
                )
                ind = {
                    "vwap_window_sec": vw,
                    "vwap": vwap,
                    "deviation_pct": self.calc.calc_deviation(token.last_price, vwap),
                    "zscore": self.calc.calc_zscore(token.trades, token.last_price, window=5),
                    "momentum_window_sec": mw,
                    "momentum_pct": self.calc.calc_momentum(
                        token.trades, token.last_price, window=mw
                    ),
                }
            return {"book": book, "indicators": ind}

        strategy: dict = {
            "signal_text": "等待数据...",
            "favorite": None,
            "win_rate_str": None,
            "checks": {},
            "up_line": "",
            "down_line": "",
        }

        if self.state.up_token and self.state.down_token:
            up = self.state.up_token
            down = self.state.down_token
            vwap_window = self.config.strategy.vwap_window_sec
            up_vwap = self.calc.calc_vwap(
                self.calc.get_trades_in_window(up.trades, vwap_window)
            )
            down_vwap = self.calc.calc_vwap(
                self.calc.get_trades_in_window(down.trades, vwap_window)
            )
            up_dev = self.calc.calc_deviation(up.last_price, up_vwap)
            down_dev = self.calc.calc_deviation(down.last_price, down_vwap)
            mom_window = self.config.strategy.momentum_window_sec
            up_mom = self.calc.calc_momentum(up.trades, up.last_price, window=mom_window)
            down_mom = self.calc.calc_momentum(down.trades, down.last_price, window=mom_window)

            time_minutes = time_left / 60.0
            span = self.config.market.interval_minutes
            time_bin = int((span - 1) - time_minutes)
            time_bin = max(0, min(time_bin, span - 1))

            if up.last_price > down.last_price:
                fav_name = "UP"
                fav_price = up.last_price
                fav_dev = up_dev
                fav_mom = up_mom
            else:
                fav_name = "DOWN"
                fav_price = down.last_price
                fav_dev = down_dev
                fav_mom = down_mom

            base_wr = self.winrate_table.get_winrate(fav_price, time_bin, span)
            wr_str = f"{base_wr:.1f}%" if base_wr else None

            min_price = self.config.strategy.min_price
            max_price = self.config.strategy.max_price
            min_elapsed = self.config.strategy.min_elapsed_sec
            min_dev = self.config.strategy.min_deviation_pct
            max_dev = self.config.strategy.max_deviation_pct
            no_entry_cutoff = self.config.strategy.no_entry_before_end_sec
            elapsed_sec = self.config.market.duration_sec - time_left

            price_ok = min_price <= fav_price <= max_price
            time_ok = elapsed_sec >= min_elapsed
            dev_ok = fav_dev > min_dev and fav_dev < max_dev
            mom_ok = fav_mom is not None and fav_mom > 5
            time_cutoff_ok = time_left > no_entry_cutoff

            if not time_cutoff_ok:
                signal = f"🚫 NO ENTRY (< {no_entry_cutoff}s 剩余)"
            elif price_ok and time_ok and dev_ok and mom_ok:
                signal = f"✅ BUY {fav_name}"
            elif fav_price >= 0.70 and time_ok:
                if not mom_ok:
                    signal = "🟡 ALMOST (需要动量>0%)"
                elif fav_dev >= max_dev:
                    signal = f"🟡 ALMOST (偏差≥{max_dev}%)"
                else:
                    signal = "🟡 ALMOST (需要偏差)"
            elif not time_ok:
                signal = f"⏳ WAIT (已过<{min_elapsed}s)"
            elif not price_ok:
                signal = "⏳ WAIT (价格不在范围内)"
            elif not dev_ok:
                signal = (
                    f"⏳ WAIT (偏差≥{max_dev}%)"
                    if fav_dev >= max_dev
                    else f"⏳ WAIT (偏差<{min_dev}%)"
                )
            elif not mom_ok:
                signal = "⏳ WAIT (动量≤0%)"
            else:
                signal = "⏳ WAIT"

            strategy = {
                "signal_text": signal,
                "favorite": f"{fav_name} ({fav_price:.3f})",
                "win_rate_str": wr_str,
                "time_bin": time_bin,
                "checks": {
                    "price": price_ok,
                    "time": time_ok,
                    "dev": dev_ok,
                    "mom": mom_ok,
                    "time_cutoff": time_cutoff_ok,
                },
                "up_line": (
                    f"{up.last_price:.3f} | 偏差 {up_dev:+.1f}% "
                    f"| 动量 {up_mom if up_mom is not None else 0:.2f}%"
                ),
                "down_line": (
                    f"{down.last_price:.3f} | 偏差 {down_dev:+.1f}% "
                    f"| 动量 {down_mom if down_mom is not None else 0:.2f}%"
                ),
            }

        s = self.state
        btc_age = time.time() - s.btc_last_update if s.btc_last_update > 0 else None
        btc_block: dict = {
            "btc_current_price": s.btc_current_price,
            "btc_anchor_price": s.btc_anchor_price,
            "btc_connected": s.btc_connected,
            "fresh_sec": btc_age,
            "deviation_line": "",
        }
        if s.btc_current_price > 0 and s.btc_anchor_price > 0:
            dev_abs = s.btc_current_price - s.btc_anchor_price
            dev_pct = (dev_abs / s.btc_anchor_price) * 100 if s.btc_anchor_price else 0.0
            btc_block["deviation_line"] = f"${dev_abs:+,.2f} ({dev_pct:+.3f}%)"

        # ── 交易数据：优先从数据库查询 ──────────────────────────────
        mode = "simulation" if sim else "live"
        bet = self.config.entry.bet_amount_usd

        db_summary = {"trade_count": 0, "wins": 0, "losses": 0,
                      "win_rate_pct": 0.0, "total_pnl_usd": 0.0,
                      "best_trade_pnl_usd": None, "worst_trade_pnl_usd": None}
        db_markets_seen = 0
        db_account = None
        if self._db:
            try:
                db_summary = self._db.get_trade_summary(mode=mode)
                db_markets_seen = self._db.get_markets_seen_count(mode=mode)
                db_account = self._db.get_account(mode=mode)
            except Exception:
                pass

        wr_str = f"{db_summary['win_rate_pct']:.1f}%" if db_summary['trade_count'] > 0 else None
        trading: dict = {
            "bet_usd": bet,
            "markets_seen": db_markets_seen,
            "trade_count": db_summary["trade_count"],
            "wins": db_summary["wins"],
            "losses": db_summary["losses"],
            "win_rate_str": wr_str,
            "total_pnl": db_summary["total_pnl_usd"],
            "best_trade_pnl": db_summary["best_trade_pnl_usd"],
            "worst_trade_pnl": db_summary["worst_trade_pnl_usd"],
            "account": {
                "initial_capital": db_account["initial_capital"] if db_account else 0,
                "current_capital": db_account["current_capital"] if db_account else 0,
                "realized_pnl": db_account["realized_pnl"] if db_account else 0,
            } if db_account else None,
            "position": None,
            "recent_trades": [],
        }

        # 最近交易：从数据库取
        if self._db:
            try:
                recent_rows = self._db.get_trades(mode=mode, limit=5)
                for r in reversed(recent_rows):  # 按时间升序
                    icon = "✅" if r["won"] else "❌"
                    trading["recent_trades"].append({
                        "line": f"{icon} {r['token_name']} @ {r['entry_price']:.2f} → ${r['pnl']:+.2f}",
                        "market_slug": r["market_slug"],
                        "timestamp": r["timestamp"],
                    })
            except Exception:
                pass

        # 当前持仓（运行时数据，不在数据库中）
        st = self.stats
        if st.position:
            pos = st.position
            if pos.token_name == "UP" and self.state.up_token:
                current_price = self.state.up_token.best_bid or self.state.up_token.last_price
            elif pos.token_name == "DOWN" and self.state.down_token:
                current_price = self.state.down_token.best_bid or self.state.down_token.last_price
            else:
                current_price = pos.entry_price
            unrealized = (pos.contracts * current_price) - (
                pos.contracts * pos.entry_price
            )
            dd_price = max(0.0, pos.entry_price - pos.min_price_seen)
            dd_pct = (dd_price / pos.entry_price * 100) if pos.entry_price > 0 else 0.0
            dd_usd = dd_price * pos.contracts
            trading["position"] = {
                "token_name": pos.token_name,
                "entry_price": pos.entry_price,
                "contracts": pos.contracts,
                "hedged": pos.hedged,
                "current_price": current_price,
                "unrealized_pnl": unrealized,
                "max_dd_usd": dd_usd,
                "max_dd_pct": dd_pct,
                "min_price_seen": pos.min_price_seen,
            }
        return {
            "ts": now,
            "header": header,
            "strategy": strategy,
            "up": token_block(self.state.up_token),
            "down": token_block(self.state.down_token),
            "btc": btc_block,
            "trading": trading,
            "last_signal": self.last_signal,
        }
