"""SQLite 持久化缓存储层。

把拉取到的数据存进本地数据库（data/astock.db），下次直接从库读，
避免每次临时去 akshare 抓。跨重启保留、原子写入、可查询。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Optional

from ..config import DATA_DIR

DB_PATH = DATA_DIR / "astock.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_cache_updated ON cache(updated_at);
CREATE TABLE IF NOT EXISTS signals (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    side TEXT NOT NULL,          -- sell / buy
    level TEXT,                  -- red/orange/yellow/green/hold 或 strong/watch
    score REAL,
    reason TEXT,
    price REAL,
    created_at REAL,
    PRIMARY KEY (date, code, side)
);
CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(date);
CREATE TABLE IF NOT EXISTS pool_members (
    pool TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    entry_date TEXT,
    entry_price REAL,
    entry_score REAL,
    entry_reason TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    exit_date TEXT,
    exit_price REAL,
    exit_reason TEXT,
    PRIMARY KEY (pool, code)
);
CREATE TABLE IF NOT EXISTS pool_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pool TEXT, code TEXT, name TEXT,
    entry_date TEXT, entry_price REAL, entry_score REAL, entry_reason TEXT,
    exit_date TEXT, exit_price REAL, exit_reason TEXT,
    return_pct REAL, hit INTEGER,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_pool_members_status ON pool_members(pool, status);
CREATE TABLE IF NOT EXISTS bt_cache (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    horizon TEXT NOT NULL,
    bars INTEGER,
    payload TEXT,
    created_at REAL,
    PRIMARY KEY (date, code, horizon)
);
CREATE INDEX IF NOT EXISTS idx_bt_date ON bt_cache(date);
CREATE TABLE IF NOT EXISTS equity_snapshots (
    date TEXT PRIMARY KEY,
    total_assets REAL, net_assets REAL, position_value REAL, cash REAL,
    financing REAL, position_ratio REAL, financing_ratio REAL,
    pnl_ratio REAL, created_at REAL
);
"""


def _conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def cache_get(key: str, ttl: float) -> Optional[dict]:
    """读取缓存，超 TTL 返回 None。"""
    with _conn() as conn:
        row = conn.execute("SELECT value, updated_at FROM cache WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    value, updated_at = row
    if time.time() - float(updated_at) > ttl:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


_WRITE_LOCK = threading.Lock()


def cache_set(key: str, data: dict) -> None:
    """写入缓存（覆盖）。加锁避免多线程同时写导致 database is locked。"""
    blob = json.dumps(data, ensure_ascii=False, default=str)
    with _WRITE_LOCK, _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cache(key, value, updated_at) VALUES(?,?,?)",
            (key, blob, time.time()),
        )
        conn.commit()


def cache_clear() -> None:
    """清空全部缓存。"""
    with _conn() as conn:
        conn.execute("DELETE FROM cache")
        conn.commit()


# ── 信号日志 ─────────────────────────────────────────────────────
def save_signal(date: str, code: str, name: str, side: str, level: str,
                score: float, reason: str, price: float) -> None:
    """写一条信号（同日同股同方向只留一条）。"""
    with _WRITE_LOCK, _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO signals(date, code, name, side, level, score, reason, price, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (date, code, name, side, level, score, reason, price, time.time()),
        )
        conn.commit()


def list_signals(since: str, until: str | None = None) -> list[dict]:
    """按日期区间读信号（until 为空则到最新）。"""
    q = "SELECT date, code, name, side, level, score, reason, price FROM signals WHERE date >= ?"
    args: list = [since]
    if until:
        q += " AND date <= ?"
        args.append(until)
    q += " ORDER BY date DESC, score DESC"
    with _conn() as conn:
        rows = conn.execute(q, args).fetchall()
    return [
        {"date": r[0], "code": r[1], "name": r[2], "side": r[3], "level": r[4],
         "score": r[5], "reason": r[6], "price": r[7]}
        for r in rows
    ]


def clear_signals() -> None:
    with _WRITE_LOCK, _conn() as conn:
        conn.execute("DELETE FROM signals")
        conn.commit()


# ── 账户每日快照 ─────────────────────────────────────────────────
def save_equity_snapshot(date: str, row: dict) -> None:
    with _WRITE_LOCK, _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO equity_snapshots"
            "(date, total_assets, net_assets, position_value, cash, financing,"
            " position_ratio, financing_ratio, pnl_ratio, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (date, row.get("total_assets"), row.get("net_assets"), row.get("position_value"),
             row.get("cash"), row.get("financing"), row.get("position_ratio"),
             row.get("financing_ratio"), row.get("pnl_ratio"), time.time()),
        )
        conn.commit()


def list_equity_snapshots(since: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT date, total_assets, net_assets, position_value, cash, financing,"
            " position_ratio, financing_ratio, pnl_ratio FROM equity_snapshots"
            " WHERE date >= ? ORDER BY date", (since,),
        ).fetchall()
    keys = ["date", "total_assets", "net_assets", "position_value", "cash", "financing",
            "position_ratio", "financing_ratio", "pnl_ratio"]
    return [dict(zip(keys, r)) for r in rows]


def delete_equity_snapshot(date: str) -> None:
    with _WRITE_LOCK, _conn() as conn:
        conn.execute("DELETE FROM equity_snapshots WHERE date = ?", (date,))
        conn.commit()


# ── 回测结果缓存（按 日期 × 代码 × 周期）────────────────────────
def save_bt(date: str, code: str, horizon: str, bars: int, payload: dict) -> None:
    """一条回测结果落库（同日同股同周期覆盖）。"""
    blob = json.dumps(payload, ensure_ascii=False)
    with _WRITE_LOCK, _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO bt_cache(date, code, horizon, bars, payload, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (date, code, horizon, bars, blob, time.time()),
        )
        conn.commit()


def load_bt(date: str, code: str | None = None, horizon: str | None = None) -> list[dict]:
    """读回测缓存。不带 code 就读整天。"""
    q = "SELECT date, code, horizon, bars, payload, created_at FROM bt_cache WHERE date = ?"
    args: list = [date]
    if code:
        q += " AND code = ?"; args.append(code)
    if horizon:
        q += " AND horizon = ?"; args.append(horizon)
    with _conn() as conn:
        rows = conn.execute(q, args).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r[4])
        except Exception:
            payload = None
        out.append({"date": r[0], "code": r[1], "horizon": r[2], "bars": r[3],
                    "payload": payload, "created_at": r[5]})
    return out


def bt_dates(limit: int = 30) -> list[str]:
    """有缓存数据的日期（降序）。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT date FROM bt_cache ORDER BY date DESC LIMIT ?", (limit,)
        ).fetchall()
    return [r[0] for r in rows]


def prune_bt(keep_days: int = 90) -> int:
    """删掉太老的缓存，返回删除行数。"""
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - keep_days * 86400))
    with _WRITE_LOCK, _conn() as conn:
        cur = conn.execute("DELETE FROM bt_cache WHERE date < ?", (cutoff,))
        conn.commit()
        return cur.rowcount


def clear_bt(date: str) -> None:
    with _WRITE_LOCK, _conn() as conn:
        conn.execute("DELETE FROM bt_cache WHERE date = ?", (date,))
        conn.commit()


# ── 自选池（短/中/长）+ 进出历史 ────────────────────────────────
def upsert_pool_member(pool: str, code: str, name: str, entry_date: str,
                       entry_price: float, entry_score: float, entry_reason: str) -> None:
    with _WRITE_LOCK, _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pool_members"
            "(pool, code, name, entry_date, entry_price, entry_score, entry_reason, status)"
            " VALUES(?,?,?,?,?,?,?,'active')",
            (pool, code, name, entry_date, entry_price, entry_score, entry_reason),
        )
        conn.commit()


def list_pool_members(pool: str | None = None, status: str = "active") -> list[dict]:
    q = "SELECT pool, code, name, entry_date, entry_price, entry_score, entry_reason, status,"
    q += " exit_date, exit_price, exit_reason FROM pool_members"
    conds, args = [], []
    if pool:
        conds.append("pool = ?"); args.append(pool)
    if status:
        conds.append("status = ?"); args.append(status)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY pool, entry_date DESC"
    with _conn() as conn:
        rows = conn.execute(q, args).fetchall()
    keys = ["pool", "code", "name", "entry_date", "entry_price", "entry_score",
            "entry_reason", "status", "exit_date", "exit_price", "exit_reason"]
    return [dict(zip(keys, r)) for r in rows]


def member_exists(pool: str, code: str) -> bool:
    with _conn() as conn:
        row = conn.execute("SELECT 1 FROM pool_members WHERE pool=? AND code=?",
                           (pool, code)).fetchone()
    return row is not None


def set_pool_member_removed(pool: str, code: str, exit_date: str,
                            exit_price: float, exit_reason: str) -> None:
    with _WRITE_LOCK, _conn() as conn:
        conn.execute(
            "UPDATE pool_members SET status='removed', exit_date=?, exit_price=?, exit_reason=?"
            " WHERE pool=? AND code=?",
            (exit_date, exit_price, exit_reason, pool, code),
        )
        conn.commit()


def append_pool_history(row: dict) -> None:
    with _WRITE_LOCK, _conn() as conn:
        conn.execute(
            "INSERT INTO pool_history(pool, code, name, entry_date, entry_price, entry_score,"
            " entry_reason, exit_date, exit_price, exit_reason, return_pct, hit, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["pool"], row["code"], row["name"], row["entry_date"], row["entry_price"],
             row["entry_score"], row["entry_reason"], row["exit_date"], row["exit_price"],
             row["exit_reason"], row["return_pct"], row["hit"], time.time()),
        )
        conn.commit()


def list_pool_history() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT pool, code, name, entry_date, entry_price, entry_score, entry_reason,"
            " exit_date, exit_price, exit_reason, return_pct, hit FROM pool_history"
            " ORDER BY exit_date DESC, pool").fetchall()
    keys = ["pool", "code", "name", "entry_date", "entry_price", "entry_score", "entry_reason",
            "exit_date", "exit_price", "exit_reason", "return_pct", "hit"]
    return [dict(zip(keys, r)) for r in rows]
