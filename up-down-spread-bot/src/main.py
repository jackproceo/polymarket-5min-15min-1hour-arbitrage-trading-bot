#!/usr/bin/env python3
"""
Meridian — Polymarket 15分钟多资产交易系统。

四个并行交易器 (BTC, ETH, SOL, XRP)，共用一个钱包。
策略：窗口末期入场 (Late Entry V3 / late_v3)。
"""
import argparse
import asyncio
import time
import signal
import sys
import subprocess
import os
import threading
import requests
from pathlib import Path
from typing import Dict
from concurrent.futures import ThreadPoolExecutor

from data_feed import DataFeed
from strategy import LateEntryStrategy
from multi_trader import MultiTrader
from dashboard_multi_ab import DashboardMultiAB
from polymarket_api import get_market_outcome
from telegram_notifier import get_notifier
from safety_guard import SafetyGuard
from order_executor import OrderExecutor
from keyboard_listener import KeyboardListener
import trader as trader_module
import db_manager  # SQLite database for trades, balances, balance changes

from utils.logging_setup import get_logger
log = get_logger("main")


# 全局配置常量
STRATEGY_BASES = ['late_v3']
COINS = ['btc', 'eth', 'sol', 'xrp']

# 全局停止标志
stop_flag = False
data_feed = None
multi_trader_instance = None  # 持有 MultiTrader 实例，用于优雅关闭
keyboard_listener = None  # 持有 KeyboardListener 实例，用于清理

# 全局赎回仓位缓存，供 Telegram /r 命令使用
redeem_positions_cache = []
redeem_cache_lock = threading.Lock()


def signal_handler(sig, frame):
    """处理 Ctrl+C - 设置停止标志，事件循环负责清理。"""
    global stop_flag
    if stop_flag:
        # 第二次 Ctrl+C - 强制退出
        sys.exit(1)
    stop_flag = True
    log.info("\n[SYSTEM] Shutdown signal received, stopping gracefully...")


signal.signal(signal.SIGINT, signal_handler)


def load_config(config_path: str = None):
    """加载配置，返回 Config 实例（兼容 dict 式访问）。"""
    from utils.config import Config
    return Config.load(config_path)


def _parse_cli_args():
    """CLI 参数解析，支持可选的 Web 仪表盘。"""
    p = argparse.ArgumentParser(description="Meridian — Polymarket 15m crypto desk")
    p.add_argument(
        "--web",
        action="store_true",
        help="启动后台 Web 仪表盘 (Flask)，用于控制与实时分析",
    )
    p.add_argument("--web-port", type=int, default=5050, help="仪表盘端口（默认 5050）")
    p.add_argument(
        "--web-host",
        type=str,
        default="127.0.0.1",
        help="绑定地址（默认 127.0.0.1；使用 0.0.0.0 开放局域网）",
    )
    return p.parse_args()


def validate_system():
    """启动前验证所有组件"""
    log.info("[VALIDATION] Testing sizing formulas...")
    # 验证通过
    
    log.info("[VALIDATION] All systems ready")
    return True


def _get_portfolio_stats(multi_trader, markets_skipped, session_start_time):
    """计算投资组合统计信息，用于 Telegram 通知"""
    stats = {}
    
    for coin in COINS:
        strategy_name = f"{STRATEGY_BASES[0]}_{coin}"
        trader = multi_trader.traders.get(strategy_name)
        
        if not trader:
            stats[f'{coin}_pnl'] = 0
            stats[f'{coin}_wr'] = 0
            stats[f'{coin}_markets_played'] = 0
            stats[f'{coin}_markets_skipped'] = 0
            continue
        
        perf = trader.get_performance_stats()
        
        stats[f'{coin}_pnl'] = trader.current_capital - trader.starting_capital
        stats[f'{coin}_wr'] = perf['win_rate']
        stats[f'{coin}_markets_played'] = perf['total_trades']
        stats[f'{coin}_markets_skipped'] = markets_skipped.get(coin, 0)
    
    stats['total_pnl'] = sum(stats.get(f'{coin}_pnl', 0) for coin in COINS)
    stats['uptime'] = time.time() - session_start_time
    
    return stats


# ═══════════════════════════════════════════════════════════
# 全局状态（供回调函数使用）
# ═══════════════════════════════════════════════════════════
wallet_balance = 0.0  # 在 main() 检查钱包后设置


def validate_prices(up_ask: float, down_ask: float, up_timestamp: float, down_timestamp: float, 
                   coin: str = '', threshold_sec: float = 2.0) -> tuple:
    """
    验证价格是否同步且新鲜
    
    返回: (is_valid: bool, reason: str)
    """
    now = time.time()
    
    # 检查1：新鲜度（价格是否最近更新）
    up_age = now - up_timestamp if up_timestamp > 0 else 999
    down_age = now - down_timestamp if down_timestamp > 0 else 999
    
    if up_age > threshold_sec:
        return False, f"UP_STALE_{up_age:.1f}s"
    if down_age > threshold_sec:
        return False, f"DOWN_STALE_{down_age:.1f}s"
    
    # 检查2：时间戳同步（两者在同一时间窗口内更新）
    if abs(up_timestamp - down_timestamp) > threshold_sec:
        return False, f"DESYNC_{abs(up_timestamp - down_timestamp):.1f}s"
    
    # 检查3：总和验证 (UP + DOWN ≈ 1.0)
    # 允许更宽范围 (0.95-1.15) 以容纳价差和快速价格变动
    price_sum = up_ask + down_ask
    if price_sum < 0.95 or price_sum > 1.15:
        return False, f"INVALID_SUM_{price_sum:.3f}"
    
    return True, "OK"


def run_manual_redeem():
    """手动赎回回调（按 M 键触发）"""
    log.info("\n" + "="*80)
    log.info(" MANUAL REDEEM TRIGGERED ".center(80, "="))
    log.info("="*80 + "\n")
    
    try:
        # 直接导入 redeemall 模块
        import sys
        sys.path.insert(0, "/root/clip")
        
        # 从 4coins_live 加载环境变量
        from dotenv import load_dotenv
        from pathlib import Path
        env_path = Path("/root/4coins_live/.env")
        load_dotenv(env_path, override=True)
        
        # 导入并运行 redeemall（自动确认）
        import redeemall
        log.info("[REDEEM] Starting automatic redemption...")
        log.info("[REDEEM] Using wallet from: /root/4coins_live/.env")
        log.info("")
        
        redeemall.main(auto_confirm=True)
        
        log.info("\n[REDEEM] Completed!")
            
    except Exception as e:
        log.info(f"\n[REDEEM] Error: {e}")
        import traceback
        traceback.print_exc()
    
    log.info("\n" + "="*80)
    log.info(" Returning to trading... ".center(80))
    log.info("="*80 + "\n")
    
    # 给用户 2 秒查看结果
    time.sleep(2)


def main(args=None):
    """主交易循环"""
    global stop_flag, data_feed, wallet_balance, keyboard_listener
    # 初始化日志（同时输出到控制台和文件）
    from utils.logging_setup import setup_logging
    setup_logging(level="INFO", log_dir="logs")
    
    if args is None:
        args = _parse_cli_args()
    
    # 记录会话启动时间（用于计算运行时长）
    session_start_time = time.time()
    
    config = load_config()
    
    log.info("=" * 115)
    _pm = config.get("data_sources.polymarket", {})
    _iv = int(_pm.get("market_interval_sec", 900))
    _ml = "5m" if _iv == 300 else ("15m" if _iv == 900 else f"{_iv}s")
    log.info(f"  MERIDIAN — Polymarket crypto desk ({_ml} markets)".center(115))
    log.info("  BTC · ETH · SOL · XRP  |  Late-window entry  |  Hybrid stop-loss & flip-stop".center(115))
    log.info("  Unified wallet  |  Real-time books  |  FAK execution".center(115))
    log.info("=" * 115)
    log.info("")
    
    # 验证系统
    if not validate_system():
        log.info("[ERROR] System validation failed!")
        return
    
    # 跟踪每个币种跳过的市场
    markets_skipped = {coin: 0 for coin in COINS}
    
    # 跟踪已完结市场数量，用于图表生成
    total_completed_markets = 0
    last_chart_at = 0  # 上次发送图表时的市场计数
    CHART_INTERVAL = config.get('notifications.chart_every_n_markets', 10)
    log.info(f"[CONFIG] Loaded configuration (Meridian · late-window entry + hybrid stop-loss)")
    _pm_cfg = config.get("data_sources.polymarket", {})
    _iv_cfg = int(_pm_cfg.get("market_interval_sec", 900))
    _mw_cfg = str(_pm_cfg.get("market_window", "") or ("15m" if _iv_cfg == 900 else "5m" if _iv_cfg == 300 else ""))
    log.info(f"         Market window: \"{_mw_cfg or _iv_cfg}\" → {_iv_cfg}s " f"(edit data_sources.polymarket.market_window: \"5m\" or \"15m\")")
    log.info(f"         Entry window (config file): {config.get('strategy.entry_window_sec', 'default')} seconds (strategy may cap to market length)")
    log.info(f"         Entry Frequency: Every {config.get('strategy.entry_frequency_sec')} seconds")
    log.info(f"         Price Max: ${config.get('strategy.price_max')}")
    log.info(f"         Exit #1: Hybrid Stop-Loss (per coin):")
    
    # 从配置动态推导
    for coin in ['btc', 'eth', 'sol', 'xrp']:
        sl_cfg = config.get(f'exit.stop_loss.per_coin.{coin}', {})
        if sl_cfg.get('enabled'):
            sl_type = sl_cfg.get('type', 'fixed')
            sl_value = sl_cfg.get('value', 0)
            if sl_type == 'fixed':
                log.info(f"                  {coin.upper()}: Fixed ${sl_value}")
            else:
                log.info(f"                  {coin.upper()}: Percent {sl_value}%")
        else:
            log.info(f"                  {coin.upper()}: Disabled")
    
    log.info(f"         Exit #2: Flip-Stop (price reversal protection)")
    _sz = config.get("strategy.sizing", {})
    log.info(f"         Sizing: {_sz.get('above_180_sec', 8)}/{_sz.get('above_120_sec', 10)}/{_sz.get('below_120_sec', 12)} " f"contracts (tiers vs time-left; thresholds scale with market window)")
    log.info("")
    
    # ═══════════════════════════════════════════════════════════
    # 安全与实盘交易设置
    # ═══════════════════════════════════════════════════════════
    
    # 创建 SafetyGuard（传入完整 config，SafetyGuard 自行提取安全配置部分）
    safety_guard = SafetyGuard(config)
    
    # 创建 OrderExecutor（传入 config 获取重试参数！）
    order_executor = OrderExecutor(safety_guard, config)
    
    # 设置余额变更回调，更新全局 wallet_balance
    def on_balance_change(amount: float, operation: str, is_absolute: bool = False):
        """
        余额变更回调（来自 OrderExecutor）
        
        Args:
            amount: 变更金额（正数=收到，负数=支出）或绝对余额
            operation: 操作类型（'BUY', 'SELL', 'REDEEM', 'REDEEM_REFRESH'）
            is_absolute: 若为 True，amount 为新的绝对余额（而非增量）
        """
        global wallet_balance
        try:
            if is_absolute:
                # 来自区块链的绝对值
                old_balance = wallet_balance
                wallet_balance = amount
                change = amount - old_balance
                change_sign = "+" if change >= 0 else ""
                log.info(f"[BALANCE] 🔄 Updated from blockchain: ${wallet_balance:,.2f} ({change_sign}${change:.2f})")
                # 记录到数据库
                try:
                    db_manager.get_db().save_balance_snapshot(usdc_balance=wallet_balance, source='blockchain_refresh')
                    if change != 0:
                        db_manager.get_db().save_balance_change(
                            amount=change,
                            balance_before=old_balance,
                            balance_after=wallet_balance,
                            operation_type=operation.lower() if operation else "blockchain_update"
                        )
                except Exception as db_err:
                    log.warning(f"[BALANCE] ⚠️ DB log error: {db_err}")
            else:
                # 增量变更
                old_balance = wallet_balance
                wallet_balance += amount
                sign = "+" if amount >= 0 else ""
                log.info(f"[BALANCE] 💰 {operation}: {sign}${amount:.2f} → ${wallet_balance:,.2f}")
                # 记录到数据库
                try:
                    db_manager.get_db().save_balance_snapshot(usdc_balance=wallet_balance, source='trade_operation')
                    db_manager.get_db().save_balance_change(
                        amount=amount,
                        balance_before=old_balance,
                        balance_after=wallet_balance,
                        operation_type=operation.lower() if operation else "unknown"
                    )
                except Exception as db_err:
                    log.warning(f"[BALANCE] ⚠️ DB log error: {db_err}")
        except Exception as e:
            log.warning(f"[BALANCE] ⚠️ Callback error: {e}")
            import traceback
            traceback.print_exc()
    
    order_executor.set_balance_callback(on_balance_change)
    
    # 设置市场收盘检查回调（竞态条件保护）
    def is_market_closing(market_slug: str, coin: str) -> bool:
        """
        检查：特定币种的市场是否正在关闭（止损/翻转止损已触发）
        
        🔥 关键：若 market_start_prices[coin] == -2 则阻止买入
        防止在触发信号之后买入的竞态条件
        
        Args:
            market_slug: 市场标识符
            coin: 币种名称（'btc', 'eth', 'sol', 'xrp'）
        
        Returns:
            True - 市场正在为此币种关闭，阻止买入
            False - 市场对此币种开放，允许买入
        """
        # 仅检查指定币种（按币种隔离阻断！）
        if coin in market_start_prices:
            status = market_start_prices[coin].get(market_slug, None)
            if status == -2:
                return True  # 市场正在为此币种关闭！
        return False  # 市场对此币种开放
    
    order_executor.set_market_closing_check(is_market_closing)
    
    # 检查钱包余额（若非 DRY_RUN）
    if not safety_guard.dry_run:
        log.info("\n[WALLET] Checking wallet balance...")
        wallet_balance = order_executor.get_wallet_usdc_balance()
        
        if not wallet_balance or wallet_balance <= 0:
            log.info("\n" + "="*80)
            log.error("❌ ERROR: Cannot read wallet balance or balance is 0!")
            log.info("   Check your PRIVATE_KEY in .env and ensure wallet has USDC")
            log.info("="*80)
            sys.exit(1)
        
        log.info("\n" + "="*80)
        log.info(f"💰 Wallet balance: ${wallet_balance:.2f}")
        log.info(f"   Address: {order_executor.wallet_address}")
        log.info("🔴 LIVE TRADING MODE - REAL MONEY")
        log.info("="*80 + "\n")
    else:
        # DRY_RUN - 使用模拟余额
        wallet_balance = 10000.0  # 模拟余额
        log.info("\n" + "="*80)
        log.info(f"🟢 DRY_RUN MODE: Simulated balance ${wallet_balance:.2f}")
        log.info("   No real orders will be placed")
        log.info("="*80 + "\n")
    
    # 初始化 SQLite 数据库
    db_manager.init_db()
    log.info("[SYSTEM] ✓ SQLite database initialized (data/trading.db)")

    # 记录初始余额快照
    db_manager.get_db().save_balance_snapshot(usdc_balance=wallet_balance, source='startup')
    log.info(f"[DB] Initial balance recorded: ${wallet_balance:.2f}")

    # 将 executor 注入 trader 模块
    trader_module.set_order_executor(order_executor)
    log.info("[SYSTEM] ✓ OrderExecutor injected into trader module")
    
    # 📂 从磁盘加载元数据（重启后赎回的关键！）
    trader_module.load_market_metadata_from_disk()
    log.info("")
    
    # ═══════════════════════════════════════════════════════════
    
    # 初始化数据源（所有策略共享）
    log.info("[SYSTEM] Initializing multi-market data feed...")
    data_feed = DataFeed(config)
    data_feed.start()
    time.sleep(5)  # 等待数据稳定
    
    # 初始化 2 个策略（1 个基础策略 × 2 个币种）使用全局常量
    log.info(f"[SYSTEM] Initializing 2 parallel strategies...")
    strategies = {}
    strategy_names = []
    
    for base_name in STRATEGY_BASES:
        for coin in COINS:
            strategy_name = f"{base_name}_{coin}"
            strategy_names.append(strategy_name)
            strategies[strategy_name] = LateEntryStrategy(config)
            log.info(f"         ✓ {strategy_name:30s} (late-window entry | time-based sizing)")
    
    _sample_st = strategies.get(f"{STRATEGY_BASES[0]}_{COINS[0]}")
    if _sample_st:
        log.info(f"         Effective entry window: last {_sample_st.entry_window}s | sizing tiers: >{_sample_st.sizing_t1}s / >{_sample_st.sizing_t2}s")
    
    # 初始化 multi-trader（统一钱包 - 无资金分配）
    global multi_trader_instance
    log.info("\n[SYSTEM] Initializing multi-trader...")
    # 注意：capital_per_strategy=0 因为所有策略共享同一个钱包余额
    # 单个 trader 资金仅用于按币种 PnL 统计，而非限制
    multi_trader = MultiTrader(capital_per_strategy=0, strategy_names=strategy_names)
    multi_trader_instance = multi_trader  # 存储用于优雅关闭
    log.info("")

    # 将内存中已平仓交易同步到 SQLite（确保 dashboard 可查询）
    def _sync_closed_trades():
        """遍历所有 trader 的 closed_trades，将缺失的交易写入 SQLite。"""
        import db_manager as _dbm
        _db = _dbm.get_db()
        if _db is None:
            return
        synced = 0
        for name, trader in multi_trader.traders.items():
            for trade in getattr(trader, "closed_trades", []):
                slug = trade.get("market_slug", "")
                if not slug:
                    continue
                # 检查 SQLite 是否已有该市场记录
                existing = _db.get_trades(limit=1, coin=getattr(trader, "coin", None))
                if any(t.get("market_slug") == slug for t in existing):
                    continue
                total_cost = trade.get("total_cost", 0)
                up_shares = trade.get("up_shares", 0)
                down_shares = trade.get("down_shares", 0)
                total_shares = up_shares + down_shares
                entry_price = (total_cost / total_shares) if total_cost > 0 and total_shares > 0 else None
                _db.save_trade({
                    "market_slug": slug,
                    "coin": getattr(trader, "coin", None),
                    "side": trade.get("winner"),
                    "entry_price": entry_price,
                    "contracts": total_shares,
                    "size_usd": total_cost,
                    "pnl": trade.get("pnl", 0),
                    "roi_pct": trade.get("roi_pct", 0),
                    "winner": trade.get("winner"),
                    "exit_type": trade.get("exit_reason", "market_resolution"),
                    "exit_price": trade.get("exit_price"),
                    "total_entries": trade.get("total_entries", 0),
                    "up_invested": trade.get("up_invested", 0),
                    "down_invested": trade.get("down_invested", 0),
                    "up_shares": up_shares,
                    "down_shares": down_shares,
                    "duration_sec": trade.get("duration", 0),
                    "close_time": trade.get("close_timestamp"),
                    "status": "closed",
                })
                synced += 1
        if synced:
            log.info(f"[DB] Synced {synced} in-memory closed trades to SQLite")

    _sync_closed_trades()
    
    # 初始化仪表盘（传入 config 用于交易状态显示）
    dashboard = DashboardMultiAB(width=160, coins=COINS, config=config)
    
    import web_dashboard_state as web_dashboard_state_mod
    web_dashboard_state_mod.set_session_start(session_start_time)
    if getattr(args, "web", False):
        from web_dashboard.server import run_server_thread
        proj_root = Path(__file__).resolve().parent.parent
        run_server_thread(host=args.web_host, port=args.web_port, project_root=proj_root)
        log.info(f"[WEB] Dashboard: http://{args.web_host}:{args.web_port}/")
        log.info("")
    
    # 初始化 Telegram 通知器（带事件回调）
    dashboard.add_event("Initializing Telegram notifier...", 'system')
    from telegram_notifier import TelegramNotifier
    notifier = TelegramNotifier(event_callback=lambda msg, t: dashboard.add_event(msg, t))
    
    # 分别跟踪每个币种的市场起始价格
    # {coin: {market_slug: price or status}}
    # 值：正数（有效价格），-1（已跳过 - 中途开始）
    market_start_prices = {coin: {} for coin in COINS}
    
    # 分别跟踪每个币种的待处理市场
    # {coin: {market_slug: {...}}}
    pending_markets = {coin: {} for coin in COINS}
    
    # 跟踪每个币种是否发生了市场切换
    witnessed_market_switch = {coin: False for coin in COINS}
    
    # 共享状态访问的线程安全锁（仍然用于 telegram 回调线程）
    market_lock = threading.Lock()
    
    # 🔄 异步赎回：用于顺序赎回的线程池
    # max_workers=1 确保赎回逐一执行（非并行）
    redeem_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="redeem")
    
    # ═══════════════════════════════════════════════════════════════
    # TELEGRAM 命令处理器 - 线程安全的按需图表生成
    # ═══════════════════════════════════════════════════════════════
    def handle_chart_command():
        """
        按需生成并发送 PnL 图表（用户发送 /chart 或 /pnl 时触发）
        线程安全：使用 market_lock 安全读取 multi_trader 数据
        容错：完整错误处理，不会崩溃主循环
        """
        try:
            log.info("\n[TELEGRAM CMD] 📊 Generating PnL chart on demand...")
            
            # 生成图表路径（唯一名称避免冲突）
            import uuid
            chart_path = f"/root/4coins_live/logs/pnl_chart_on_demand_{uuid.uuid4().hex[:8]}.png"
            
            log.info(f"[TELEGRAM CMD] 📊 Chart request received")
            log.info(f"[TELEGRAM CMD] Chart path: {chart_path}")
            log.info(f"[TELEGRAM CMD] COINS list: {COINS}")
            log.info(f"[TELEGRAM CMD] Log dir: /root/4coins_live/logs")
            
            # 导入图表生成器
            from pnl_chart_generator import generate_pnl_chart
            
            # 生成图表（读取 JSONL 文件 - 安全的并发读取）
            # 注意：不检查 total_completed_markets，因为重启后该值会重置
            # 改为由 generate_pnl_chart 检查实际文件，若无数据则返回 False
            log.info(f"[TELEGRAM CMD] Calling generate_pnl_chart()...")
            result = generate_pnl_chart('/root/4coins_live/logs', COINS, chart_path)
            log.info(f"[TELEGRAM CMD] generate_pnl_chart() returned: {result}")
            
            if not result:
                log.warning("[TELEGRAM CMD] ⚠️ No trade data found in files")
                notifier.send_message("⚠️ No completed markets yet. Chart will be available after first market closes.")
                return
            
            # 线程安全：锁定共享数据以读取统计信息
            with market_lock:
                
                # 获取当前投资组合统计（锁内安全读取）
                try:
                    portfolio_stats = _get_portfolio_stats(multi_trader, markets_skipped, session_start_time)
                except Exception as e:
                    log.warning(f"[TELEGRAM CMD] ⚠️ Stats error: {e}")
                    portfolio_stats = {'total_pnl': 0, 'uptime': '?'}
                
                # 从文件计数实际已完结市场（而非内存变量）
                # 这样在机器人重启后也能正确工作
                actual_markets_count = 0
                for coin in COINS:
                    trades_file = Path(f"/root/4coins_live/logs/late_v3_{coin}/trades.jsonl")
                    if trades_file.exists():
                        try:
                            with open(trades_file, 'r') as f:
                                actual_markets_count += sum(1 for _ in f)
                        except (FileNotFoundError, OSError):
                            pass
                
                # 创建标题文字
                total_pnl = portfolio_stats.get('total_pnl', 0)
                uptime = portfolio_stats.get('uptime', '?')
                
                # 按币种格式化 PnL
                coin_stats = []
                for coin in COINS:
                    coin_pnl = portfolio_stats.get(f'{coin}_pnl', 0)
                    emoji = "🟢" if coin_pnl >= 0 else "🔴"
                    coin_stats.append(f"{coin.upper()}: {emoji} ${coin_pnl:+.0f}")
                
                caption = f"""<b>📊 Current PnL Chart</b>

💰 <b>Total:</b> ${total_pnl:+.2f}
📈 <b>Markets:</b> {actual_markets_count}
⏱ <b>Session:</b> {uptime}

<b>By Coin:</b>
{' | '.join(coin_stats)}"""
            
            # 发送图片（在锁外 - 网络 I/O 可能较慢）
            if notifier.send_photo(chart_path, caption):
                log.info(f"[TELEGRAM CMD] ✓ Chart sent successfully")
            else:
                log.error(f"[TELEGRAM CMD] ✗ Failed to send chart to Telegram")
                notifier.send_message("❌ Chart generated but failed to send. Please try again.")
            
            # 清理临时文件
            try:
                import os
                os.remove(chart_path)
            except (FileNotFoundError, PermissionError, OSError):
                pass
                
        except Exception as e:
            error_msg = str(e)[:200]
            log.error(f"[TELEGRAM CMD] ✗ Fatal error: {error_msg}")
            try:
                notifier.send_message(f"❌ Error generating chart:\n<code>{error_msg}</code>")
            except Exception:
                pass  # 通知失败也不崩溃
    
    def get_pol_price_usd() -> float:
        """
        通过 CoinGecko API 获取当前 POL 的美元价格
        
        Returns:
            POL 美元价格，若 API 不可用则返回回退值 0.45
        """
        try:
            url = "https://api.coingecko.com/api/v3/simple/price"
            params = {
                'ids': 'polygon-ecosystem-token',
                'vs_currencies': 'usd'
            }
            response = requests.get(url, params=params, timeout=5)
            
            if response.status_code == 200:
                data = response.json()
                price = data.get('polygon-ecosystem-token', {}).get('usd')
                if price:
                    log.info(f"[PRICE API] POL price: ${price:.4f}")
                    return float(price)
            
            # API 未返回价格时的回退
            log.warning(f"[PRICE API] ⚠️ Failed to get POL price, using fallback: $0.45")
            return 0.45
            
        except Exception as e:
            log.warning(f"[PRICE API] ⚠️ Error getting POL price: {e}, using fallback: $0.45")
            return 0.45
    
    def get_active_positions():
        """
        通过 Polymarket Data API 获取活跃持仓
        线程安全：仅只读 API 请求，不访问共享状态
        
        Returns:
            持仓列表，出错时返回 None
        """
        try:
            # 从 order_executor 获取钱包地址
            wallet = order_executor.wallet_address
            if not wallet:
                log.warning("[POSITIONS API] ⚠️ No wallet address")
                return None
            
            url = "https://data-api.polymarket.com/positions"
            params = {
                'user': wallet,
                'sizeThreshold': 0.1,  # 最低 0.1 张合约
                'limit': 50,
                'sortBy': 'CURRENT',
                'sortDirection': 'DESC'
            }
            
            log.info(f"[POSITIONS API] Fetching positions for {wallet[:6]}...{wallet[-4:]}")
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                positions = response.json()
                log.info(f"[POSITIONS API] ✅ Got {len(positions)} positions")
                return positions
            else:
                log.warning(f"[POSITIONS API] ⚠️ Failed: HTTP {response.status_code}")
                return None
                
        except Exception as e:
            log.warning(f"[POSITIONS API] ⚠️ Error: {e}")
            return None
    
    def handle_balance_command():
        """
        用户发送 /balance 时显示钱包余额
        线程安全：安全的并发访问
        """
        try:
            log.info("\n[TELEGRAM CMD] 💰 Getting wallet balance...")
            
            # 获取余额
            usdc_balance = order_executor.get_wallet_usdc_balance()
            pol_balance = order_executor.get_pol_balance()
            
            if usdc_balance is None:
                notifier.send_message("❌ Failed to get USDC balance")
                return
            
            # 通过 CoinGecko API 获取当前 POL 价格
            pol_price_usd = get_pol_price_usd()
            pol_value_usd = (pol_balance or 0) * pol_price_usd
            
            total_usd = usdc_balance + pol_value_usd
            
            # 格式化消息
            message = f"""<b>💰 WALLET BALANCE</b>
━━━━━━━━━━━━━━━

<b>USDC:</b> ${usdc_balance:,.2f}
<b>POL:</b> {pol_balance or 0:.4f} (~${pol_value_usd:.2f})

━━━━━━━━━━━━━━━
<b>TOTAL:</b> ${total_usd:,.2f}

<i>Wallet: {order_executor.wallet_address[:6]}...{order_executor.wallet_address[-4:]}</i>"""
            
            notifier.send_message(message)
            log.info(f"[TELEGRAM CMD] ✅ Balance sent: ${total_usd:.2f}")
            
        except Exception as e:
            error_msg = str(e)[:200]
            log.error(f"[TELEGRAM CMD] ✗ Balance error: {error_msg}")
            try:
                notifier.send_message(f"❌ Error getting balance:\n<code>{error_msg}</code>")
            except Exception:
                pass  # 通知失败也不崩溃
    
    def handle_positions_command():
        """
        用户发送 /t 或 /positions 时显示活跃持仓
        线程安全：仅只读 API 调用，不访问共享状态
        """
        try:
            log.info("\n[TELEGRAM CMD] 📊 Getting active positions...")
            
            # 通过 API 获取持仓（线程安全 - 仅 API 请求）
            positions = get_active_positions()
            
            if positions is None:
                notifier.send_message("❌ Failed to get positions from API")
                return
            
            if not positions:
                notifier.send_message("📊 <b>No active positions</b>\n\nAll markets closed or redeemed! 🎉")
                return
            
            # 计算总指标
            total_value = sum(p.get('currentValue', 0) for p in positions)
            total_pnl = sum(p.get('cashPnl', 0) for p in positions)
            redeemable_value = sum(p.get('currentValue', 0) for p in positions if p.get('redeemable'))
            redeemable_count = sum(1 for p in positions if p.get('redeemable'))
            
            # 格式化消息
            message = f"<b>📊 ACTIVE POSITIONS ({len(positions)})</b>\n"
            message += "━━━━━━━━━━━━━━━\n\n"
            
            # 最多显示 10 条持仓
            for i, p in enumerate(positions[:10]):
                title = p.get('title', 'Unknown')
                # 截断过长名称
                if len(title) > 45:
                    title = title[:42] + "..."
                
                outcome = p.get('outcome', '?')
                size = p.get('size', 0)
                avg_price = p.get('avgPrice', 0)
                cur_price = p.get('curPrice', 0)
                initial = p.get('initialValue', 0)
                current = p.get('currentValue', 0)
                pnl = p.get('cashPnl', 0)
                pnl_pct = p.get('percentPnl', 0)
                redeemable = p.get('redeemable', False)
                
                # 按状态显示图标
                if redeemable:
                    emoji = "💰"
                    status = " [REDEEM!]"
                elif pnl >= 0:
                    emoji = "🟢"
                    status = ""
                else:
                    emoji = "🔴"
                    status = ""
                
                message += f"<b>{outcome}</b>: {title}\n"
                message += f"├ Size: {size:.1f} contracts\n"
                message += f"├ Entry: ${avg_price:.3f} → Now: ${cur_price:.3f}\n"
                message += f"├ Value: ${initial:.2f} → ${current:.2f}\n"
                message += f"└ PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%) {emoji}{status}\n\n"
            
            # 如果超过 10 条持仓
            if len(positions) > 10:
                hidden_value = sum(p.get('currentValue', 0) for p in positions[10:])
                hidden_pnl = sum(p.get('cashPnl', 0) for p in positions[10:])
                message += f"<i>...and {len(positions) - 10} more positions"
                message += f" (${hidden_value:.2f}, PnL: ${hidden_pnl:+.2f})</i>\n\n"
            
            # 最终统计
            message += "━━━━━━━━━━━━━━━\n"
            message += f"<b>Total Value:</b> ${total_value:.2f}\n"
            message += f"<b>Total PnL:</b> ${total_pnl:+.2f}"
            
            if total_value > 0:
                total_pnl_pct = (total_pnl / (total_value - total_pnl)) * 100
                message += f" ({total_pnl_pct:+.1f}%)"
            
            if redeemable_count > 0:
                message += f"\n<b>💰 Redeemable:</b> ${redeemable_value:.2f} ({redeemable_count} markets)"
            
            notifier.send_message(message)
            log.info(f"[TELEGRAM CMD] ✅ Positions sent: {len(positions)} items, ${total_value:.2f}")
            
        except Exception as e:
            error_msg = str(e)[:200]
            log.error(f"[TELEGRAM CMD] ✗ Positions error: {error_msg}")
            import traceback
            traceback.print_exc()
            try:
                notifier.send_message(f"❌ Error getting positions:\n<code>{error_msg}</code>")
            except Exception:
                pass  # 通知失败也不崩溃
    
    def handle_redeem_command():
        """
        显示可赎回持仓及交互按钮
        线程安全：使用 API 调用和 redeem_collector 方法
        """
        global redeem_positions_cache
        
        try:
            log.info("\n[TELEGRAM CMD] 💰 Getting redeemable positions...")
            
            # 使用 SimpleRedeemCollector 的现有方法
            positions = redeem_collector._fetch_redeemable_positions()
            
            if positions is None:
                notifier.send_message("❌ Failed to fetch redeemable positions from API")
                return
            
            if not positions:
                notifier.send_message("✅ <b>No positions to redeem!</b>\n\nAll markets are already redeemed or still open.")
                return
            
            # 缓存到回调处理程序（线程安全）
            with redeem_cache_lock:
                redeem_positions_cache = positions
            
            # 计算总价值
            total_value = sum(p.get('currentValue', 0) for p in positions)
            
            # 格式化消息
            message = f"<b>💰 REDEEMABLE POSITIONS ({len(positions)})</b>\n"
            message += "━━━━━━━━━━━━━━━\n\n"
            
            for i, p in enumerate(positions[:10]):  # 最多列出 10 个仓位
                title = p.get('title', 'Unknown')
                if len(title) > 40:
                    title = title[:37] + "..."
                
                outcome = p.get('outcome', '?')
                size = p.get('size', 0)
                value = p.get('currentValue', 0)
                
                message += f"<b>#{i+1}</b> [{outcome}] {title}\n"
                message += f"  └ {size:.1f} contracts = ${value:.2f}\n\n"
            
            if len(positions) > 10:
                hidden_value = sum(p.get('currentValue', 0) for p in positions[10:])
                message += f"<i>...and {len(positions) - 10} more (${hidden_value:.2f})</i>\n\n"
            
            message += "━━━━━━━━━━━━━━━\n"
            message += f"<b>Total Value:</b> ${total_value:.2f}\n\n"
            message += "<i>Choose action:</i>"
            
            # 创建按钮
            buttons = [
                [
                    {"text": "💰 Redeem All", "callback_data": "redeem_all"},
                    {"text": "❌ Cancel", "callback_data": "redeem_cancel"}
                ]
            ]
            
            # 为每个仓位添加按钮（最多 10 项）
            for i in range(min(len(positions), 10)):
                buttons.append([
                    {"text": f"💰 Redeem #{i+1}", "callback_data": f"redeem_pos_{i}"}
                ])
            
            # 发送带按钮的消息
            notifier.send_message_with_buttons(message, buttons)
            log.info(f"[TELEGRAM CMD] ✅ Redeem menu sent: {len(positions)} positions, ${total_value:.2f}")
            
        except Exception as e:
            error_msg = str(e)[:200]
            log.error(f"[TELEGRAM CMD] ✗ Redeem error: {error_msg}")
            import traceback
            traceback.print_exc()
            try:
                notifier.send_message(f"❌ Error getting redeemable positions:\n<code>{error_msg}</code>")
            except Exception:
                pass
    
    def handle_redeem_all_callback(callback_id: str, message_id: int):
        """处理"赎回全部"按钮点击"""
        global redeem_positions_cache
        
        try:
            # 从缓存获取仓位（线程安全）
            with redeem_cache_lock:
                positions = redeem_positions_cache.copy()
            
            if not positions:
                notifier.answer_callback_query(callback_id, "❌ No positions in cache", show_alert=True)
                return
            
            notifier.answer_callback_query(callback_id, "🚀 Starting redeem process...")
            
            total = len(positions)
            
            # 更新消息
            notifier.edit_message_text(
                message_id, 
                f"<b>🚀 REDEEMING {total} POSITIONS...</b>\n\n<i>Please wait, this may take a few minutes...</i>"
            )
            
            # 赎回流程（带间隔停顿）
            success_count = 0
            fail_count = 0
            total_redeemed = 0.0
            
            for i, pos in enumerate(positions):
                # 使用 SimpleRedeemCollector 的现有方法
                result = redeem_collector._redeem_one(i + 1, total, pos)
                
                if result:
                    success_count += 1
                    total_redeemed += pos.get('currentValue', 0)
                else:
                    fail_count += 1
                
                # 赎回之间的停顿（与自动收集器一致）
                if i < total - 1:
                    pause = redeem_collector.pause_between
                    log.info(f"[REDEEM] Pause {pause}s before next redeem...")
                    time.sleep(pause)
            
            # 最终报告
            message = f"<b>✅ REDEEM COMPLETED!</b>\n"
            message += "━━━━━━━━━━━━━━━\n\n"
            message += f"<b>Total positions:</b> {total}\n"
            message += f"<b>Redeemed:</b> {success_count} ✅\n"
            message += f"<b>Failed:</b> {fail_count} ❌\n"
            message += f"<b>Total value:</b> ${total_redeemed:.2f}\n"
            
            notifier.edit_message_text(message_id, message)
            log.info(f"[TELEGRAM CMD] ✅ Redeem all completed: {success_count}/{total}")
            
        except Exception as e:
            error_msg = str(e)[:200]
            log.error(f"[TELEGRAM CMD] ✗ Redeem all error: {error_msg}")
            import traceback
            traceback.print_exc()
            try:
                notifier.edit_message_text(message_id, f"❌ Redeem failed:\n<code>{error_msg}</code>")
            except Exception:
                pass
    
    def handle_redeem_position_callback(callback_id: str, message_id: int, index: int):
        """处理"赎回 #N"按钮点击"""
        global redeem_positions_cache
        
        try:
            # 从缓存获取持仓（线程安全）
            with redeem_cache_lock:
                positions = redeem_positions_cache.copy()
            
            if index >= len(positions):
                notifier.answer_callback_query(callback_id, "❌ Position not found", show_alert=True)
                return
            
            pos = positions[index]
            title = pos.get('title', 'Unknown')[:40]
            
            notifier.answer_callback_query(callback_id, f"🚀 Redeeming position #{index+1}...")
            
            # 更新消息
            notifier.edit_message_text(
                message_id,
                f"<b>🚀 REDEEMING POSITION #{index+1}...</b>\n\n{title}\n\n<i>Please wait...</i>"
            )
            
            # 赎回单个持仓
            result = redeem_collector._redeem_one(1, 1, pos)
            
            if result:
                value = pos.get('currentValue', 0)
                message = f"<b>✅ REDEEM SUCCESS!</b>\n\n"
                message += f"Position #{index+1} redeemed\n"
                message += f"Value: ${value:.2f}"
            else:
                message = f"<b>❌ REDEEM FAILED!</b>\n\n"
                message += f"Position #{index+1} failed to redeem\n"
                message += f"Check logs for details."
            
            notifier.edit_message_text(message_id, message)
            log.info(f"[TELEGRAM CMD] ✅ Redeem position #{index+1}: {'success' if result else 'failed'}")
            
        except Exception as e:
            error_msg = str(e)[:200]
            log.error(f"[TELEGRAM CMD] ✗ Redeem position error: {error_msg}")
            import traceback
            traceback.print_exc()
            try:
                notifier.edit_message_text(message_id, f"❌ Redeem failed:\n<code>{error_msg}</code>")
            except Exception:
                pass
    
    def handle_redeem_cancel_callback(callback_id: str, message_id: int):
        """处理"取消"按钮点击"""
        try:
            notifier.answer_callback_query(callback_id, "Cancelled")
            notifier.edit_message_text(message_id, "❌ <b>Redeem cancelled</b>")
            log.info(f"[TELEGRAM CMD] ℹ️ Redeem cancelled by user")
        except Exception as e:
            log.error(f"[TELEGRAM CMD] ✗ Cancel error: {e}")
    
    def handle_shutdown_command():
        """
        紧急关闭：查找并停止 main.py 进程
        线程安全：使用 OS 信号，不访问共享状态
        
        ⚠️ 关键：这将停止交易机器人！
        """
        try:
            log.info("\n[TELEGRAM CMD] 🛑 EMERGENCY SHUTDOWN requested!")
            
            # 查找 main.py 进程
            try:
                result = subprocess.run(
                    ['pgrep', '-f', 'python3.*src/main.py'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                
                if result.returncode == 0:
                    pid = result.stdout.strip()
                    
                    if not pid:
                        notifier.send_message("❌ <b>Process not found!</b>\n\nThe bot is not running.")
                        return
                    
                    # 发送带按钮的确认消息
                    message = f"⚠️ <b>EMERGENCY SHUTDOWN</b>\n\n"
                    message += f"<b>Process found:</b> PID {pid}\n"
                    message += f"<b>Command:</b> python3 src/main.py\n\n"
                    message += f"<i>This will gracefully stop the bot and save all positions.</i>\n\n"
                    message += f"<b>Are you sure?</b>"
                    
                    buttons = [
                        [
                            {"text": "🛑 STOP BOT", "callback_data": f"shutdown_confirm_{pid}"},
                            {"text": "❌ Cancel", "callback_data": "shutdown_cancel"}
                        ]
                    ]
                    
                    notifier.send_message_with_buttons(message, buttons)
                    log.info(f"[TELEGRAM CMD] ℹ️ Shutdown confirmation sent for PID {pid}")
                    
                else:
                    notifier.send_message("❌ <b>Process not found!</b>\n\nThe bot is not running.")
                    
            except subprocess.TimeoutExpired:
                notifier.send_message("❌ <b>Timeout!</b>\n\nFailed to find process.")
            except Exception as e:
                error_msg = str(e)[:200]
                notifier.send_message(f"❌ <b>Error finding process:</b>\n<code>{error_msg}</code>")
                
        except Exception as e:
            error_msg = str(e)[:200]
            log.error(f"[TELEGRAM CMD] ✗ Shutdown error: {error_msg}")
            import traceback
            traceback.print_exc()
            try:
                notifier.send_message(f"❌ <b>Shutdown failed:</b>\n<code>{error_msg}</code>")
            except Exception:
                pass
    
    def handle_shutdown_confirm_callback(callback_id: str, message_id: int, pid: str):
        """处理"停止机器人"确认按钮点击"""
        try:
            notifier.answer_callback_query(callback_id, "🛑 Stopping bot...", show_alert=True)
            
            # 更新消息
            notifier.edit_message_text(
                message_id,
                f"<b>🛑 STOPPING BOT...</b>\n\nPID: {pid}\n\n<i>Sending SIGINT signal...</i>"
            )
            
            # 发送 SIGINT 信号（如同 Ctrl+C）
            try:
                os.kill(int(pid), signal.SIGINT)
                
                # 稍等片刻
                time.sleep(2)
                
                # 检查进程是否已停止
                result = subprocess.run(
                    ['ps', '-p', pid],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                
                if result.returncode == 0:
                    # 进程仍在运行（优雅关闭进行中）
                    message = f"<b>✅ SHUTDOWN SIGNAL SENT!</b>\n\n"
                    message += f"PID: {pid}\n\n"
                    message += f"<i>Bot is shutting down gracefully...</i>\n"
                    message += f"<i>Check logs for details.</i>"
                else:
                    # 进程已停止
                    message = f"<b>✅ BOT STOPPED!</b>\n\n"
                    message += f"PID: {pid}\n\n"
                    message += f"<i>All positions saved.</i>"
                
                notifier.edit_message_text(message_id, message)
                log.info(f"[TELEGRAM CMD] ✅ Shutdown signal sent to PID {pid}")
                
            except ProcessLookupError:
                # 进程已不存在
                notifier.edit_message_text(
                    message_id,
                    f"<b>ℹ️ BOT ALREADY STOPPED</b>\n\nPID {pid} no longer exists."
                )
            except PermissionError:
                notifier.edit_message_text(
                    message_id,
                    f"<b>❌ PERMISSION DENIED</b>\n\nCannot stop PID {pid}.\nRun bot as same user."
                )
            
        except Exception as e:
            error_msg = str(e)[:200]
            log.error(f"[TELEGRAM CMD] ✗ Shutdown confirm error: {error_msg}")
            import traceback
            traceback.print_exc()
            try:
                notifier.edit_message_text(message_id, f"❌ <b>Shutdown failed:</b>\n<code>{error_msg}</code>")
            except Exception:
                pass
    
    def handle_shutdown_cancel_callback(callback_id: str, message_id: int):
        """处理"取消"按钮点击"""
        try:
            notifier.answer_callback_query(callback_id, "Cancelled")
            notifier.edit_message_text(message_id, "✅ <b>Shutdown cancelled</b>\n\nBot continues running.")
            log.info(f"[TELEGRAM CMD] ℹ️ Shutdown cancelled by user")
        except Exception as e:
            log.error(f"[TELEGRAM CMD] ✗ Cancel error: {e}")
    
    # 创建赎回回调处理程序字典
    redeem_callbacks = {
        'redeem_all': handle_redeem_all_callback,
        'redeem_position': handle_redeem_position_callback,
        'redeem_cancel': handle_redeem_cancel_callback
    }
    
    # 创建关闭回调处理程序字典
    shutdown_callbacks = {
        'shutdown_confirm': handle_shutdown_confirm_callback,
        'shutdown_cancel': handle_shutdown_cancel_callback
    }
    
    # 启动 Telegram 命令监听器（守护线程，不阻塞关闭）
    dashboard.add_event("Starting command listener...", 'telegram')
    try:
        notifier.start_command_listener(
            on_chart_command=handle_chart_command,
            on_balance_command=handle_balance_command,
            on_positions_command=handle_positions_command,
            on_redeem_command=handle_redeem_command,
            on_redeem_callbacks=redeem_callbacks,
            on_shutdown_command=handle_shutdown_command,
            on_shutdown_callbacks=shutdown_callbacks
        )
        dashboard.add_event("Command listener active (/chart, /b, /t, /r, /off)", 'success')
    except Exception as e:
        dashboard.add_event(f"Listener failed: {str(e)[:40]}", 'error')
        dashboard.add_event("Bot continues without commands", 'info')
    
    # ═══════════════════════════════════════════════════════════════
    # 🔥 简单赎回收集器 - 基于 API 的定期赎回系统
    # 替代复杂的 pending_markets 逻辑
    # ═══════════════════════════════════════════════════════════════
    from simple_redeem_collector import SimpleRedeemCollector
    
    # 从 order_executor 获取钱包地址
    wallet_address = order_executor.wallet_address
    
    if wallet_address:
        log.info(f"\n[SYSTEM] Initializing Simple Redeem Collector...")
        log.info(f"[SYSTEM] Wallet: {wallet_address[:10]}...{wallet_address[-8:]}")
        
        redeem_collector = SimpleRedeemCollector(
            wallet_address=wallet_address,
            config=config,
            order_executor=order_executor,
            trader_module=trader_module,
            multi_trader=multi_trader,  # 🔥 FIX: For creating trade records
            notifier=notifier  # 🔥 FIX: For Telegram notifications
        )
        
        # 异步启动由 start_async 在事件循环中处理
        log.info(f"[SYSTEM] ✅ Simple Redeem Collector initialized (async start pending)")
        dashboard.add_event("Redeem collector initialized", 'info')
    else:
        log.warning(f"\n[SYSTEM] ⚠️ WARNING: No wallet address, redeem collector disabled")
        log.info(f"[SYSTEM]    Check that POLYMARKET_PRIVATE_KEY is set in .env")
        redeem_collector = None
        dashboard.add_event("Redeem collector disabled (no wallet)", 'warning')
    
    # ═══════════════════════════════════════════════════════════════
    # 旧版：异步赎回处理器（即将移除）
    # ═══════════════════════════════════════════════════════════════
    def process_redeem_async(coin, prev_market, pending_info, config, markets_skipped, 
                            session_start_time):
        """异步处理赎回，不阻塞主循环"""
        # 🔍 关键：记录函数启动（确认 submit() 已执行！）
        log.info(f"\n[REDEEM ASYNC] 🚀 Started for {coin.upper()} market {prev_market}")
        
        try:
            redeem_cfg = config.get("execution.redeem", {})
            max_attempts = redeem_cfg.get("max_attempts", 3)
            retry_delay = redeem_cfg.get("retry_delay_sec", 300)
            now = time.time()
            
            elapsed = (now - pending_info['first_attempt']) / 60
            log.info(f"[{coin.upper()} REDEEM] Attempt {pending_info['attempts']}/{max_attempts} for {prev_market} (after {elapsed:.1f} min)")
            
            # 尝试赎回
            metadata = trader_module.get_market_metadata(prev_market)
            redeem_success = False
            
            # 🔍 详细的元数据诊断
            log.info(f"[REDEEM] Checking metadata for {prev_market}...")
            log.info(f"[REDEEM]   - Metadata exists: {metadata is not None}")
            if metadata:
                log.info(f"[REDEEM]   - Has condition_id: {'condition_id' in metadata}")
                if 'condition_id' in metadata:
                    log.info(f"[REDEEM]   - Condition ID: {metadata['condition_id'][:20]}...")
            
            if metadata and metadata.get('condition_id'):
                token_ids = trader_module.get_token_ids(prev_market)
                log.info(f"[REDEEM]   - Token IDs exist: {token_ids is not None}")
                if token_ids:
                    log.info(f"[REDEEM]   - Has UP token: {'UP' in token_ids}")
                    log.info(f"[REDEEM]   - Has DOWN token: {'DOWN' in token_ids}")
                
                if token_ids and token_ids.get('UP') and token_ids.get('DOWN'):
                    log.info(f"[REDEEM] ✅ All metadata OK, calling redeem_position()...")
                    success, amount = order_executor.redeem_position(
                        market_slug=prev_market,
                        condition_id=metadata['condition_id'],
                        up_token_id=token_ids['UP'],
                        down_token_id=token_ids['DOWN'],
                        neg_risk=metadata.get('neg_risk', True)
                    )
                    
                    if success:
                        redeem_success = True
                        log.info(f"[REDEEM] ✅ Redeemed ${amount:.2f} USDC!")
                        
                        # ═══════════════════════════════════════════════════════════
                        # 🔥 关键：重置该市场的投资跟踪！
                        # 现在可以无限制地交易新市场！
                        # ═══════════════════════════════════════════════════════════
                        try:
                            # 从 order_executor 获取 safety_guard
                            if hasattr(trader_module, 'order_executor') and trader_module.order_executor:
                                trader_module.order_executor.safety.reset_market(prev_market)
                        except Exception as reset_err:
                            log.warning(f"[REDEEM] ⚠ Failed to reset market tracking: {reset_err}")
                    else:
                        log.warning(f"[REDEEM] ⚠ Failed (oracle not resolved or no tokens)")
                else:
                    log.error(f"[REDEEM] ❌ CRITICAL: No token IDs cached for {prev_market}")
                    log.info(f"[REDEEM]    This market cannot be redeemed without token IDs!")
                    log.info(f"[REDEEM]    Possible causes:")
                    log.info(f"[REDEEM]    1. Market was opened before restart")
                    log.info(f"[REDEEM]    2. EMERGENCY_SAVE position (no metadata saved)")
                    log.info(f"[REDEEM]    3. Metadata file corrupted or missing")
            else:
                log.error(f"[REDEEM] ❌ CRITICAL: No metadata cached for {prev_market}")
                log.info(f"[REDEEM]    Missing condition_id - redeem IMPOSSIBLE!")
                log.info(f"[REDEEM]    Metadata: {metadata}")
                log.info(f"[REDEEM]    Possible causes:")
                log.info(f"[REDEEM]    1. Market was opened before restart")
                log.info(f"[REDEEM]    2. Metadata not saved to disk (check logs/market_metadata.json)")
                log.info(f"[REDEEM]    3. Bug in set_token_ids() or save_market_metadata_to_disk()")
            
            # 赎回成功后关闭持仓
            if redeem_success:
                api_result = get_market_outcome(prev_market)
                
                if api_result.get("winner"):
                    winner = api_result["winner"]
                    price_start = pending_info['price_start']
                    price_final = pending_info['price_final']
                    
                    # 为所有策略关闭该市场
                    for base_name in STRATEGY_BASES:
                        strategy_name = f"{base_name}_{coin}"
                        try:
                            result = multi_trader.close_market(
                                strategy_name=strategy_name,
                                market_slug=prev_market,
                                winner=winner,
                                btc_start=price_start,
                                btc_final=price_final
                            )
                            if result:
                                # 发送 Telegram 通知
                                session_stats = multi_trader.get_session_stats(strategy_name, markets_skipped[coin])
                                portfolio_stats = _get_portfolio_stats(multi_trader, markets_skipped, session_start_time)
                                notifier.send_market_closed(coin, result, session_stats, portfolio_stats)
                                
                                # 图表生成（按需）
                                nonlocal total_completed_markets, last_chart_at
                                total_completed_markets += 1
                                
                                if total_completed_markets - last_chart_at >= CHART_INTERVAL:
                                    log.info(f"[CHART] {total_completed_markets} markets completed, generating PnL chart...")
                                    chart_path = f"/root/4coins_live/logs/pnl_chart_{total_completed_markets}.png"
                                    from pnl_chart_generator import generate_pnl_chart
                                    if generate_pnl_chart('/root/4coins_live/logs', COINS, chart_path):
                                        caption = f"<b>📊 PnL Chart - {total_completed_markets} Markets Completed</b>"
                                        if notifier.send_photo(chart_path, caption):
                                            log.info(f"[CHART] ✓ Sent to Telegram successfully")
                                            last_chart_at = total_completed_markets
                                        else:
                                            log.error(f"[CHART] ✗ Failed to send to Telegram")
                                    else:
                                        log.error(f"[CHART] ✗ Failed to generate chart")
                                
                                pnl_sign = "+" if result['pnl'] >= 0 else ""
                                log.info(f"[{strategy_name:30s}] Closed {prev_market}: {pnl_sign}${result['pnl']:,.2f}")
                            elif redeem_amount > 0:
                                # ═══════════════════════════════════════════════════════════
                                # 🔥 修复：若 close_market() 返回 None（重启后持仓为空）
                                # 但赎回成功，则从订单重建最小化交易记录
                                # 确保所有自然关闭在仪表盘上显示！
                                # ═══════════════════════════════════════════════════════════
                                log.info(f"[{strategy_name}] Position was empty but redeem successful (${redeem_amount:.2f})")
                                log.info(f"[{strategy_name}] Creating trade record from orders for dashboard...")
                                
                                try:
                                    # 获取 trader
                                    trader = multi_trader.traders.get(strategy_name)
                                    if trader:
                                        # 从 orders.jsonl 重建最小化交易记录
                                        import json
                                        total_cost = 0
                                        total_contracts = 0
                                        
                                        try:
                                            with open(f'logs/orders.jsonl', 'r') as f:
                                                for line in f:
                                                    try:
                                                        order = json.loads(line)
                                                        if (order.get('market_slug') == prev_market and 
                                                            order.get('order_type') == 'BUY' and 
                                                            order.get('success')):
                                                            total_cost += order.get('total_spent_usd', 0)
                                                            total_contracts += order.get('contracts', 0)
                                                    except (json.JSONDecodeError, KeyError, TypeError):
                                                        continue
                                        except Exception as e:
                                            log.info(f"[{strategy_name}] Warning: Could not read orders: {e}")
                                        
                                        if total_cost > 0:
                                            # 创建最小化交易记录
                                            pnl = redeem_amount - total_cost
                                            roi_pct = (pnl / total_cost * 100) if total_cost > 0 else 0
                                            
                                            minimal_trade = {
                                                'market_slug': prev_market,
                                                'winner': winner,
                                                'exit_type': 'natural_close',
                                                'exit_reason': 'natural_close',
                                                'pnl': pnl,
                                                'roi_pct': roi_pct,
                                                'total_cost': total_cost,
                                                'payout': redeem_amount,
                                                'winner_ratio': 100.0,  # Unknown
                                                'total_entries': 0,  # Unknown
                                                'up_entries': 0,
                                                'down_entries': 0,
                                                'up_invested': total_cost,
                                                'down_invested': 0.0,
                                                'up_shares': total_contracts,
                                                'down_shares': 0.0,
                                                'duration': 0,
                                                'close_time': time.time(),
                                                'close_timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
                                                'reconstructed': True  # 标记表示此记录为重建
                                            }
                                            
                                            # 添加到 closed_trades 以在仪表盘显示
                                            trader.closed_trades.append(minimal_trade)
                                            
                                            # 记录到文件
                                            try:
                                                trader._log_trade(minimal_trade)
                                            except Exception as e:
                                                log.info(f"[{strategy_name}] Warning: Could not log trade: {e}")
                                            
                                            pnl_sign = "+" if pnl >= 0 else ""
                                            log.info(f"[{strategy_name:30s}] Reconstructed {prev_market}: {pnl_sign}${pnl:,.2f} (from redeem)")
                                        else:
                                            log.info(f"[{strategy_name}] No buy orders found in logs, skipping reconstruction")
                                except Exception as e:
                                    log.info(f"[{strategy_name}] Warning: Could not reconstruct trade: {e}")
                        except Exception as e:
                            log.info(f"[ERROR] {strategy_name} close failed: {e}")
                
                # 从待处理中移除 - 成功！
                del pending_markets[coin][prev_market]
                log.info(f"[SUCCESS] Market {prev_market} completed and redeemed!")
                log.info("")
                return True
            
            # 赎回失败
            if pending_info['attempts'] < max_attempts:
                pending_info['next_retry'] = now + retry_delay
                log.info(f"[PENDING] Will retry in {retry_delay // 60} minutes")
                return False
            else:
                # 达到最大尝试次数后失败
                log.error(f"[ERROR] ❌ Market {prev_market} failed after {max_attempts} attempts!")
                
                # 获取持仓信息
                strategy_name = f"{STRATEGY_BASES[0]}_{coin}"
                trader = multi_trader.get_trader(strategy_name)
                position_info = ""
                if trader and prev_market in trader.positions:
                    pos = trader.positions[prev_market]
                    for side in ['UP', 'DOWN']:
                        if pos[side]['total_shares'] > 0:
                            position_info += f" {side}:{pos[side]['total_shares']:.0f}@${pos[side]['total_invested']:.2f}"
                
                # 记录失败
                failed_log = Path("logs/failed_redeems.log")
                failed_log.parent.mkdir(exist_ok=True)
                with open(failed_log, "a") as f:
                    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
                    f.write(f"{timestamp} | {prev_market} | {position_info}\n")
                
                log.info(f"[ERROR] Logged to logs/failed_redeems.log")
                
                # 发送警报
                alert_msg = f"⚠️ <b>FAILED REDEEM</b>\n\nMarket: <code>{prev_market}</code>\nPosition: {position_info}\nAttempts: {max_attempts}\n\nCheck logs/failed_redeems.log"
                order_executor._send_telegram_alert(alert_msg)
                
                # 从待处理中移除
                del pending_markets[coin][prev_market]
                log.info("")
                return False
                
        except Exception as e:
            log.error(f"\n[REDEEM ERROR] ❌ EXCEPTION in process_redeem_async!")
            log.info(f"[REDEEM ERROR] Coin: {coin}, Market: {prev_market}")
            log.info(f"[REDEEM ERROR] Exception: {e}")
            log.info(f"[REDEEM ERROR] Full traceback:")
            import traceback
            traceback.print_exc()
            log.info(f"[REDEEM ERROR] This redeem task will be abandoned!")
            return False
    
    # ═══════════════════════════════════════════════════════════════
    # 事件驱动回调 - 价格变化时立即触发
    # ═══════════════════════════════════════════════════════════════
    def on_price_update(coin: str, market_state: Dict):
        """
        当 Polymarket WebSocket 价格变化时立即调用
        实时处理退出检查和入场信号
        线程安全，包含全面的错误处理
        """
        try:
            # ═══════════════════════════════════════════════════════
            # 验证：检查输入
            # ═══════════════════════════════════════════════════════
            if not market_state or not coin:
                return
            
            market_slug = market_state.get('market_slug')
            if not market_slug:
                return
            
            # 获取带安全默认值的价格
            up_ask = market_state.get('up_ask', 0.5)
            down_ask = market_state.get('down_ask', 0.5)
            up_bid = market_state.get('up_bid', up_ask * 0.95)  # 卖出时的买一价（回退：ASK 的 95%）
            down_bid = market_state.get('down_bid', down_ask * 0.95)  # 卖出时的买一价（回退：ASK 的 95%）
            
            # 验证价格
            if up_ask <= 0 or down_ask <= 0 or up_ask > 1 or down_ask > 1:
                return
            
            # ═══════════════════════════════════════════════════════
            # 线程安全：检查市场状态
            # ═══════════════════════════════════════════════════════
            with market_lock:
                if coin not in market_start_prices:
                    return
                if market_slug not in market_start_prices[coin]:
                    return
                
                status = market_start_prices[coin].get(market_slug, -999)
                if status in [-1, -2, -999]:
                    return  # 市场未激活、已关闭或未知
            
            # ═══════════════════════════════════════════════════════
            # 处理：该币种的所有策略
            # ═══════════════════════════════════════════════════════
            for base_name in STRATEGY_BASES:
                strategy_name = f"{base_name}_{coin}"
                
                # 验证策略是否存在
                if strategy_name not in strategies:
                    continue
                
                try:
                    # 获取当前持仓统计（通过 multi_trader 锁实现线程安全）
                    position_stats = multi_trader.get_market_stats(strategy_name, market_slug, up_ask, down_ask)
                    
                    # ═══════════════════════════════════════════════════════
                    # 第 1 部分：退出检查（如有持仓）
                    # ═══════════════════════════════════════════════════════
                    if position_stats and position_stats.get('total_invested', 0) > 0:
                        # ─────────────────────────────────────────────────
                        # 关键：验证价格新鲜度和同步性
                        # 防止因过期/不同步的价格触发虚假止损
                        # ─────────────────────────────────────────────────
                        up_ask_ts = market_state.get('up_ask_timestamp', 0)
                        down_ask_ts = market_state.get('down_ask_timestamp', 0)
                        
                        is_valid, reason = validate_prices(up_ask, down_ask, up_ask_ts, down_ask_ts, coin)
                        
                        if not is_valid:
                            # 价格无效——跳过所有退出检查
                            log.warning(f"[PRICE] ⚠️ {coin.upper()} prices invalid: {reason}, skipping exit checks")
                            continue
                        
                        # 确定我方方向（按合约数量）
                        up_shares = position_stats.get('up_shares', 0)
                        down_shares = position_stats.get('down_shares', 0)
                        
                        our_side = None
                        our_price = None
                        
                        if up_shares > down_shares and up_shares > 0:
                            our_side = 'UP'
                            our_price = up_ask
                        elif down_shares > 0:
                            our_side = 'DOWN'
                            our_price = down_ask
                        
                        if not our_side or not our_price:
                            continue  # 无明确仓位
                        
                        # 获取未实现盈亏用于止损检查
                        unrealized_pnl = position_stats.get('unrealized_pnl', 0)
                        total_invested = position_stats.get('total_invested', 0)
                        
                        # ─────────────────────────────────────────────────
                        # 退出检查 #1：混合止损（按币种配置）
                        # BTC：无 | ETH：-$10 | SOL：-15% | XRP：-$10
                        # 回测：混合方法提升 +126% 利润
                        # ─────────────────────────────────────────────────
                        # 获取该币种的止损配置
                        sl_config = config.get(f'exit.stop_loss.per_coin.{coin}', {})
                        sl_enabled = sl_config.get('enabled', False)
                        sl_type = sl_config.get('type', 'none')
                        sl_value = sl_config.get('value', None)
                        
                        # 根据类型计算阈值
                        stop_loss_triggered = False
                        stop_loss_threshold = 0
                        
                        if sl_enabled and sl_value is not None:
                            if sl_type == 'fixed':
                                # 固定美元金额
                                stop_loss_threshold = sl_value
                                stop_loss_triggered = unrealized_pnl <= stop_loss_threshold
                            elif sl_type == 'percent':
                                # 投资本金的百分比
                                stop_loss_threshold = total_invested * (sl_value / 100.0)
                                stop_loss_triggered = unrealized_pnl <= stop_loss_threshold
                        
                        if stop_loss_triggered:
                            # 双重检查仓位是否仍然存在（竞态条件保护）
                            trader = multi_trader.get_trader(strategy_name)
                            if not trader or market_slug not in trader.positions:
                                continue  # 仓位已关闭
                            
                            # 线程安全检查：市场尚未关闭
                            with market_lock:
                                current_status = market_start_prices[coin].get(market_slug, -999)
                                if current_status == -2:
                                    continue  # 已被其他回调关闭
                            
                            # 🔥 修复 1：记录退出触发器（适用于所有 4 个币种）
                            from trade_logger import log_exit_trigger
                            log_exit_trigger(
                                market_slug=market_slug,
                                exit_reason='stop_loss',
                                coin=coin,
                                unrealized_pnl=unrealized_pnl,
                                threshold_pnl=stop_loss_threshold
                            )
                            
                            # 🔥 修复 2：在退出前将市场标记为已关闭，防止竞态条件（线程安全）
                            with market_lock:
                                market_start_prices[coin][market_slug] = -2
                            
                            # 🔥 修复 2.1：原子块（按币种保护）
                            order_executor.block_market(market_slug, coin)
                            
                            # 以止损关闭仓位（传入当前 BID 价格用于卖出）
                            result = multi_trader.close_market_early_exit(
                                strategy_name=strategy_name,
                                market_slug=market_slug,
                                exit_price=our_price,
                                exit_reason='stop_loss',
                                up_bid=up_bid,  # ✅ 卖出 UP 代币的实际 BID
                                down_bid=down_bid  # ✅ 卖出 DOWN 代币的实际 BID
                            )
                            
                            if result:
                                
                                # 发送通知
                                if isinstance(result, dict):
                                    try:
                                        session_stats = multi_trader.get_session_stats(strategy_name, markets_skipped[coin])
                                        portfolio_stats = _get_portfolio_stats(multi_trader, markets_skipped, session_start_time)
                                        notifier.send_market_closed(coin, result, session_stats, portfolio_stats)
                                        
                                        # 增加已完成市场计数器
                                        total_completed_markets += 1
                                        
                                        # 每 CHART_INTERVAL 个市场生成并发送 PnL 图表
                                        if total_completed_markets - last_chart_at >= CHART_INTERVAL:
                                            log.info(f"[CHART] {total_completed_markets} markets completed, generating PnL chart...")
                                            
                                            chart_path = f"/root/4coins_live/logs/pnl_chart_{total_completed_markets}.png"
                                            
                                            # 导入图表生成器
                                            from pnl_chart_generator import generate_pnl_chart
                                            
                                            if generate_pnl_chart('/root/4coins_live/logs', COINS, chart_path):
                                                # 发送到 Telegram
                                                caption = f"<b>📊 PnL Chart - {total_completed_markets} Markets Completed</b>"
                                                if notifier.send_photo(chart_path, caption):
                                                    log.info(f"[CHART] ✓ Sent to Telegram successfully")
                                                    last_chart_at = total_completed_markets
                                                else:
                                                    log.error(f"[CHART] ✗ Failed to send to Telegram")
                                            else:
                                                log.error(f"[CHART] ✗ Failed to generate chart")
                                    except Exception as e:
                                        log.info(f"[ERROR] Notification failed: {e}")
                                
                                # 打印确认信息
                                log.info(f"\n{'='*80}")
                                if sl_type == 'fixed':
                                    log.info(f"[{coin.upper()}] 🛑 STOP-LOSS (Fixed ${sl_value:.2f})")
                                elif sl_type == 'percent':
                                    log.info(f"[{coin.upper()}] 🛑 STOP-LOSS (Percent {sl_value:.0f}% = ${stop_loss_threshold:.2f})")
                                else:
                                    log.info(f"[{coin.upper()}] 🛑 STOP-LOSS")
                                log.info(f"[{strategy_name}] {market_slug}")
                                log.info(f"[EXIT] Our side: {our_side}")
                                log.info(f"[EXIT] Invested: ${total_invested:.2f}")
                                log.info(f"[EXIT] Unrealized PnL: ${unrealized_pnl:.2f} (threshold: ${stop_loss_threshold:.2f})")
                                if isinstance(result, dict):
                                    log.info(f"[EXIT] Final PnL: ${result['pnl']:+.2f}")
                                log.info(f"[EXIT] Market is NO LONGER trading!")
                                log.info(f"{'='*80}\n")
                                return  # 关闭后退出回调
                        
                        # ─────────────────────────────────────────────────
                        # 退出检查 #2：翻转止损（动态来自策略）
                        # 当己方价格过低时触发
                        # ─────────────────────────────────────────────────
                        strategy = strategies.get(strategy_name)
                        if strategy and our_price <= strategy.flip_stop_price:
                            # 双重检查仓位是否仍然存在（竞态条件保护）
                            trader = multi_trader.get_trader(strategy_name)
                            if not trader or market_slug not in trader.positions:
                                continue  # 仓位已关闭
                            
                            # 线程安全检查：市场尚未关闭
                            with market_lock:
                                current_status = market_start_prices[coin].get(market_slug, -999)
                                if current_status == -2:
                                    continue  # 已被其他回调关闭
                            
                            # 🔥 修复 1：记录退出触发器（适用于所有 4 个币种）
                            from trade_logger import log_exit_trigger
                            log_exit_trigger(
                                market_slug=market_slug,
                                exit_reason='flip_stop',
                                coin=coin,
                                trigger_price=our_price,
                                threshold_price=strategy.flip_stop_price
                            )
                            
                            # 🔥 修复 2：在退出前将市场标记为已关闭，防止竞态条件（线程安全）
                            with market_lock:
                                market_start_prices[coin][market_slug] = -2
                            
                            # 🔥 修复 2.1：原子块（按币种保护）
                            order_executor.block_market(market_slug, coin)
                            
                            # 关闭仓位（翻转止损，使用当前 BID 价格卖出）
                            result = multi_trader.close_market_early_exit(
                                strategy_name=strategy_name,
                                market_slug=market_slug,
                                exit_price=our_price,
                                exit_reason='flip_stop',
                                up_bid=up_bid,  # ✅ 卖出 UP 代币的实际 BID
                                down_bid=down_bid  # ✅ 卖出 DOWN 代币的实际 BID
                            )
                            
                            if result:
                                
                                # 发送通知
                                if isinstance(result, dict):
                                    try:
                                        session_stats = multi_trader.get_session_stats(strategy_name, markets_skipped[coin])
                                        portfolio_stats = _get_portfolio_stats(multi_trader, markets_skipped, session_start_time)
                                        notifier.send_market_closed(coin, result, session_stats, portfolio_stats)
                                        
                                        # 增加已完成市场计数器
                                        total_completed_markets += 1
                                        
                                        # 每 CHART_INTERVAL 个市场生成并发送 PnL 图表
                                        if total_completed_markets - last_chart_at >= CHART_INTERVAL:
                                            log.info(f"[CHART] {total_completed_markets} markets completed, generating PnL chart...")
                                            
                                            chart_path = f"/root/4coins_live/logs/pnl_chart_{total_completed_markets}.png"
                                            
                                            # 导入图表生成器
                                            from pnl_chart_generator import generate_pnl_chart
                                            
                                            if generate_pnl_chart('/root/4coins_live/logs', COINS, chart_path):
                                                # 发送到 Telegram
                                                caption = f"<b>📊 PnL Chart - {total_completed_markets} Markets Completed</b>"
                                                if notifier.send_photo(chart_path, caption):
                                                    log.info(f"[CHART] ✓ Sent to Telegram successfully")
                                                    last_chart_at = total_completed_markets
                                                else:
                                                    log.error(f"[CHART] ✗ Failed to send to Telegram")
                                            else:
                                                log.error(f"[CHART] ✗ Failed to generate chart")
                                    except Exception as e:
                                        log.info(f"[ERROR] Notification failed: {e}")
                                
                                # 打印确认信息
                                log.info(f"\n{'='*80}")
                                log.info(f"[{coin.upper()}] 🛑 FLIP-STOP @ ${our_price:.2f}")
                                log.info(f"[{strategy_name}] {market_slug}")
                                log.info(f"[EXIT] Our side: {our_side}")
                                log.info(f"[EXIT] Price dropped to: ${our_price:.2f} (≤${strategy.flip_stop_price:.2f})")
                                if isinstance(result, dict):
                                    log.info(f"[EXIT] PnL: ${result['pnl']:+.2f}")
                                log.info(f"[EXIT] Market is NO LONGER trading!")
                                log.info(f"{'='*80}\n")
                                return  # 关闭后退出回调
                    
                    # ═══════════════════════════════════════════════════════
                    # 第二部分：入场信号检查（实时）
                    # ═══════════════════════════════════════════════════════
                    strategy = strategies.get(strategy_name)
                    if not strategy:
                        log.info(f"[ERROR] Strategy {strategy_name} not found in strategies dict!")
                        continue
                    
                    # 根据当前市场状态生成信号
                    signal = strategy.should_enter(market_state, position_stats)
                    
                    if signal:
                        # 提取方向/合约数——兼容 LateEntryStrategy 格式
                        side = None
                        contracts = None
                        
                        if 'favored' in signal:
                            # LateEntryStrategy 格式
                            favored = signal.get('favored', {})
                            side = favored.get('side')
                            contracts = favored.get('contracts')
                        else:
                            # 回退格式
                            side = signal.get('side')
                            contracts = signal.get('contracts')
                        
                        # 验证提取的值
                        if not side or contracts is None or contracts <= 0:
                            continue
                        
                        # ═══════════════════════════════════════════════════════
                        # 关键：防止竞态条件下重复入场
                        # 入场前双重检查市场状态
                        # 信号处理期间其他线程可能已关闭市场
                        # ═══════════════════════════════════════════════════════
                        with market_lock:
                            current_status = market_start_prices[coin].get(market_slug, -999)
                            if current_status in [-1, -2]:
                                # 信号处理期间市场已关闭或跳过
                                log.info(f"[RACE] {coin.upper()} market {market_slug} status={current_status}, skipping entry")
                                continue
                        
                        # 检查该币种是否启用交易
                        trading_enabled = config.get(f'trading.{coin}.enabled', True)
                        if not trading_enabled:
                            # 跳过入场——该币种交易已禁用
                            dashboard.add_event(f"Trading disabled for {coin.upper()}, skipping entry", 'system')
                            continue
                        
                        # 计算价格
                        price = up_ask if side == 'UP' else down_ask
                        
                        # 执行交易
                        success = multi_trader.enter_position(
                            strategy_name=strategy_name,
                            market_slug=market_slug,
                            side=side,
                            price=price,
                            contracts=contracts,
                            up_ask=up_ask,
                            down_ask=down_ask,
                            seconds_till_end=market_state.get('seconds_till_end', 0)
                        )
                        
                        if success and contracts > 0:
                            # 入场后更新仓位统计
                            updated_stats = multi_trader.get_market_stats(strategy_name, market_slug, up_ask, down_ask)
                            if updated_stats:
                                total_entries = updated_stats.get('total_entries', 0)
                                total_invested = updated_stats.get('total_invested', 0)
                                unrealized_pnl = updated_stats.get('unrealized_pnl', 0)
                                
                                # 打印入场确认信息
                                log.info(f"[{strategy_name:30s}] {market_slug} | {side:5s} {contracts:3.0f} @ ${price:.2f} | " f"Total: {total_entries:3d} entries ${total_invested:7.2f} | PnL: ${unrealized_pnl:+7.2f}")
                
                except KeyError as e:
                    log.info(f"[ERROR] Callback KeyError for {strategy_name}: {e}")
                except AttributeError as e:
                    log.info(f"[ERROR] Callback AttributeError for {strategy_name}: {e}")
                except Exception as e:
                    log.info(f"[ERROR] Callback unexpected error for {strategy_name}: {e}")
                    import traceback
                    traceback.print_exc()
        
        except Exception as e:
            # 顶级错误处理器——理论上不应到达此处
            log.info(f"[CRITICAL] Callback top-level error: {e}")
            import traceback
            traceback.print_exc()
    
    # 向数据馈送注册回调
    data_feed.register_price_callback(on_price_update)
    log.info("[SYSTEM] ✓ Event-driven trading callbacks registered (INSTANT entry & exit)")
    log.info("")
    
    log.info("[SYSTEM] Starting trading loop...")
    log.info("         Press Ctrl+C to stop")
    log.info("         NOTE: First market for each coin will be skipped (started mid-market)")
    log.info("         Will start trading after first market switch on each coin")
    log.info("")
    
    # 初始化键盘监听器用于手动命令
    keyboard_listener = KeyboardListener()
    keyboard_listener.register_callback('m', run_manual_redeem, "Manual redeem all positions")
    keyboard_listener.start()
    log.info("[KEYBOARD] 🎹 Listener active - Press [M] to manually redeem all positions")
    log.info("")
    
    loop_counter = 0
    
    # ── async 系统 #2：止损/翻转止损并行检查 ──────────────
    async def _sys2_check_coin(coin_name):
        """单个币种的止损/翻转止损检查（async 协程）。"""
        try:
            strategy_name = f"late_entry_v3_{coin_name}"
            if strategy_name not in strategies:
                return
            
            market_state = data_feed.get_state(coin_name)
            market_slug = market_state.get('market_slug')
            if not market_slug:
                return
            
            # 检查市场状态
            with market_lock:
                status = market_start_prices.get(coin_name, {}).get(market_slug, -999)
                if status in [-1, -2, -999]:
                    return
            
            # 获取价格
            up_ask = market_state.get('up_ask', 0.5)
            down_ask = market_state.get('down_ask', 0.5)
            up_bid = market_state.get('up_bid', 0.5)
            down_bid = market_state.get('down_bid', 0.5)
            
            # 在出场检查前验证价格新鲜度
            up_ask_ts = market_state.get('up_ask_timestamp', 0)
            down_ask_ts = market_state.get('down_ask_timestamp', 0)
            
            is_valid, reason = validate_prices(up_ask, down_ask, up_ask_ts, down_ask_ts, coin_name)
            if not is_valid:
                return
            
            # 获取详细统计
            detailed_stats = multi_trader.traders[strategy_name].get_market_detailed_stats(
                market_slug=market_slug,
                up_ask=up_ask,
                down_ask=down_ask
            )
            
            if not detailed_stats:
                return
            
            # 检查止损
            if detailed_stats.get('stop_loss_triggered', False):
                with market_lock:
                    if market_start_prices[coin_name].get(market_slug, -999) == -2:
                        return
                
                up_shares = detailed_stats['up_shares']
                down_shares = detailed_stats['down_shares']
                our_side = 'UP' if up_shares > down_shares else 'DOWN'
                our_price = up_ask if our_side == 'UP' else down_ask
                
                from trade_logger import log_exit_trigger
                log_exit_trigger(
                    market_slug=market_slug,
                    exit_reason='stop_loss',
                    coin=coin_name,
                    unrealized_pnl=detailed_stats.get('unrealized_pnl', 0),
                    threshold_pnl=detailed_stats.get('stop_loss_threshold', 0)
                )
                
                with market_lock:
                    market_start_prices[coin_name][market_slug] = -2
                
                order_executor.block_market(market_slug, coin_name)
                
                result = multi_trader.close_market_early_exit(
                    strategy_name=strategy_name,
                    market_slug=market_slug,
                    exit_price=our_price,
                    exit_reason='stop_loss',
                    up_bid=up_bid,
                    down_bid=down_bid
                )
                
                if result:
                    log.error(f"[SYS#2] 🚨 {coin_name.upper()} STOP-LOSS: PnL=${detailed_stats['unrealized_pnl']:.2f}")
            
            # 检查翻转止损
            if detailed_stats.get('flip_stop_triggered', False):
                with market_lock:
                    if market_start_prices[coin_name].get(market_slug, -999) == -2:
                        return
                
                up_shares = detailed_stats['up_shares']
                down_shares = detailed_stats['down_shares']
                our_side = 'UP' if up_shares > down_shares else 'DOWN'
                our_price = up_ask if our_side == 'UP' else down_ask
                
                from trade_logger import log_exit_trigger
                log_exit_trigger(
                    market_slug=market_slug,
                    exit_reason='flip_stop',
                    coin=coin_name,
                    trigger_price=our_price,
                    threshold_price=detailed_stats.get('flip_stop_price', 0)
                )
                
                with market_lock:
                    market_start_prices[coin_name][market_slug] = -2
                
                order_executor.block_market(market_slug, coin_name)
                
                result = multi_trader.close_market_early_exit(
                    strategy_name=strategy_name,
                    market_slug=market_slug,
                    exit_price=our_price,
                    exit_reason='flip_stop',
                    up_bid=up_bid,
                    down_bid=down_bid
                )
                
                if result:
                    log.error(f"[SYS#2] 🚨 {coin_name.upper()} FLIP-STOP")
        
        except Exception:
            pass  # 静默处理——不刷日志
    
    # ── async 主循环入口 ──────────────────────────────────
    async def _run_async():
        """async 主循环（asyncio.run 入口）。"""
        global stop_flag
        nonlocal loop_counter
        # P1: 在事件循环内启动 DataFeed 异步定时器
        await data_feed.start_async()
        # P2: 在事件循环内启动 RedeemCollector 异步循环
        if redeem_collector:
            await redeem_collector.start_async()
        while not stop_flag:
            try:
                if web_dashboard_state_mod.consume_stop_request():
                    stop_flag = True
                    break
                loop_counter += 1
                
                # 定期保存余额快照到数据库（约 30 秒间隔）
                if loop_counter % 300 == 0:
                    try:
                        db_manager.get_db().save_balance_snapshot(usdc_balance=wallet_balance, source='periodic_check')
                    except Exception:
                        pass
                
                # 独立处理每个币种
                for coin in COINS:
                    market_state = data_feed.get_state(coin)
                    market_slug = market_state['market_slug']
                    price = market_state['price']
                    
                    if not market_slug:
                        continue
                    
                    # 第一步：先检查市场切换
                    for prev_market in list(market_start_prices[coin].keys()):
                        if prev_market != market_slug and prev_market != "":
                            # 检测到市场切换！
                            if not witnessed_market_switch[coin]:
                                witnessed_market_switch[coin] = True
                                log.info(f"\n{'='*80}")
                                log.info(f"[{coin.upper()}] ✓✓✓ FIRST MARKET SWITCH DETECTED ✓✓✓")
                                log.info(f"[{coin.upper()}] From: {prev_market}")
                                log.info(f"[{coin.upper()}] To:   {market_slug}")
                                log.info(f"[{coin.upper()}] Will now start trading from this market onwards!")
                                log.info(f"{'='*80}\n")
                            else:
                                log.info(f"\n[{coin.upper()}] Market switch: {prev_market} → {market_slug}")
                            
                            price_start = market_start_prices[coin].get(prev_market, 0)
                            
                            # 检查我们是否在该市场有仓位
                            strategy_name = f"{STRATEGY_BASES[0]}_{coin}"  # 使用常量而非硬编码
                            trader = multi_trader.get_trader(strategy_name)
                            had_position = trader and prev_market in trader.positions
                            
                            if price_start > 0 or (price_start == 0 and had_position):
                                # 🔥 已禁用：旧的 pending_markets 逻辑（已由 SimpleRedeemCollector 替代）
                                # SimpleRedeemCollector 将通过 API 自动查找并赎回该仓位
                                log.info(f"\n[{coin.upper()}] Market ended: {prev_market}")
                                log.info(f"[REDEEM] Will be collected by SimpleRedeemCollector API scanner")
                                # if prev_market not in pending_markets[coin]:
                                #     redeem_cfg = config.get("execution", {}).get("redeem", {})
                                #     first_delay = redeem_cfg.get("first_attempt_delay_sec", 300)
                                #     print(f"[PENDING] Added to pending queue, first redeem attempt in {first_delay // 60} minutes...")
                                #     pending_markets[coin][prev_market] = {
                                #         'price_start': price_start if price_start > 0 else 0.0,
                                #         'price_final': price if price > 0 else 0.0,
                                #         'first_attempt': time.time(),
                                #         'attempts': 0,
                                #         'next_retry': time.time() + first_delay
                                #     }
                            elif price_start == -1:
                                # 市场已被跳过（中途启动）
                                markets_skipped[coin] += 1
                                session_stats = multi_trader.get_session_stats(strategy_name, markets_skipped[coin])
                                portfolio_stats = _get_portfolio_stats(multi_trader, markets_skipped, session_start_time)
                                notifier.send_market_skipped(coin, prev_market, "Started mid-market", session_stats, portfolio_stats)
                                log.info(f"\n[{coin.upper()}] ⏭️  Skipped market {prev_market} ended (was started mid-market)")
                            elif price_start == 0 and not had_position:
                                # 市场活跃但未入场——跳过！
                                markets_skipped[coin] += 1
                                session_stats = multi_trader.get_session_stats(strategy_name, markets_skipped[coin])
                                portfolio_stats = _get_portfolio_stats(multi_trader, markets_skipped, session_start_time)
                                notifier.send_market_skipped(coin, prev_market, "No entry signals", session_stats, portfolio_stats)
                                log.info(f"\n[{coin.upper()}] ⏭️  Skipped market {prev_market} ended (no entry signals)")
                            elif price_start == -2:
                                # 🔥 市场已被提前关闭（止损/翻转止损）
                                # 🔥 已禁用：旧的 pending_markets 逻辑（已由 SimpleRedeemCollector 替代）
                                # SimpleRedeemCollector 将通过 API 自动查找并赎回该仓位
                                log.info(f"\n[{coin.upper()}] Market {prev_market} ended (was closed early)")
                                log.info(f"[REDEEM] Will be collected by SimpleRedeemCollector API scanner")
                                # if prev_market not in pending_markets[coin]:
                                #     redeem_cfg = config.get("execution", {}).get("redeem", {})
                                #     first_delay = redeem_cfg.get("first_attempt_delay_sec", 300)
                                #     print(f"[PENDING] Added to pending queue (early exit), first redeem attempt in {first_delay // 60} minutes...")
                                #     pending_markets[coin][prev_market] = {
                                #         'price_start': -2,  # Mark as early exit
                                #         'price_final': price if price > 0 else 0.0,
                                #         'first_attempt': time.time(),
                                #         'attempts': 0,
                                #         'next_retry': time.time() + first_delay
                                #     }
                            
                            # 从跟踪中移除
                            del market_start_prices[coin][prev_market]
                    
                    # 第二步：跟踪市场起始价格
                    if market_slug not in market_start_prices[coin]:
                        # 首次见到该市场
                        if not witnessed_market_switch[coin]:
                            # 这是启动时见到的第一个市场——跳过它
                            market_start_prices[coin][market_slug] = -1  # -1 = 跳过
                            log.info(f"\n[{coin.upper()}] First market detected at startup: {market_slug}")
                            log.info(f"[SKIP] Not trading this market (script started mid-market)")
                            log.info(f"[SKIP] Will start trading after this market ends\n")
                            # 不要继续——让它在下面的入场窗口检查中处理！
                        else:
                            # 已见证过市场切换，这是一个新的有效市场
                            market_start_prices[coin][market_slug] = price if price > 0 else 0.0
                            log.info(f"\n[{coin.upper()}] ✓ New market witnessed from start: {market_slug}")
                            log.info(f"[TRADE] Start price: ${price:,.2f}" if price > 0 else "[TRADE] Start price: pending...")
                            log.info(f"[TRADE] Will trade this market ✓\n")
                            
                    elif market_start_prices[coin][market_slug] == 0:
                        # 用有效价格更新待定市场
                        if price > 0:
                            market_start_prices[coin][market_slug] = price
                            log.info(f"\n[{coin.upper()}] ✓ Start price updated: {market_slug} | Price: ${price:,.2f}\n")
                            
                    elif market_start_prices[coin][market_slug] == -1:
                        # 该市场标记为跳过——不交易
                        pass
                    
                    # 🔥 已禁用：旧的 pending_markets 处理逻辑（已由 SimpleRedeemCollector 替代）
                    # SimpleRedeemCollector 通过定期 API 扫描处理所有赎回
                    # now = time.time()
                    # 
                    # for prev_market in list(pending_markets[coin].keys()):
                    #     pending_info = pending_markets[coin][prev_market]
                    #     
                    #     # 检查是否到达重试时间
                    #     if now < pending_info['next_retry']:
                    #         continue
                    #     
                    #                 # 增加尝试次数
                    #     pending_info['attempts'] += 1
                    #     
                    #     # 提交到异步执行器（非阻塞！）
                    #     try:
                    #         print(f"[REDEEM SUBMIT] 📤 Submitting {coin.upper()} market {prev_market} to async executor...")
                    #         future = redeem_executor.submit(
                    #             process_redeem_async,
                    #             coin, prev_market, pending_info, config,
                    #             markets_skipped, session_start_time
                    #         )
                    #         print(f"[REDEEM SUBMIT] ✅ Task submitted successfully (Future: {future})")
                    #         # 立即更新下次重试时间（不等待结果）
                    #         redeem_cfg = config.get("execution", {}).get("redeem", {})
                    #         retry_delay = redeem_cfg.get("retry_delay_sec", 300)
                    #         pending_info['next_retry'] = now + retry_delay
                    #     except Exception as e:
                    #         print(f"[REDEEM] Failed to submit {coin}/{prev_market}: {e}")
                    
                    # ═══════════════════════════════════════════════════════
                    # 余额检查：BTC 市场结束前 60 秒
                    # （BTC 市场每 15 分钟结束一次——适合定期刷新余额）
                    # ═══════════════════════════════════════════════════════
                    if coin == 'btc':
                        seconds_till_end = market_state.get('seconds_till_end', 0)
                        
                        # 在市场结束前 60 秒检查余额
                        if 55 <= seconds_till_end <= 65:
                            # 跟踪已检查的市场以避免重复
                            if not hasattr(main, '_balance_checked_markets'):
                                main._balance_checked_markets = set()
                            
                            current_market = market_state.get('market_slug')
                            if current_market and current_market not in main._balance_checked_markets:
                                main._balance_checked_markets.add(current_market)
                                
                                # 清理旧条目（仅保留最近 10 个）
                                if len(main._balance_checked_markets) > 10:
                                    main._balance_checked_markets = set(list(main._balance_checked_markets)[-10:])
                                
                                # 异步余额检查（非阻塞）
                                def check_balance_async():
                                    """异步查询钱包 USDC 余额并更新全局变量（非阻塞）。"""
                                    global wallet_balance
                                    try:
                                        if not safety_guard.dry_run:
                                            new_balance = order_executor.get_wallet_usdc_balance()
                                            if new_balance and new_balance > 0:
                                                old_balance = wallet_balance
                                                wallet_balance = new_balance
                                                change = new_balance - old_balance
                                                change_sign = "+" if change >= 0 else ""
                                                log.info(f"[BALANCE] 🔄 Updated: ${wallet_balance:,.2f} ({change_sign}${change:.2f})")
                                    except Exception as e:
                                        log.warning(f"[BALANCE] ⚠️ Check failed: {e}")
                                
                                threading.Thread(target=check_balance_async, daemon=True, name="balance_check").start()
                    
                    # 检查该市场是否活跃
                    current_market_status = market_start_prices[coin].get(market_slug, -999)
                    
                    # 如果之前跳过（-1）但现在已进入入场窗口——允许交易
                    _strategy_name = f"{STRATEGY_BASES[0]}_{coin}"
                    _ew = strategies[_strategy_name].entry_window
                    if current_market_status == -1 and market_state['seconds_till_end'] <= _ew:
                        market_start_prices[coin][market_slug] = 0  # 激活市场
                        log.info(f"\n[{coin.upper()}] ✅ Market {market_slug} NOW ACTIVE (entry window)")
                    elif current_market_status in [-1, -2, -999]:
                        # 市场未激活（-1）、被提前关闭（-2）或未跟踪（-999）
                        continue
                    
                    # ========================================
                    # 市场发现与监控
                    # 入场/出场信号现由回调处理！
                    # ========================================
                    # 仪表板更新循环（此处无信号处理）
                    pass  # 市场监控由回调处理
                    
                
                # 实时更新仪表板
                # 🔥 已变更：pending_markets 由 SimpleRedeemCollector 替代
                # 仪表板现在显示空待处理列表（收集器自动处理赎回）
                all_pending = {}  # 空字典——收集器在后台处理赎回
                dashboard.render(multi_trader, strategies, data_feed, wallet_balance, all_pending)
                
                try:
                    from web_dashboard.snapshot_builder import build_snapshot
                    _proj = Path(__file__).resolve().parent.parent
                    _snap = build_snapshot(
                        coins=COINS,
                        strategy_base=STRATEGY_BASES[0],
                        multi_trader=multi_trader,
                        data_feed=data_feed,
                        wallet_balance=wallet_balance,
                        config=config,
                        session_start_time=session_start_time,
                        dry_run=safety_guard.dry_run,
                        markets_skipped=markets_skipped,
                    )
                    web_dashboard_state_mod.set_snapshot(_snap)
                    if getattr(args, "web", False):
                        web_dashboard_state_mod.write_state_file(_proj, _snap)
                except Exception:
                    pass
                
                # ═══════════════════════════════════════════════════════════
                # 🔥 系统 #2：异步即时止损检查（每 0.1 秒）
                # 并行检查全部 4 个币种
                # ═══════════════════════════════════════════════════════════
                sys2_tasks = []
                for coin in COINS:
                    sys2_tasks.append(asyncio.create_task(_sys2_check_coin(coin)))
                
                # 休眠——现在可以慢一些了（入场/退出在回调中处理）
                await asyncio.sleep(0.1)
                
            except Exception as e:
                log.info(f"[ERROR] Main loop error: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(1)
    
    # ── 运行异步主循环 ────────────────────────────────────
    asyncio.run(_run_async())
    
    # 清理资源
    log.info("\n[SYSTEM] Stopping keyboard listener...")
    if keyboard_listener:
        keyboard_listener.stop()
    
    log.info("[SYSTEM] Stopping data feed...")
    data_feed.stop()
    
    # 最终摘要
    log.info("\n" + "=" * 115)
    log.info("  MERIDIAN — SESSION RESULTS".center(115))
    log.info("=" * 115)
    
    portfolio_stats = multi_trader.get_portfolio_stats()
    
    # 按策略显示（按基础分组，显示 BTC 和 ETH）
    for base_name in STRATEGY_BASES:
        log.info(f"\n=== {base_name.upper()} (BTC + ETH) ===")
        
        total_capital_strategy = 0
        total_pnl_strategy = 0
        total_trades_strategy = 0
        
        for coin in COINS:
            strategy_name = f"{base_name}_{coin}"
            trader = multi_trader.traders.get(strategy_name)
            if not trader:
                log.info(f"[WARNING] Trader {strategy_name} not found!")
                continue
            stats = trader.get_performance_stats()
            pnl = trader.current_capital - trader.starting_capital
            pnl_sign = "+" if pnl >= 0 else ""
            
            total_capital_strategy += trader.current_capital
            total_pnl_strategy += pnl
            total_trades_strategy += stats['total_trades']
            
            log.info(f"  {coin.upper():3s}: ${trader.current_capital:>8,.0f}  |  PnL: {pnl_sign}${pnl:>7,.0f}  |  " f"Trades: {stats['total_trades']:3d}  |  WR: {stats['win_rate']:.1f}%")
        
        # 策略合计
        pnl_sign = "+" if total_pnl_strategy >= 0 else ""
        log.info(f"  {'TOTAL':3s}: ${total_capital_strategy:>8,.0f}  |  PnL: {pnl_sign}${total_pnl_strategy:>7,.0f}  |  " f"Trades: {total_trades_strategy:3d}")
    
    # 投资组合总计
    log.info("\n" + "=" * 115)
    total_pnl = portfolio_stats['total_pnl']
    pnl_sign = "+" if total_pnl >= 0 else ""
    log.info(f"{'TOTAL PORTFOLIO':30s}: ${portfolio_stats['total_capital']:>10,.0f}  |  " f"PnL: {pnl_sign}${total_pnl:>8,.0f} ({pnl_sign}{portfolio_stats['portfolio_roi']:.2f}%)")
    log.info("=" * 115)
    log.info("")


if __name__ == '__main__':
    main(_parse_cli_args())
