"""
赎回处理逻辑 - 从 main.py 提取。

管理 redeem 缓存、批量/单笔赎回、取消流程。
"""
import asyncio
import time
import traceback
from typing import Dict, List, Optional, Set

from utils.logging_setup import get_logger
from utils.bot_context import BotContext

log = get_logger("redeem")


class RedeemCache:
    """管理可赎回持仓的缓存，避免重复查询 CLOB API。"""

    def __init__(self):
        self.positions: List[dict] = []
        self.cached_time: float = 0.0
        self.cache_duration: float = 60.0

    def is_fresh(self) -> bool:
        return (time.time() - self.cached_time) < self.cache_duration

    def update(self, positions: List[dict]):
        self.positions = positions
        self.cached_time = time.time()

    def invalidate(self):
        self.cached_time = 0.0

    def get(self) -> List[dict]:
        return self.positions if self.is_fresh() else []


async def process_redeem_async(ctx: BotContext):
    """后台异步执行所有可赎回持仓的赎回。"""
    log.info("=== Redeem Process: Starting ===")
    try:
        active = get_active_positions(ctx.order_executor.wallet_address)
        if active is None:
            log.warning("Failed to get positions")
            return
        redeemables = [p for p in active if p.get('redeemable')]
        if not redeemables:
            log.info("No redeemable positions found")
            return

        log.info("Found %d redeemable positions", len(redeemables))
        results: list = []
        for pos in redeemables:
            try:
                tx = ctx.order_executor.redeem_position(pos)
                results.append((pos, tx))
            except Exception as e:
                log.error("Redeem failed for %s: %s", pos.get('title', '?')[:40], str(e)[:200])
                results.append((pos, None))
            await asyncio.sleep(1)

        succeeded = sum(1 for _, tx in results if tx)
        failed = sum(1 for _, tx in results if not tx)
        log.info("Redeem complete: %d success, %d failed", succeeded, failed)

        total_value = sum(p.get('currentValue', 0) for p, tx in results if tx)
        msg = f"<b>💰 REDEEM COMPLETE</b>\n━━━━━━━━━━━━━━━\n\n"
        msg += f"✅ <b>Success:</b> {succeeded}\n"
        msg += f"❌ <b>Failed:</b> {failed}\n"
        if succeeded > 0:
            msg += f"💰 <b>Redeemed:</b> ${total_value:.2f}"
        ctx.notifier.send_message(msg)
    except Exception as e:
        log.error("Redeem process error: %s", str(e)[:200])
        ctx.notifier.send_message(f"❌ <b>Redeem process error:</b>\n<code>{str(e)[:200]}</code>")


def handle_redeem_command(ctx: BotContext):
    """用户发送 /redeem 时触发赎回。"""
    try:
        log.info("Redeem command received")
        active = get_active_positions(ctx.order_executor.wallet_address)
        if active is None:
            ctx.notifier.send_message("❌ Failed to get positions")
            return
        redeemables = [p for p in active if p.get('redeemable')]
        if not redeemables:
            ctx.notifier.send_message("ℹ️ No redeemable positions found")
            return

        total_value = sum(p.get('currentValue', 0) for p in redeemables)
        msg = f"<b>💰 REDEEMABLE ({len(redeemables)})</b>\n\n"
        for pos in redeemables[:10]:
            title = (pos.get('title', '?')[:42] + "...") if len(pos.get('title', '')) > 45 else pos.get('title', '?')
            value = pos.get('currentValue', 0)
            msg += f"• {title}: ${value:.2f}\n"
        if len(redeemables) > 10:
            msg += f"<i>...and {len(redeemables) - 10} more</i>\n"
        msg += f"\n<b>Total:</b> ${total_value:.2f}"

        buttons = [
            [
                {"text": "💰 Redeem All", "callback_data": "redeem_all"},
                {"text": "Cancel", "callback_data": "redeem_cancel"}
            ]
        ]
        for pos in redeemables[:5]:
            short = pos.get('title', '?')[:25]
            buttons.append([{"text": f"📌 {short}", "callback_data": f"redeem_{pos.get('id', '?')}"}])

        ctx.notifier.send_message_with_buttons(msg, buttons)
        log.info("Redeem prompt sent: %d positions, $%.2f", len(redeemables), total_value)
    except Exception as e:
        log.error("Redeem prompt error: %s", str(e)[:200])
        ctx.notifier.send_message(f"❌ Error:\n<code>{str(e)[:200]}</code>")


def handle_redeem_all_callback(ctx: BotContext, callback_id: str, message_id: int):
    """处理"赎回全部"按钮。"""
    try:
        ctx.notifier.answer_callback_query(callback_id, "🔄 Processing all...", show_alert=True)
        ctx.notifier.edit_message_text(message_id, "<b>🔄 Processing all redeemable positions...</b>")
        ctx.redeem_executor.submit(process_redeem_async, ctx)
    except Exception as e:
        log.error("Redeem all error: %s", str(e)[:200])
        ctx.notifier.send_message(f"❌ Error:\n<code>{str(e)[:200]}</code>")


def handle_redeem_position_callback(ctx: BotContext, callback_id: str, message_id: int, position_id: str):
    """处理单个市场赎回按钮。"""
    try:
        ctx.notifier.answer_callback_query(callback_id, "🔄 Processing...", show_alert=True)
        ctx.notifier.edit_message_text(message_id, f"<b>🔄 Redeeming position...</b>\n\nID: {position_id}")
        active = get_active_positions(ctx.order_executor.wallet_address)
        pos = next((p for p in (active or []) if p.get('id') == position_id), None)
        if pos:
            tx = ctx.order_executor.redeem_position(pos)
            if tx:
                value = pos.get('currentValue', 0)
                ctx.notifier.edit_message_text(message_id, f"<b>✅ Redeemed!</b>\n\n${value:.2f}")
            else:
                ctx.notifier.edit_message_text(message_id, "❌ <b>Redeem failed</b>")
        else:
            ctx.notifier.edit_message_text(message_id, "❌ <b>Position not found</b>")
    except Exception as e:
        log.error("Redeem position error: %s", str(e)[:200])
        ctx.notifier.edit_message_text(message_id, f"❌ Error:\n<code>{str(e)[:200]}</code>")


def handle_redeem_cancel_callback(ctx: BotContext, callback_id: str, message_id: int):
    """处理赎回取消按钮。"""
    try:
        ctx.notifier.answer_callback_query(callback_id, "Cancelled")
        ctx.notifier.edit_message_text(message_id, "✅ <b>Redeem cancelled</b>")
    except Exception as e:
        log.error("Redeem cancel error: %s", e)
