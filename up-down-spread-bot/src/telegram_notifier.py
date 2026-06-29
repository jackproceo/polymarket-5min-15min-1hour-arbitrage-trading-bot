"""
交易机器人的电报通知系统
每次交易后发送详细的市场更新 - 不发送垃圾信息！
"""
import os
import time
import requests
from datetime import timedelta
from threading import Thread, Lock
from queue import Queue, Empty
from typing import Dict
from dotenv import load_dotenv

from utils.logging_setup import get_logger
log = get_logger("notifier")

# Load environment variables from .env file
load_dotenv("/root/4coins_live/.env")


class TelegramNotifier:
    """
    非阻塞电报通知发送器，带速率限制
    
    功能特点：
    - 后台线程发送
    - 速率限制（每秒最多 2 条消息以防止垃圾信息）
    - 优雅的错误处理（永不崩溃主进程）
    - 基于队列并带丢弃计数器
    - 仅市场关闭/跳过通知（无启动时的垃圾信息）
    """
    
    def __init__(self, bot_token: str = None, chat_id: str = None, rate_limit: float = 2.0, event_callback=None):
        """
        初始化电报通知器
        
        参数：
            bot_token: 电报机器人 token（来自 @BotFather）
            chat_id: 电报聊天 ID（您的用户 ID）
            rate_limit: 每秒最大消息数（默认：2）
            event_callback: 用于记录事件日志的回调函数(message, event_type)
        """
        # 如果未提供则从环境变量获取
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.event_callback = event_callback
        
        # 配置
        self.rate_limit = rate_limit
        self.min_interval = 1.0 / rate_limit
        self.last_send_time = 0.0
        
        # 消息队列
        self.queue = Queue(maxsize=30)  # 小队列 - 仅市场通知
        self.running = True
        self.enabled = bool(self.bot_token and self.chat_id)
        
        # 统计
        self.dropped_count = 0
        self.sent_count = 0
        self.error_count = 0
        self.last_error_time = 0.0
        
        # 会话跟踪
        self.session_start_time = time.time()
        
        # 如果启用则启动工作线程
        if self.enabled:
            self.thread = Thread(target=self._worker, daemon=True, name="TelegramNotifier")
            self.thread.start()
            if self.event_callback:
                self.event_callback("Notifier started", 'telegram')
        else:
            if self.event_callback:
                self.event_callback("Telegram disabled (no credentials)", 'info')
    
    def _worker(self):
        """从队列发送消息的后台工作线程"""
        while self.running:
            try:
                # 带超时获取消息
                msg = self.queue.get(timeout=1.0)
                if msg is None:
                    continue
                
                # 速率限制
                now = time.time()
                elapsed = now - self.last_send_time
                if elapsed < self.min_interval:
                    time.sleep(self.min_interval - elapsed)
                
                # 发送消息
                if self._send(msg):
                    self.sent_count += 1
                else:
                    self.error_count += 1
                
                self.last_send_time = time.time()
                
            except Empty:
                continue
            except Exception:
                # 静默错误处理
                self.error_count += 1
                pass
    
    def _send(self, message: str) -> bool:
        """
        发送消息到电报（带超时）
        
        返回：
            True 如果发送成功，否则 False
        """
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            response = requests.post(url, json={
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }, timeout=5.0)
            
            return response.status_code == 200
            
        except Exception as e:
            # 每分钟仅记录一次错误以避免日志泛滥
            now = time.time()
            if now - self.last_error_time > 60:
                if self.event_callback:
                    self.event_callback(f"Send error: {str(e)[:40]}", 'error')
                self.last_error_time = now
            return False
    
    def notify(self, message: str):
        """
        将通知加入队列（非阻塞）
        
        参数：
            message: 消息文本（支持 HTML 格式）
        """
        if not self.enabled:
            return
        
        try:
            self.queue.put_nowait(message)
        except Exception:
            self.dropped_count += 1
    
    def send_market_closed(self, coin: str, trade: Dict, session_stats: Dict, portfolio_stats: Dict = None):
        """
        市场关闭且有交易时发送紧凑通知
        
        参数：
            coin: 币种名称（'btc', 'eth', 'sol', 'xrp'）
            trade: 来自交易器的交易结果字典
            session_stats: 该币种的会话统计
            portfolio_stats: 可选的所有币种投资组合统计
        """
        # 提取交易数据
        market_slug = trade.get('market_slug', 'unknown')
        pnl = trade.get('pnl', 0)
        roi_pct = trade.get('roi_pct', 0)
        winner = trade.get('winner', '?')
        
        # 确定结果表情符号
        if pnl > 0:
            result_emoji = "🟢"
            result_text = "WIN"
        else:
            result_emoji = "🔴"
            result_text = "LOSS"
        
        # 格式化盈亏
        pnl_str = f"${pnl:+.2f}"
        roi_str = f"{roi_pct:+.1f}%"
        
        # 市场 ID（简短）
        market_id = market_slug.split('-')[-1][:10] if '-' in market_slug else market_slug[-10:]
        
        # 构建紧凑消息
        message = f"""<b>{coin.upper()}</b> {result_emoji} {result_text}
━━━━━━━━━━━━━━━
Market: ...{market_id}
PnL: {pnl_str} ({roi_str})
Winner: {winner}"""
        
        # 会话摘要（紧凑）
        total_pnl = session_stats.get('total_pnl', 0)
        win_rate = session_stats.get('win_rate', 0)
        
        message += f"\nTotal: ${total_pnl:+.2f} | WR: {win_rate:.0f}%"
        
        # 投资组合统计（所有币种）
        if portfolio_stats:
            message += "\n\n━━━━━━━━━━━━━━━\n<b>🏦 PORTFOLIO</b>"
            
            coins = ['btc', 'eth', 'sol', 'xrp']
            for c in coins:
                c_pnl = portfolio_stats.get(f'{c}_pnl', 0)
                c_wr = portfolio_stats.get(f'{c}_wr', 0)
                c_markets = portfolio_stats.get(f'{c}_markets_played', 0)
                
                # 盈亏表情符号
                pnl_emoji = "🟢" if c_pnl > 0 else "🔴" if c_pnl < 0 else "⚪"
                
                message += f"\n{c.upper()}: {pnl_emoji} ${c_pnl:+.2f} ({c_wr:.0f}% WR, {c_markets}m)"
            
            # 总计
            total_portfolio_pnl = portfolio_stats.get('total_pnl', 0)
            total_emoji = "🟢" if total_portfolio_pnl > 0 else "🔴" if total_portfolio_pnl < 0 else "⚪"
            uptime = portfolio_stats.get('uptime', 0)
            uptime_str = self._format_uptime(uptime)
            
            message += f"\n<b>Total: {total_emoji} ${total_portfolio_pnl:+.2f}</b> | {uptime_str}"
        
        # 发送通知
        self.notify(message)
    
    def send_market_skipped(self, coin: str, market_slug: str, skip_reason: str, session_stats: Dict, portfolio_stats: Dict = None):
        """
        当市场被跳过（无交易）时发送最小化通知
        
        参数：
            coin: 币种名称（'btc', 'eth', 'sol', 'xrp'）
            market_slug: 市场标识（未使用）
            skip_reason: 跳过原因（未使用）
            session_stats: 会话统计（未使用）
            portfolio_stats: 投资组合统计（未使用）
        """
        # 超精简消息：仅币种 + 已跳过
        message = f"<b>{coin.upper()}</b> ⏭️ SKIPPED"
        
        # 发送通知
        self.notify(message)
    
    def send_photo(self, photo_path: str, caption: str = ""):
        """
        发送照片到电报
        
        参数：
            photo_path: 图片文件路径
            caption: 可选说明文字（支持 HTML）
        
        返回：
            True 如果发送成功，否则 False
        """
        if not self.enabled:
            return False
        
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendPhoto"
            
            with open(photo_path, 'rb') as photo:
                files = {'photo': photo}
                data = {
                    'chat_id': self.chat_id,
                    'caption': caption,
                    'parse_mode': 'HTML'
                }
                
                response = requests.post(url, data=data, files=files, timeout=30)
                
                if response.status_code == 200:
                    self.sent_count += 1
                    return True
                else:
                    self.error_count += 1
                    if self.event_callback:
                        self.event_callback(f"Photo send failed: {response.status_code}", 'error')
                    return False
                    
        except Exception as e:
            self.error_count += 1
            if self.event_callback:
                self.event_callback(f"Photo error: {str(e)[:40]}", 'error')
            return False
    
    def _format_uptime(self, seconds: float) -> str:
        """将运行时间格式化为人类可读格式"""
        delta = timedelta(seconds=int(seconds))
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        
        if delta.days > 0:
            return f"{delta.days}d {hours}h {minutes}m"
        elif hours > 0:
            return f"{hours}h {minutes}m"
        else:
            return f"{minutes}m"
    
    def get_stats(self) -> Dict:
        """获取通知器统计信息"""
        return {
            'enabled': self.enabled,
            'sent_count': self.sent_count,
            'dropped_count': self.dropped_count,
            'error_count': self.error_count,
            'queue_size': self.queue.qsize()
        }
    
    def stop(self):
        """停止通知器"""
        self.running = False
        if self.enabled and self.event_callback:
            self.event_callback(f"Stopped (sent:{self.sent_count} drop:{self.dropped_count} err:{self.error_count})", 'telegram')
    
    def start_command_listener(self, on_chart_command, on_balance_command=None, 
                               on_positions_command=None, on_redeem_command=None, on_redeem_callbacks=None,
                               on_shutdown_command=None, on_shutdown_callbacks=None):
        """
        启动后台线程监听电报命令
        线程安全：在独立的后台线程中运行，带有完整的错误处理
        
        参数：
            on_chart_command: 收到 /chart 或 /pnl 命令时调用的回调函数
            on_balance_command: 收到 /balance 命令时调用的回调函数
            on_positions_command: 收到 /t 或 /positions 命令时调用的回调函数
            on_redeem_command: 收到 /r 或 /redeem 命令时调用的回调函数
            on_redeem_callbacks: 用于赎回按钮的回调函数字典
                                 {'redeem_all': func, 'redeem_position': func, 'redeem_cancel': func}
            on_shutdown_command: 收到 /off 或 /stop 命令时调用的回调函数
            on_shutdown_callbacks: 用于关闭按钮的回调函数字典
                                   {'shutdown_confirm': func, 'shutdown_cancel': func}
        """
        if not self.enabled:
            if self.event_callback:
                self.event_callback("Command listener disabled", 'info')
            return None
        
        def listener_thread():
            """Telegram 长轮询监听线程：接收命令并处理。"""
            last_update_id = 0
            consecutive_errors = 0
            max_consecutive_errors = 10
            
            if self.event_callback:
                self.event_callback("Command listener started", 'telegram')
            
            while self.running:
                try:
                    # 长轮询获取更新（30 秒超时）
                    url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
                    params = {
                        'offset': last_update_id + 1,
                        'timeout': 30,  # 长轮询 - 最多等待 30 秒获取更新
                        'allowed_updates': ['message', 'callback_query']  # 消息和按钮点击
                    }
                    
                    response = requests.get(url, params=params, timeout=35)
                    
                    # 连接成功后重置错误计数器
                    consecutive_errors = 0
                    
                    if response.status_code != 200:
                        if self.event_callback:
                            self.event_callback(f"API status {response.status_code}", 'error')
                        time.sleep(5)
                        continue
                    
                    data = response.json()
                    
                    if not data.get('ok'):
                        if self.event_callback:
                            self.event_callback(f"API error: {data.get('description', 'unknown')[:30]}", 'error')
                        time.sleep(5)
                        continue
                    
                    updates = data.get('result', [])
                    
                    # 处理所有更新
                    for update in updates:
                        try:
                            last_update_id = update['update_id']
                            
                            # 处理回调查询（按钮点击）
                            if 'callback_query' in update and on_redeem_callbacks:
                                callback_query = update['callback_query']
                                callback_data = callback_query.get('data', '')
                                callback_id = callback_query['id']
                                message_id = callback_query['message']['message_id']
                                from_chat_id = str(callback_query['from']['id'])
                                
                                # 安全措施：仅响应来自我们 chat_id 的回调
                                if from_chat_id != self.chat_id:
                                    continue
                                
                                log.info(f"[TELEGRAM] Callback received: {callback_data}")
                                
                                try:
                                    # 赎回回调
                                    if callback_data == "redeem_all":
                                        on_redeem_callbacks['redeem_all'](callback_id, message_id)
                                    
                                    elif callback_data.startswith("redeem_pos_"):
                                        index = int(callback_data.split("_")[-1])
                                        on_redeem_callbacks['redeem_position'](callback_id, message_id, index)
                                    
                                    elif callback_data == "redeem_cancel":
                                        on_redeem_callbacks['redeem_cancel'](callback_id, message_id)
                                    
                                    # 关闭回调
                                    elif on_shutdown_callbacks:
                                        if callback_data.startswith("shutdown_confirm_"):
                                            pid = callback_data.split("_")[-1]
                                            on_shutdown_callbacks['shutdown_confirm'](callback_id, message_id, pid)
                                        
                                        elif callback_data == "shutdown_cancel":
                                            on_shutdown_callbacks['shutdown_cancel'](callback_id, message_id)
                                
                                except Exception as e:
                                    error_msg = str(e)[:200]
                                    log.info(f"[TELEGRAM] Callback error: {error_msg}")
                                    self.answer_callback_query(callback_id, f"Error: {error_msg[:50]}", show_alert=True)
                                
                                continue
                            
                            # 处理常规消息
                            if 'message' not in update:
                                continue
                            
                            message = update['message']
                            
                            if 'text' not in message:
                                continue
                            
                            text = message['text'].strip().lower()
                            from_chat_id = str(message['chat']['id'])
                            from_user = message.get('from', {}).get('username', 'unknown')
                            
                            # 安全措施：仅响应来自我们 chat_id 的消息
                            if from_chat_id != self.chat_id:
                                if self.event_callback:
                                    self.event_callback(f"Unauthorized msg from {from_user}", 'error')
                                continue
                            
                            # 处理命令
                            if text in ['/chart', '/pnl', '/график']:
                                if self.event_callback:
                                    self.event_callback(f"Received {text}", 'telegram')
                                try:
                                    # 调用回调（应为线程安全！）
                                    on_chart_command()
                                except Exception as e:
                                    error_msg = str(e)[:200]
                                    if self.event_callback:
                                        self.event_callback(f"Chart cmd error: {error_msg[:40]}", 'error')
                                    self.send_message(f"❌ Error generating chart:\n<code>{error_msg}</code>")
                            
                            elif text in ['/balance', '/b']:
                                if self.event_callback:
                                    self.event_callback(f"Received {text}", 'telegram')
                                try:
                                    if on_balance_command:
                                        on_balance_command()
                                    else:
                                        self.send_message("❌ Balance command not available")
                                except Exception as e:
                                    error_msg = str(e)[:200]
                                    if self.event_callback:
                                        self.event_callback(f"Balance cmd error: {error_msg[:40]}", 'error')
                                    self.send_message(f"❌ Error getting balance:\n<code>{error_msg}</code>")
                            
                            elif text in ['/t', '/positions']:
                                if self.event_callback:
                                    self.event_callback(f"Received {text}", 'telegram')
                                try:
                                    if on_positions_command:
                                        on_positions_command()
                                    else:
                                        self.send_message("❌ Positions command not available")
                                except Exception as e:
                                    error_msg = str(e)[:200]
                                    if self.event_callback:
                                        self.event_callback(f"Positions cmd error: {error_msg[:40]}", 'error')
                                    self.send_message(f"❌ Error getting positions:\n<code>{error_msg}</code>")
                            
                            elif text in ['/r', '/redeem']:
                                if self.event_callback:
                                    self.event_callback(f"Received {text}", 'telegram')
                                try:
                                    if on_redeem_command:
                                        on_redeem_command()
                                    else:
                                        self.send_message("❌ Redeem command not available")
                                except Exception as e:
                                    error_msg = str(e)[:200]
                                    if self.event_callback:
                                        self.event_callback(f"Redeem cmd error: {error_msg[:40]}", 'error')
                                    self.send_message(f"❌ Error getting redeemable positions:\n<code>{error_msg}</code>")
                            
                            elif text in ['/off', '/shutdown', '/stop']:
                                if self.event_callback:
                                    self.event_callback(f"Received {text}", 'telegram')
                                try:
                                    if on_shutdown_command:
                                        on_shutdown_command()
                                    else:
                                        self.send_message("❌ Shutdown command not available")
                                except Exception as e:
                                    error_msg = str(e)[:200]
                                    if self.event_callback:
                                        self.event_callback(f"Shutdown cmd error: {error_msg[:40]}", 'error')
                                    self.send_message(f"❌ Error executing shutdown:\n<code>{error_msg}</code>")
                            
                            elif text in ['/help', '/start']:
                                help_text = """<b>📊 Trading Bot Commands:</b>

/chart or /pnl - Generate current PnL chart
/b or /balance - Show wallet balance (USDC + POL)
/t or /positions - Show active positions
/r or /redeem - Redeem completed markets (interactive)
/off or /stop - Emergency shutdown (with confirmation)
/help - Show this help message

<b>💡 Tip:</b> Charts are sent automatically every 10 markets.

<b>🔒 Security:</b> Commands only work from authorized chat ID."""
                                self.send_message(help_text)
                            
                            elif text.startswith('/'):
                                # 未知命令
                                self.send_message(f"❌ Unknown command: {text}\nSend /help for available commands")
                        
                        except Exception as e:
                            # 处理单个更新时出错 - 记录日志并继续
                            if self.event_callback:
                                self.event_callback(f"Update error: {str(e)[:40]}", 'error')
                            continue
                        
                except requests.exceptions.Timeout:
                    # 超时对于长轮询是正常的 - 继续
                    continue
                
                except requests.exceptions.ConnectionError as e:
                    consecutive_errors += 1
                    if self.event_callback and consecutive_errors % 5 == 1:  # 每第5个错误记录一次
                        self.event_callback(f"Connection error ({consecutive_errors})", 'error')
                    
                    if consecutive_errors >= max_consecutive_errors:
                        if self.event_callback:
                            self.event_callback("Too many errors, stopping listener", 'error')
                        break
                    
                    time.sleep(min(10 * consecutive_errors, 60))  # 指数退避
                    
                except Exception as e:
                    consecutive_errors += 1
                    if self.event_callback and consecutive_errors % 5 == 1:  # 每第5个错误记录一次
                        self.event_callback(f"Listener error ({consecutive_errors})", 'error')
                    
                    if consecutive_errors >= max_consecutive_errors:
                        if self.event_callback:
                            self.event_callback("Too many errors, stopping listener", 'error')
                        break
                    
                    time.sleep(10)
            
            if self.event_callback:
                self.event_callback("Command listener stopped", 'telegram')
        
        # 在后台守护线程中启动监听器
        # Daemon=True 意味着主程序退出时该线程将被终止
        thread = Thread(target=listener_thread, daemon=True, name="TelegramCommandListener")
        thread.start()
        
        if self.event_callback:
            self.event_callback("Command listener thread started", 'telegram')
        return thread
    
    def send_message_with_buttons(self, text: str, buttons: list) -> int:
        """
        发送带有内联键盘按钮的消息
        
        参数：
            text: 消息文本（支持 HTML）
            buttons: 按钮列表 [[{text, callback_data}, ...], ...]
        
        返回：
            成功时返回 message_id，错误时返回 None
        """
        if not self.enabled:
            return None
        
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": {
                    "inline_keyboard": buttons
                }
            }
            
            response = requests.post(url, json=payload, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                message_id = data['result']['message_id']
                log.info(f"[TELEGRAM] ✅ Message with buttons sent (ID: {message_id})")
                return message_id
            else:
                log.warning(f"[TELEGRAM] ⚠️ Failed to send message with buttons: {response.status_code}")
                return None
                
        except Exception as e:
            log.warning(f"[TELEGRAM] ⚠️ Error sending message with buttons: {e}")
            return None
    
    def edit_message_text(self, message_id: int, text: str, buttons: list = None) -> bool:
        """
        编辑现有消息的文本
        
        参数：
            message_id: 要编辑的消息 ID
            text: 新文本（支持 HTML）
            buttons: 新按钮（可选）
        
        返回：
            成功时返回 True
        """
        if not self.enabled:
            return False
        
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/editMessageText"
            payload = {
                "chat_id": self.chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML"
            }
            
            if buttons:
                payload["reply_markup"] = {"inline_keyboard": buttons}
            
            response = requests.post(url, json=payload, timeout=10)
            
            if response.status_code == 200:
                log.info(f"[TELEGRAM] ✅ Message edited (ID: {message_id})")
                return True
            else:
                log.warning(f"[TELEGRAM] ⚠️ Failed to edit message: {response.status_code}")
                return False
                
        except Exception as e:
            log.warning(f"[TELEGRAM] ⚠️ Error editing message: {e}")
            return False
    
    def answer_callback_query(self, callback_query_id: str, text: str = "", show_alert: bool = False) -> bool:
        """
        响应回调查询（显示弹出通知）
        
        参数：
            callback_query_id: 回调查询 ID
            text: 通知文本
            show_alert: 显示为弹窗（True）或提示（False）
        
        返回：
            成功时返回 True
        """
        if not self.enabled:
            return False
        
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/answerCallbackQuery"
            payload = {
                "callback_query_id": callback_query_id,
                "text": text,
                "show_alert": show_alert
            }
            
            response = requests.post(url, json=payload, timeout=10)
            return response.status_code == 200
                
        except Exception as e:
            log.warning(f"[TELEGRAM] ⚠️ Error answering callback: {e}")
            return False
    
    def send_message(self, message: str):
        """
        发送纯文本消息到电报（用于命令响应）
        直接发送（不入队），用于即时命令响应
        
        参数：
            message: 要发送的文本消息
        """
        if not self.enabled:
            return False
        
        # 直接发送以获取即时响应（不入队）
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            data = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True
            }
            
            response = requests.post(url, json=data, timeout=10)
            
            if response.status_code == 200:
                self.sent_count += 1
                return True
            else:
                self.error_count += 1
                if self.event_callback:
                    self.event_callback(f"Send msg failed: {response.status_code}", 'error')
                return False
            
        except Exception as e:
            self.error_count += 1
            if self.event_callback:
                self.event_callback(f"Send msg error: {str(e)[:40]}", 'error')
            return False


# Global notifier instance (singleton)
_notifier = None
_notifier_lock = Lock()


def get_notifier() -> TelegramNotifier:
    """获取或创建全局电报通知器（单例）"""
    global _notifier
    if _notifier is None:
        with _notifier_lock:
            if _notifier is None:  # 双重检查
                _notifier = TelegramNotifier()
    return _notifier



