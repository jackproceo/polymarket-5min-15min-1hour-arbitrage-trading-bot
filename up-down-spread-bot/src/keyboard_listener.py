"""
仪表盘无阻塞键盘监听器（跨平台）
"""
import sys
import os
import threading
import time

IS_WINDOWS = os.name == 'nt'

if IS_WINDOWS:
    import msvcrt
else:
    import select
    import termios
    import tty


class KeyboardListener:
    """无阻塞键盘监听器（Windows 和 Unix）"""
    
    def __init__(self):
        self.running = False
        self.thread = None
        self.key_callbacks = {}
        self.last_key = None
        self.last_key_time = 0
        
    def register_callback(self, key: str, callback, description: str = ""):
        """注册指定按键的回调
        
        参数：
            key: 单字符按键（例如 'm'、'M'、'q'）
            callback: 按键时调用的函数
            description: 供帮助显示的说明（可选）
        """
        key = key.lower()
        self.key_callbacks[key] = {
            'callback': callback,
            'description': description
        }
    
    def _get_key_windows(self):
        """获取单个按键（Windows 下无阻塞）"""
        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            return ch.lower()
        return None

    def _get_key_unix(self):
        """获取单个按键（Unix 下无阻塞）"""
        if select.select([sys.stdin], [], [], 0)[0]:
            return sys.stdin.read(1).lower()
        return None
    
    def _listener_loop(self):
        """主监听循环（在线程中运行）"""
        if IS_WINDOWS:
            self._listener_loop_windows()
        else:
            self._listener_loop_unix()

    def _listener_loop_windows(self):
        """Windows 平台的主监听循环（轮询 msvcrt.kbhit）。"""
        while self.running:
            key = self._get_key_windows()
            self._handle_key(key)
            time.sleep(0.05)

    def _listener_loop_unix(self):
        """Unix 平台的主监听循环（设置 cbreak 模式后轮询）。"""
        old_settings = termios.tcgetattr(sys.stdin)
        try:
            tty.setcbreak(sys.stdin.fileno())
            while self.running:
                key = self._get_key_unix()
                self._handle_key(key)
                time.sleep(0.05)
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)

    def _handle_key(self, key):
        """处理按下的键：查找注册的回调并执行（带 500ms 防抖）。"""
        if key and key in self.key_callbacks:
            now = time.time()
            if now - self.last_key_time > 0.5 or key != self.last_key:
                self.last_key = key
                self.last_key_time = now
                try:
                    self.key_callbacks[key]['callback']()
                except Exception as e:
                    print(f"\n[KEYBOARD] 执行 '{key}' 的回调出错：{e}")
    
    def start(self):
        """在后台线程中启动键盘监听器"""
        if self.running:
            return
        
        self.running = True
        self.thread = threading.Thread(target=self._listener_loop, daemon=True)
        self.thread.start()
        print("[KEYBOARD] 监听器已启动")
    
    def stop(self):
        """停止键盘监听器"""
        if not self.running:
            return
        
        self.running = False
        if self.thread:
            self.thread.join(timeout=1.0)
        print("[KEYBOARD] 监听器已停止")
    
    def get_help_text(self):
        """获取所有已注册按键的帮助文本"""
        if not self.key_callbacks:
            return "未注册键盘快捷键"
        
        lines = ["键盘快捷键："]
        for key, info in sorted(self.key_callbacks.items()):
            desc = info['description'] or '无说明'
            lines.append(f"  [{key.upper()}] {desc}")
        
        return "\n".join(lines)
