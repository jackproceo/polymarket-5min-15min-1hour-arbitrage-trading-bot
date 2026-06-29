"""
数据库管理器 — 用于交易记录、余额和余额变动的 SQLite 存储。

表：
  - trades (交易记录)       : 每笔已平仓交易及其盈亏
  - account_balance (账户资金): 定期钱包余额快照
  - balance_changes (余额变动) : 每笔买入/卖出/赎回操作

线程安全（通过 threading.local 实现每线程独立连接）。
WAL 日志模式支持并发读取。
"""

import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime

from utils.logging_setup import get_logger
log = get_logger("db")


# ---------------------------------------------------------------------------
# DatabaseManager 类
# ---------------------------------------------------------------------------

class DatabaseManager:
    """交易机器人的 SQLite 数据库管理器。"""

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = Path(__file__).resolve().parent.parent / "data" / "trading.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()
        # 使用纯 ASCII 以确保 Windows GBK 兼容
        log.info(f"[DB] OK SQLite database initialized: {self.db_path}")

    # ── 连接（线程本地）──────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        """获取当前线程的数据库连接（线程本地，自动创建）。"""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(str(self.db_path))
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
        return self._local.conn

    # ── 模式定义 ─────────────────────────────────────────────────────────

    def _init_db(self):
        """初始化数据库表结构和索引（启动时自动调用）。"""
        conn = self._get_conn()
        cur = conn.cursor()

        # 交易记录 (Trade Records)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                market_slug     TEXT    NOT NULL,
                coin            TEXT,
                side            TEXT,
                entry_price     REAL,
                contracts       REAL,
                size_usd        REAL,
                pnl             REAL,
                roi_pct         REAL,
                winner          TEXT,
                exit_type       TEXT,
                exit_price      REAL,
                total_entries   INTEGER,
                up_invested     REAL,
                down_invested   REAL,
                up_shares       REAL,
                down_shares     REAL,
                duration_sec    REAL,
                strategy        TEXT,
                open_time       TEXT,
                close_time      TEXT,
                status          TEXT    DEFAULT 'closed',
                note            TEXT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 账户资金 (Account Balance Snapshots)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS account_balance (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT    NOT NULL,
                usdc_balance    REAL    NOT NULL,
                pol_balance     REAL,
                pol_price_usd   REAL,
                total_value     REAL,
                source          TEXT,
                wallet_address  TEXT,
                note            TEXT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 余额变动 (Balance Change Log)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS balance_changes (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT    NOT NULL,
                amount          REAL    NOT NULL,
                balance_before  REAL,
                balance_after   REAL,
                operation_type  TEXT    NOT NULL,
                market_slug     TEXT,
                coin            TEXT,
                tx_hash         TEXT,
                note            TEXT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 索引
        cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_slug)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_coin    ON trades(coin)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_close   ON trades(close_time)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_abal_ts       ON account_balance(timestamp)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bchg_ts       ON balance_changes(timestamp)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bchg_type     ON balance_changes(operation_type)")

        # 迁移：添加新列（如不存在）
        self._migrate_schema(conn)

        conn.commit()

    def _migrate_schema(self, conn):
        """增量模式迁移：添加后续版本新增的列。"""
        existing = [row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()]
        if "polymarket_order_id" not in existing:
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN polymarket_order_id TEXT")
            except Exception:
                pass
        if "open_time" not in existing:
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN open_time TEXT")
            except Exception:
                pass

    # ── 辅助方法 ────────────────────────────────────────────────────────

    @staticmethod
    def _now() -> str:
        """返回当前时间的 ISO 格式字符串。"""
        return datetime.now().isoformat()

    # ====================================================================
    # 交易记录  (Trade Records)
    # ====================================================================

    def save_trade(self, data: dict) -> int:
        """保存一条已平仓交易记录到数据库。返回新记录的自增 ID。"""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO trades (
                market_slug, coin, side, entry_price, contracts, size_usd,
                pnl, roi_pct, winner, exit_type, exit_price,
                total_entries, up_invested, down_invested, up_shares, down_shares,
                duration_sec, strategy, open_time, close_time, status, note
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            data.get('market_slug'),
            data.get('coin'),
            data.get('side'),
            data.get('entry_price'),
            data.get('contracts'),
            data.get('size_usd'),
            data.get('pnl'),
            data.get('roi_pct'),
            data.get('winner'),
            data.get('exit_type'),
            data.get('exit_price'),
            data.get('total_entries'),
            data.get('up_invested'),
            data.get('down_invested'),
            data.get('up_shares'),
            data.get('down_shares'),
            data.get('duration_sec'),
            data.get('strategy'),
            data.get('open_time'),
            data.get('close_time'),
            data.get('status', 'closed'),
            data.get('note'),
        ))
        conn.commit()
        return cur.lastrowid

    def count_trades(self, coin: str = None) -> int:
        """返回交易记录总数（可按币种筛选）。"""
        conn = self._get_conn()
        cur = conn.cursor()
        if coin:
            cur.execute("SELECT COUNT(*) FROM trades WHERE coin=?", (coin,))
        else:
            cur.execute("SELECT COUNT(*) FROM trades")
        return cur.fetchone()[0]

    def get_trades(self, limit: int = 100, offset: int = 0,
                   coin: str = None) -> List[Dict]:
        """查询交易记录，可按币种筛选，按平仓时间降序排列。"""
        conn = self._get_conn()
        cur = conn.cursor()
        if coin:
            cur.execute(
                "SELECT * FROM trades WHERE coin=? ORDER BY close_time DESC LIMIT ? OFFSET ?",
                (coin, limit, offset))
        else:
            cur.execute(
                "SELECT * FROM trades ORDER BY close_time DESC LIMIT ? OFFSET ?",
                (limit, offset))
        return [dict(r) for r in cur.fetchall()]

    def get_trade_stats(self, coin: str = None) -> Dict:
        """获取交易统计（总次数、总盈亏、平均 ROI、胜场数），可按币种筛选。"""
        conn = self._get_conn()
        cur = conn.cursor()
        if coin:
            cur.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(pnl),0) AS total_pnl,"
                " COALESCE(AVG(roi_pct),0) AS avg_roi,"
                " SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) AS wins"
                " FROM trades WHERE coin=?", (coin,))
        else:
            cur.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(pnl),0) AS total_pnl,"
                " COALESCE(AVG(roi_pct),0) AS avg_roi,"
                " SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) AS wins"
                " FROM trades")
        return dict(cur.fetchone())

    def get_trade_stats_by_coin(self) -> List[Dict]:
        """按币种分组统计（SQL GROUP BY，不走 Python 聚合）。"""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT
                COALESCE(coin, 'unknown') AS coin,
                COUNT(*) AS count,
                COALESCE(SUM(pnl),0) AS total_pnl,
                SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) AS wins,
                COUNT(*) - SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) AS losses,
                COALESCE(AVG(roi_pct),0) AS avg_roi
            FROM trades
            GROUP BY coin
            ORDER BY total_pnl DESC
        """)
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r["total_pnl"] = round(r["total_pnl"], 2)
            r["avg_roi"] = round(r["avg_roi"], 2)
            r["win_rate"] = round(r["wins"] / max(r["count"], 1) * 100, 1)
        return rows

    # ====================================================================
    # 账户资金  (Account Balance)
    # ====================================================================

    def save_balance_snapshot(self, usdc_balance: float,
                              pol_balance: float = None,
                              pol_price_usd: float = None,
                              source: str = 'periodic_check',
                              wallet_address: str = None,
                              note: str = None) -> int:
        """保存钱包余额快照（含 USDC + POL 估值）。返回新记录 ID。"""
        conn = self._get_conn()
        cur = conn.cursor()
        total = usdc_balance
        if pol_balance is not None and pol_price_usd is not None:
            total += pol_balance * pol_price_usd
        cur.execute("""
            INSERT INTO account_balance
                (timestamp, usdc_balance, pol_balance, pol_price_usd,
                 total_value, source, wallet_address, note)
            VALUES (?,?,?,?,?,?,?,?)
        """, (self._now(), usdc_balance, pol_balance, pol_price_usd,
              total, source, wallet_address, note))
        conn.commit()
        return cur.lastrowid

    def get_latest_balance(self) -> Optional[Dict]:
        """获取最近一次余额快照记录。"""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM account_balance ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        return dict(row) if row else None

    def get_balance_history(self, limit: int = 100) -> List[Dict]:
        """查询余额快照历史，按创建时间降序排列。"""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM account_balance ORDER BY timestamp DESC LIMIT ?",
                    (limit,))
        return [dict(r) for r in cur.fetchall()]

    # ====================================================================
    # 余额变动  (Balance Changes)
    # ====================================================================

    def save_balance_change(self, amount: float,
                            balance_before: float,
                            balance_after: float,
                            operation_type: str,
                            market_slug: str = None,
                            coin: str = None,
                            tx_hash: str = None,
                            note: str = None) -> int:
        """保存余额变动记录（含操作类型、金额、交易前后余额）。"""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO balance_changes
                (timestamp, amount, balance_before, balance_after,
                 operation_type, market_slug, coin, tx_hash, note)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (self._now(), amount, balance_before, balance_after,
              operation_type, market_slug, coin, tx_hash, note))
        conn.commit()
        return cur.lastrowid

    def get_balance_changes(self, limit: int = 100,
                            operation_type: str = None) -> List[Dict]:
        """查询余额变动历史，可按操作类型筛选。"""
        conn = self._get_conn()
        cur = conn.cursor()
        if operation_type:
            cur.execute(
                "SELECT * FROM balance_changes WHERE operation_type=? ORDER BY timestamp DESC LIMIT ?",
                (operation_type, limit))
        else:
            cur.execute(
                "SELECT * FROM balance_changes ORDER BY timestamp DESC LIMIT ?",
                (limit,))
        return [dict(r) for r in cur.fetchall()]

    # ── 清理 ───────────────────────────────────────────────────────

    def close(self):
        """关闭当前线程的数据库连接。"""
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None


# ---------------------------------------------------------------------------
# 全局单例访问（模块级别，遵循现有模式）
# ---------------------------------------------------------------------------

_db_instance: Optional[DatabaseManager] = None
_lock = threading.Lock()


def init_db(db_path: str = None) -> DatabaseManager:
    """初始化全局数据库管理器（启动时调用一次）。"""
    global _db_instance
    with _lock:
        if _db_instance is None:
            _db_instance = DatabaseManager(db_path)
    return _db_instance


def get_db() -> Optional[DatabaseManager]:
    """返回全局数据库管理器（如果未初始化则为 None）。"""
    return _db_instance
