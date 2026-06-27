#!/usr/bin/env python3
"""
Polymarket BTC 自动交易 — SQLite 数据库模块

记录每笔交易的完整生命周期（开仓 → 平仓）以及账户资金快照。
数据库文件位于 data/trading.db。
"""

import sqlite3
import os
import threading
from datetime import datetime

# 数据库文件路径（相对于项目根目录 data/ 文件夹）
DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "trading.db")

# 线程本地连接 + 全局锁防止并发写入冲突
_local = threading.local()
_write_lock = threading.Lock()

# ── 初始账户参数 ──────────────────────────────────────────────────────────────
INITIAL_BALANCE_USDC = 100.0  # 初始仓位 100 U


# ═════════════════════════════════════════════════════════════════════════════
# 数据库连接管理
# ═════════════════════════════════════════════════════════════════════════════

def _get_conn():
    """获取当前线程的数据库连接（自动创建）。"""
    if not hasattr(_local, "conn") or _local.conn is None:
        os.makedirs(DB_DIR, exist_ok=True)
        _local.conn = sqlite3.connect(DB_PATH)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
    return _local.conn


# ═════════════════════════════════════════════════════════════════════════════
# 建表
# ═════════════════════════════════════════════════════════════════════════════

def init_db():
    """
    初始化数据库表结构（幂等，可重复调用）。
    
    创建两张表：
    
    1. trades — 交易记录表
       ┌────────────────────┬──────────┬──────────────────────────────────────┐
       │ 字段               │ 类型     │ 说明                                 │
       ├────────────────────┼──────────┼──────────────────────────────────────┤
       │ id                 │ INTEGER  │ 自增主键                             │
       │ polymarket_slug    │ TEXT     │ 市场 Slug                            │
       │ condition_id       │ TEXT     │ 市场 Condition ID（用于赎回）         │
       │ order_id           │ TEXT     │ CLOB 订单 ID                         │
       │ side               │ TEXT     │ UP / DOWN                            │
       │ action             │ TEXT     │ BUY / SELL                           │
       │ open_reason        │ TEXT     │ 开仓原因（如 R1/R2/R3/R4 条件文本）   │
       │ entry_price        │ REAL     │ 开仓概率价格（0~1）                  │
       │ exit_price         │ REAL     │ 平仓概率价格（0~1），未平仓为 NULL   │
       │ amount_usdc        │ REAL     │ 交易 USDC 金额                       │
       │ shares             │ REAL     │ 代币数量                              │
       │ pnl_usd            │ REAL     │ 已实现盈亏（SELL 时写入，USDC）      │
       │ cumulative_pnl_usd │ REAL     │ 累计已实现盈亏                        │
       │ diff_at_trade      │ REAL     │ 成交时 BTC−PTB 差价（美元）          │
       │ btc_price          │ REAL     │ 成交时 Chainlink BTC 价格            │
       │ ptb_price          │ REAL     │ 成交时 PTB（Price To Beat）          │
       │ remaining_sec      │ INTEGER  │ 成交时市场剩余秒数                    │
       │ fee_usdc           │ REAL     │ 手续费（预留，默认 0）               │
       │ status             │ TEXT     │ submitted / filled / cancelled / fail │
       │ result             │ TEXT     │ win / loss / pending                  │
       │ btc_market_minutes │ INTEGER  │ 5 或 15                               │
       │ simulation         │ INTEGER  │ 0=实盘 / 1=模拟                      │
       │ created_at         │ TEXT     │ ISO 8601 时间戳                       │
       └────────────────────┴──────────┴──────────────────────────────────────┘

    2. account_snapshots — 账户资金快照表
       ┌──────────────────────┬─────────┬────────────────────────────────────┐
       │ 字段                 │ 类型    │ 说明                               │
       ├──────────────────────┼─────────┼────────────────────────────────────┤
       │ id                   │ INTEGER │ 自增主键                           │
       │ timestamp            │ TEXT    │ ISO 8601 快照时间                  │
       │ initial_balance      │ REAL    │ 初始入金（100 USDC）               │
       │ balance              │ REAL    │ 当前 USDC 余额                     │
       │ available_balance    │ REAL    │ 可用 USDC                          │
       │ locked_balance       │ REAL    │ 冻结中 USDC（挂单占用）            │
       │ position_value       │ REAL    │ 持仓市值                           │
       │ total_equity         │ REAL    │ 总权益 = balance + pos_value       │
       │ unrealized_pnl       │ REAL    │ 浮动盈亏                           │
       │ realized_pnl         │ REAL    │ 已实现盈亏                         │
       │ cumulative_pnl       │ REAL    │ 累计已实现盈亏                     │
       │ open_positions_count │ INTEGER │ 当前持仓数量                       │
       │ btc_price            │ REAL    │ 快照时 BTC 价格                    │
       │ btc_market_minutes   │ INTEGER │ 5 或 15                            │
       │ simulation           │ INTEGER │ 0=实盘 / 1=模拟                    │
       │ created_at           │ TEXT    │ ISO 8601 时间戳                    │
       └──────────────────────┴─────────┴────────────────────────────────────┘
    """
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            polymarket_slug     TEXT,
            condition_id        TEXT,
            order_id            TEXT,
            side                TEXT     NOT NULL,
            action              TEXT     NOT NULL,
            open_reason         TEXT,
            entry_price         REAL,
            exit_price          REAL,
            amount_usdc         REAL,
            shares              REAL,
            pnl_usd             REAL,
            cumulative_pnl_usd  REAL,
            diff_at_trade       REAL,
            btc_price           REAL,
            ptb_price           REAL,
            remaining_sec       INTEGER,
            fee_usdc            REAL     DEFAULT 0,
            status              TEXT     DEFAULT 'submitted',
            result              TEXT     DEFAULT 'pending',
            btc_market_minutes  INTEGER,
            simulation          INTEGER  DEFAULT 0,
            created_at          TEXT     NOT NULL
        );

        CREATE TABLE IF NOT EXISTS account_snapshots (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp           TEXT     NOT NULL,
            initial_balance     REAL     DEFAULT 100.0,
            balance             REAL,
            available_balance   REAL,
            locked_balance      REAL,
            position_value      REAL,
            total_equity        REAL,
            unrealized_pnl      REAL,
            realized_pnl        REAL,
            cumulative_pnl      REAL,
            open_positions_count INTEGER DEFAULT 0,
            btc_price           REAL,
            btc_market_minutes  INTEGER,
            simulation          INTEGER  DEFAULT 0,
            created_at          TEXT     NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_trades_slug
            ON trades(polymarket_slug);
        CREATE INDEX IF NOT EXISTS idx_trades_created
            ON trades(created_at);
        CREATE INDEX IF NOT EXISTS idx_trades_status
            ON trades(status);
        CREATE INDEX IF NOT EXISTS idx_snapshots_ts
            ON account_snapshots(timestamp);
    """)
    conn.commit()


# ═════════════════════════════════════════════════════════════════════════════
# 交易记录操作
# ═════════════════════════════════════════════════════════════════════════════

def insert_trade(
    polymarket_slug=None,
    condition_id=None,
    order_id=None,
    side=None,
    action=None,
    open_reason=None,
    entry_price=None,
    exit_price=None,
    amount_usdc=None,
    shares=None,
    pnl_usd=None,
    cumulative_pnl_usd=None,
    diff_at_trade=None,
    btc_price=None,
    ptb_price=None,
    remaining_sec=None,
    fee_usdc=0.0,
    status="submitted",
    result="pending",
    btc_market_minutes=None,
    simulation=False,
):
    """
    插入一条交易记录。

    每次开仓（BUY）或平仓（SELL）时调用。
    SELL 操作请使用 insert_trade_close() 或手动传入 exit_price/pnl。
    """
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            """
            INSERT INTO trades (
                polymarket_slug, condition_id, order_id,
                side, action, open_reason,
                entry_price, exit_price, amount_usdc, shares,
                diff_at_trade, btc_price, ptb_price,
                remaining_sec, fee_usdc,
                status, result,
                pnl_usd, cumulative_pnl_usd,
                btc_market_minutes, simulation,
                created_at
            ) VALUES (?,?,?, ?,?,?, ?,?,?,?, ?,?,?, ?,?, ?,?,?, ?,?, ?, ?)
            """,
            (
                polymarket_slug, condition_id, order_id,
                side, action, open_reason,
                entry_price, exit_price, amount_usdc, shares,
                diff_at_trade, btc_price, ptb_price,
                remaining_sec, fee_usdc,
                status, result,
                pnl_usd, cumulative_pnl_usd,
                btc_market_minutes, 1 if simulation else 0,
                now,
            ),
        )
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def update_trade_close(
    order_id,
    exit_price=None,
    pnl_usd=None,
    cumulative_pnl_usd=None,
    fee_usdc=None,
    status="filled",
    result=None,
):
    """
    更新一笔交易的平仓信息（通过 order_id 匹配）。
    
    参数
    ----------
    order_id : str
        原始订单 ID（BUY 或 SELL 的 order_id）。
    exit_price : float, optional
        平仓概率价格。
    pnl_usd : float, optional
        该笔交易盈亏。
    cumulative_pnl_usd : float, optional
        最新的累计已实现盈亏。
    fee_usdc : float, optional
        手续费。
    status : str
        状态（filled / cancelled / failed）。
    result : str, optional
        结果（win / loss / pending），未提供时根据 pnl_usd 自动推导。
    """
    if result is None and pnl_usd is not None:
        result = "win" if pnl_usd > 0 else ("loss" if pnl_usd < 0 else "pending")
    sets = ["exit_price = COALESCE(?, exit_price)"]
    params = [exit_price]
    if pnl_usd is not None:
        sets.append("pnl_usd = ?")
        params.append(pnl_usd)
    if cumulative_pnl_usd is not None:
        sets.append("cumulative_pnl_usd = ?")
        params.append(cumulative_pnl_usd)
    if fee_usdc is not None:
        sets.append("fee_usdc = ?")
        params.append(fee_usdc)
    sets.append("status = ?")
    params.append(status)
    sets.append("result = ?")
    params.append(result)
    params.append(order_id)
    sql = f"UPDATE trades SET {', '.join(sets)} WHERE order_id = ?"
    with _write_lock:
        conn = _get_conn()
        conn.execute(sql, params)
        conn.commit()


def insert_trade_close(
    polymarket_slug=None,
    condition_id=None,
    order_id=None,
    side=None,
    action="SELL",
    open_reason=None,
    entry_price=None,
    exit_price=None,
    amount_usdc=None,
    shares=None,
    pnl_usd=None,
    cumulative_pnl_usd=None,
    diff_at_trade=None,
    btc_price=None,
    ptb_price=None,
    remaining_sec=None,
    fee_usdc=0.0,
    status="filled",
    result=None,
    btc_market_minutes=None,
    simulation=False,
):
    """
    插入一条完整的平仓（SELL）交易记录。

    注意：此函数创建一条新记录（而非更新已有的 BUY 记录），
    目的是保留完整的交易事件日志。通过 order_id 可追溯关联的 BUY 记录。
    """
    if result is None:
        if pnl_usd is not None:
            result = "win" if pnl_usd > 0 else ("loss" if pnl_usd < 0 else "pending")
        else:
            result = "pending"
    return insert_trade(
        polymarket_slug=polymarket_slug,
        condition_id=condition_id,
        order_id=order_id,
        side=side,
        action=action,
        open_reason=open_reason,
        entry_price=entry_price,
        exit_price=exit_price,
        amount_usdc=amount_usdc,
        shares=shares,
        pnl_usd=pnl_usd,
        cumulative_pnl_usd=cumulative_pnl_usd,
        diff_at_trade=diff_at_trade,
        btc_price=btc_price,
        ptb_price=ptb_price,
        remaining_sec=remaining_sec,
        fee_usdc=fee_usdc,
        status=status,
        result=result,
        btc_market_minutes=btc_market_minutes,
        simulation=simulation,
    )


def get_recent_trades(limit=50):
    """
    获取最近的交易记录（按时间降序）。
    
    参数
    ----------
    limit : int
        返回条数上限。
    
    返回
    -------
    list[sqlite3.Row]
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return rows


# ═════════════════════════════════════════════════════════════════════════════
# 账户快照操作
# ═════════════════════════════════════════════════════════════════════════════

def insert_account_snapshot(
    balance=None,
    available_balance=None,
    locked_balance=None,
    position_value=None,
    total_equity=None,
    unrealized_pnl=None,
    realized_pnl=None,
    cumulative_pnl=None,
    initial_balance=INITIAL_BALANCE_USDC,
    open_positions_count=0,
    btc_price=None,
    btc_market_minutes=None,
    simulation=False,
):
    """
    插入一条账户资金快照记录。
    
    参数
    ----------
    balance : float, optional
        当前 USDC 余额（钱包中可自由支配的资金）。
    available_balance : float, optional
        可用余额（扣除挂单冻结）。
    locked_balance : float, optional
        冻结金额（已挂单未成交部分）。
    position_value : float, optional
        持仓市值（当前代币数量 × 市价）。
    total_equity : float, optional
        总权益 = balance + position_value。
    unrealized_pnl : float, optional
        浮动盈亏。
    realized_pnl : float, optional
        已实现盈亏。
    cumulative_pnl : float, optional
        累计已实现盈亏。
    initial_balance : float
        初始入金金额（默认 100 USDC）。
    open_positions_count : int
        当前持仓数量。
    btc_price : float, optional
        快照时 BTC 价格。
    btc_market_minutes : int, optional
        5 或 15。
    simulation : bool
        是否模拟模式。
    """
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            """
            INSERT INTO account_snapshots (
                timestamp, initial_balance,
                balance, available_balance, locked_balance,
                position_value, total_equity,
                unrealized_pnl, realized_pnl, cumulative_pnl,
                open_positions_count,
                btc_price, btc_market_minutes, simulation,
                created_at
            ) VALUES (?,?, ?,?,?, ?,?, ?,?,?, ?,?, ?,?, ?)
            """,
            (
                ts, initial_balance,
                balance, available_balance, locked_balance,
                position_value, total_equity,
                unrealized_pnl, realized_pnl, cumulative_pnl,
                open_positions_count,
                btc_price, btc_market_minutes, 1 if simulation else 0,
                now,
            ),
        )
        conn.commit()


def get_latest_account_snapshot():
    """
    获取最新的账户快照。
    
    返回
    -------
    sqlite3.Row or None
    """
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM account_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row


def get_account_history(limit=100):
    """
    获取账户快照历史（按时间降序）。
    
    返回
    -------
    list[sqlite3.Row]
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM account_snapshots ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return rows


# ═════════════════════════════════════════════════════════════════════════════
# 统计查询
# ═════════════════════════════════════════════════════════════════════════════

def get_trade_stats():
    """
    获取交易统计数据。
    
    返回
    -------
    dict
        {
            "total_trades": int,
            "wins": int,
            "losses": int,
            "pending": int,
            "win_rate": float,
            "total_pnl": float,
            "avg_pnl": float,
            "max_win": float,
            "max_loss": float,
        }
    """
    conn = _get_conn()
    stats = {"total_trades": 0, "wins": 0, "losses": 0, "pending": 0,
             "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0,
             "max_win": 0.0, "max_loss": 0.0}
    try:
        row = conn.execute("""
            SELECT
                COUNT(*)                                           AS total,
                SUM(CASE WHEN result='win'   THEN 1 ELSE 0 END)   AS wins,
                SUM(CASE WHEN result='loss'  THEN 1 ELSE 0 END)   AS losses,
                SUM(CASE WHEN result='pending' THEN 1 ELSE 0 END) AS pending,
                COALESCE(SUM(pnl_usd), 0)                          AS total_pnl,
                COALESCE(AVG(pnl_usd), 0)                          AS avg_pnl,
                COALESCE(MAX(pnl_usd), 0)                          AS max_win,
                COALESCE(MIN(pnl_usd), 0)                          AS max_loss
            FROM trades
            WHERE action = 'SELL'
        """).fetchone()
        if row and row["total"]:
            stats = {
                "total_trades": row["total"],
                "wins": row["wins"],
                "losses": row["losses"],
                "pending": row["pending"],
                "win_rate": round(row["wins"] / row["total"] * 100, 1) if row["total"] else 0.0,
                "total_pnl": round(row["total_pnl"], 4),
                "avg_pnl": round(row["avg_pnl"], 4),
                "max_win": round(row["max_win"], 4),
                "max_loss": round(row["max_loss"], 4),
            }
    except Exception:
        pass
    return stats
