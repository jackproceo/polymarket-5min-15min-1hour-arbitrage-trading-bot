"""
Telegram 命令回调处理器 - 从 main.py 提取。

所有函数接收 BotContext 作为第一个参数，替代闭包变量捕获。
"""
import os
import subprocess
import signal
import time
import uuid
import traceback
from pathlib import Path

from utils.logging_setup import get_logger
from utils.bot_context import BotContext
from utils.balance import get_active_positions, get_pol_price_usd

log = get_logger("telegram_cmd")


def handle_chart_command(ctx: BotContext):
    """按需生成并发送 PnL 图表（用户发送 /chart 或 /pnl 时触发）。"""
    try:
        log.info("Generating PnL chart on demand...")
        chart_path = f"/root/4coins_live/logs/pnl_chart_on_demand_{uuid.uuid4().hex[:8]}.png"

        from pnl_chart_generator import generate_pnl_chart
        result = generate_pnl_chart('/root/4coins_live/logs', ctx.coins, chart_path)

        if not result:
            log.warning("No trade data found")
            ctx.notifier.send_message("⚠️ No completed markets yet. Chart will be available after first market closes.")
            return

        with ctx.market_lock:
            from main import _get_portfolio_stats
            portfolio_stats = _get_portfolio_stats(ctx.multi_trader, ctx.markets_skipped, ctx.session_start_time)

            actual_markets_count = 0
            for coin in ctx.coins:
                trades_file = Path(f"/root/4coins_live/logs/late_v3_{coin}/trades.jsonl")
                if trades_file.exists():
                    try:
                        with open(trades_file, 'r') as f:
                            actual_markets_count += sum(1 for _ in f)
                    except Exception:
                        pass

            total_pnl = portfolio_stats.get('total_pnl', 0)
            coin_stats = []
            for coin in ctx.coins:
                coin_pnl = portfolio_stats.get(f'{coin}_pnl', 0)
                emoji = "🟢" if coin_pnl >= 0 else "🔴"
                coin_stats.append(f"{coin.upper()}: {emoji} ${coin_pnl:+.0f}")

            caption = f"""<b>📊 Current PnL Chart</b>

💰 <b>Total:</b> ${total_pnl:+.2f}
📈 <b>Markets:</b> {actual_markets_count}
⏱ <b>Session:</b> {portfolio_stats.get('uptime', '?')}

<b>By Coin:</b>
{' | '.join(coin_stats)}"""

        if ctx.notifier.send_photo(chart_path, caption):
            log.info("Chart sent successfully")
        else:
            log.warning("Failed to send chart")
            ctx.notifier.send_message("❌ Chart generated but failed to send.")
        try:
            os.remove(chart_path)
        except Exception:
            pass
    except Exception as e:
        err = str(e)[:200]
        log.error("Chart error: %s", err)
        try:
            ctx.notifier.send_message(f"❌ Error generating chart:\n<code>{err}</code>")
        except Exception:
            pass


def handle_balance_command(ctx: BotContext):
    """用户发送 /balance 时显示钱包余额。"""
    try:
        log.info("Getting wallet balance...")
        usdc_balance = ctx.order_executor.get_wallet_usdc_balance()
        pol_balance = ctx.order_executor.get_pol_balance()

        if usdc_balance is None:
            ctx.notifier.send_message("❌ Failed to get USDC balance")
            return

        pol_price = get_pol_price_usd()
        pol_value = (pol_balance or 0) * pol_price
        total = usdc_balance + pol_value

        msg = f"""<b>💰 WALLET BALANCE</b>
━━━━━━━━━━━━━━━

<b>USDC:</b> ${usdc_balance:,.2f}
<b>POL:</b> {pol_balance or 0:.4f} (~${pol_value:.2f})

━━━━━━━━━━━━━━━
<b>TOTAL:</b> ${total:,.2f}

<i>Wallet: {ctx.order_executor.wallet_address[:6]}...{ctx.order_executor.wallet_address[-4:]}</i>"""

        ctx.notifier.send_message(msg)
        log.info("Balance sent: $%.2f", total)
    except Exception as e:
        err = str(e)[:200]
        log.error("Balance error: %s", err)
        try:
            ctx.notifier.send_message(f"❌ Error getting balance:\n<code>{err}</code>")
        except Exception:
            pass


def handle_positions_command(ctx: BotContext):
    """用户发送 /t 或 /positions 时显示活跃持仓。"""
    try:
        log.info("Getting active positions...")
        positions = get_active_positions(ctx.order_executor.wallet_address)

        if positions is None:
            ctx.notifier.send_message("❌ Failed to get positions from API")
            return
        if not positions:
            ctx.notifier.send_message("📊 <b>No active positions</b>\n\nAll markets closed or redeemed! 🎉")
            return

        total_value = sum(p.get('currentValue', 0) for p in positions)
        total_pnl = sum(p.get('cashPnl', 0) for p in positions)
        redeemable_value = sum(p.get('currentValue', 0) for p in positions if p.get('redeemable'))
        redeemable_count = sum(1 for p in positions if p.get('redeemable'))

        msg = f"<b>📊 ACTIVE POSITIONS ({len(positions)})</b>\n━━━━━━━━━━━━━━━\n\n"
        for i, p in enumerate(positions[:10]):
            title = (p.get('title', 'Unknown')[:42] + "...") if len(p.get('title', '')) > 45 else p.get('title', 'Unknown')
            outcome = p.get('outcome', '?')
            size = p.get('size', 0)
            cur_price = p.get('curPrice', 0)
            current = p.get('currentValue', 0)
            pnl = p.get('cashPnl', 0)
            pnl_pct = p.get('percentPnl', 0)
            emoji = "💰" if p.get('redeemable') else ("🟢" if pnl >= 0 else "🔴")
            status = " [REDEEM!]" if p.get('redeemable') else ""

            msg += f"<b>{outcome}</b>: {title}\n├ Size: {size:.1f} contracts\n├ Now: ${cur_price:.3f}\n├ Value: ${current:.2f}\n└ PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%) {emoji}{status}\n\n"

        if len(positions) > 10:
            hidden_value = sum(p.get('currentValue', 0) for p in positions[10:])
            hidden_pnl = sum(p.get('cashPnl', 0) for p in positions[10:])
            msg += f"<i>...and {len(positions) - 10} more (${hidden_value:.2f}, PnL: ${hidden_pnl:+.2f})</i>\n\n"

        msg += "━━━━━━━━━━━━━━━\n"
        msg += f"<b>Total Value:</b> ${total_value:.2f}\n"
        msg += f"<b>Total PnL:</b> ${total_pnl:+.2f}"
        if total_value > 0:
            msg += f" ({(total_pnl / (total_value - total_pnl)) * 100:+.1f}%)"
        if redeemable_count > 0:
            msg += f"\n<b>💰 Redeemable:</b> ${redeemable_value:.2f} ({redeemable_count} markets)"

        ctx.notifier.send_message(msg)
        log.info("Positions sent: %d items, $%.2f", len(positions), total_value)
    except Exception as e:
        err = str(e)[:200]
        log.error("Positions error: %s", err)
        try:
            ctx.notifier.send_message(f"❌ Error getting positions:\n<code>{err}</code>")
        except Exception:
            pass


def handle_shutdown_command(ctx: BotContext):
    """紧急关闭：查找并停止 main.py 进程。"""
    try:
        log.info("EMERGENCY SHUTDOWN requested!")
        result = subprocess.run(
            ['pgrep', '-f', 'python3.*src/main.py'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            pid = result.stdout.strip()
            if not pid:
                ctx.notifier.send_message("❌ <b>Process not found!</b>\n\nThe bot is not running.")
                return
            msg = f"⚠️ <b>EMERGENCY SHUTDOWN</b>\n\nPID {pid}\n\n<i>Are you sure?</i>"
            buttons = [
                [{"text": "🛑 STOP BOT", "callback_data": f"shutdown_confirm_{pid}"},
                 {"text": "❌ Cancel", "callback_data": "shutdown_cancel"}]
            ]
            ctx.notifier.send_message_with_buttons(msg, buttons)
        else:
            ctx.notifier.send_message("❌ <b>Process not found!</b>")
    except subprocess.TimeoutExpired:
        ctx.notifier.send_message("❌ <b>Timeout!</b>")
    except Exception as e:
        ctx.notifier.send_message(f"❌ <b>Error:</b>\n<code>{str(e)[:200]}</code>")


def handle_shutdown_confirm_callback(ctx: BotContext, callback_id: str, message_id: int, pid: str):
    """处理"停止机器人"确认按钮点击。"""
    try:
        ctx.notifier.answer_callback_query(callback_id, "🛑 Stopping bot...", show_alert=True)
        ctx.notifier.edit_message_text(message_id, f"<b>🛑 STOPPING BOT...</b>\n\nPID: {pid}")
        os.kill(int(pid), signal.SIGINT)
        time.sleep(2)
        result = subprocess.run(['ps', '-p', pid], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            ctx.notifier.edit_message_text(message_id, f"<b>✅ SHUTDOWN SIGNAL SENT!</b>\n\nPID: {pid}\n\nBot is shutting down gracefully...")
        else:
            ctx.notifier.edit_message_text(message_id, f"<b>✅ BOT STOPPED!</b>\n\nPID: {pid}")
        log.info("Shutdown signal sent to PID %s", pid)
    except ProcessLookupError:
        ctx.notifier.edit_message_text(message_id, f"<b>ℹ️ BOT ALREADY STOPPED</b>")
    except PermissionError:
        ctx.notifier.edit_message_text(message_id, f"<b>❌ PERMISSION DENIED</b>")
    except Exception as e:
        log.error("Shutdown confirm error: %s", str(e)[:200])
        try:
            ctx.notifier.edit_message_text(message_id, f"❌ Failed:\n<code>{str(e)[:200]}</code>")
        except Exception:
            pass


def handle_shutdown_cancel_callback(ctx: BotContext, callback_id: str, message_id: int):
    """处理取消按钮点击。"""
    try:
        ctx.notifier.answer_callback_query(callback_id, "Cancelled")
        ctx.notifier.edit_message_text(message_id, "✅ <b>Shutdown cancelled</b>")
    except Exception as e:
        log.error("Cancel error: %s", e)
