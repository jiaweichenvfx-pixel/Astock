"""自选池（短/中/长）的自我迭代：入选、每日复盘、剔除、事后验证。

与「持仓池」分开：持仓池 = 你真实持有的股票；自选池 = 系统每天从精选股票里挑出来观察的票。

生命周期（全部留痕）：
1. 入选：收盘后从精选股票（short/mid/long）挑当日评分靠前的，进对应池；
2. 每日复盘：重算趋势，走坏（跌破关键均线 / N 日涨幅跌破阈值）就剔除；
3. 事后验证：每一笔从进池到出池的收益都记进 pool_history，
   用「命中率 + 平均收益」判断当初的选股策略到底有没有效。

规则（趋势跟随，对称于入选逻辑）：
- 短线：跌破 MA10 或 5 日涨幅 < -6% → 剔除
- 中线：跌破 MA20 或 20 日涨幅 < -10% → 剔除
- 长线：跌破 MA60 或 60 日涨幅 < -12% → 剔除
"""
from __future__ import annotations

import statistics
from datetime import date
from typing import Optional

from ..data import adapter, db

POOLS = ("short", "mid", "long")
POOL_LABEL = {"short": "短线", "mid": "中线", "long": "长线"}
POOL_SIZE = 10          # 每池目标只数
MAX_SIZE = 20           # 上限（防止某天一次进太多）
MA = {"short": 10, "mid": 20, "long": 250}
CHG_WINDOW = {"short": 5, "mid": 20, "long": 60}
CHG_EXIT = {"short": -6.0, "mid": -10.0, "long": -25.0}
HIST_DAYS = {"short": 80, "mid": 80, "long": 320}


# ── 查询 ─────────────────────────────────────────────────────────
def list_members(pool: Optional[str] = None, status: str = "active") -> list[dict]:
    return db.list_pool_members(pool, status)


def active_codes() -> list[dict]:
    """所有在池股票（供买入扫描用）。"""
    return [{"code": m["code"], "name": m["name"]} for m in db.list_pool_members(None, "active")]


def history() -> list[dict]:
    return db.list_pool_history()


# ── 入选 / 剔除 ──────────────────────────────────────────────────
def add(pool: str, code: str, name: str, entry_date: str,
        entry_price: Optional[float], entry_score: Optional[float], entry_reason: str) -> None:
    db.upsert_pool_member(pool, code, name, entry_date,
                          entry_price or 0.0, entry_score or 0.0, entry_reason)


def remove(pool: str, code: str, exit_date: str, exit_price: float, exit_reason: str) -> None:
    m = next((x for x in db.list_pool_members(pool, "active") if x["code"] == code), None)
    if m is None:
        return
    db.set_pool_member_removed(pool, code, exit_date, exit_price, exit_reason)
    entry = m.get("entry_price") or 0
    ret = round((exit_price / entry - 1) * 100, 2) if entry else None
    db.append_pool_history({
        "pool": pool, "code": code, "name": m.get("name", ""),
        "entry_date": m.get("entry_date"), "entry_price": entry,
        "entry_score": m.get("entry_score"), "entry_reason": m.get("entry_reason"),
        "exit_date": exit_date, "exit_price": exit_price, "exit_reason": exit_reason,
        "return_pct": ret, "hit": 1 if ret is not None and ret > 0 else 0,
    })


def _review(pool: str, code: str) -> Optional[tuple[float, str]]:
    """趋势是否走坏。返回 (剔除价, 原因)，None 表示仍健康。"""
    bars = adapter.get_stock_history(code, days=HIST_DAYS[pool])
    if not bars or len(bars) < 25:
        return None
    closes = [b["close"] for b in bars]
    cur = closes[-1]
    ma = MA[pool]
    if len(closes) >= ma:
        ma_val = sum(closes[-ma:]) / ma
        if cur < ma_val:
            return (round(cur, 2), f"跌破MA{ma}")
    win = CHG_WINDOW[pool]
    if len(closes) > win and closes[-win - 1] > 0:
        chg = (cur / closes[-win - 1] - 1) * 100
        if chg < CHG_EXIT[pool]:
            return (round(cur, 2), f"{win}日涨幅 {chg:.1f}% 走弱")
    return None


# ── 每日迭代 ─────────────────────────────────────────────────────
def daily_update(screener: dict, today: Optional[str] = None,
                 strategies: Optional[dict] = None,
                 pool_sizes: Optional[dict] = None) -> dict:
    """收盘后跑一次：入选 + 复盘剔除。

    screener = 精选选股结果（含 short/mid/long），作为默认入选来源；
    strategies = {pool: 策略名}，给出时改用该选股策略现算候选（多策略循环比较的落点）。
    """
    today = today or adapter.data_day()
    added: list[tuple[str, str]] = []
    removed: list[tuple[str, str]] = []
    strategies = strategies or {}
    sizes = {p: (pool_sizes or {}).get(p, POOL_SIZE) for p in POOLS}

    # 1) 入选：每池补到目标只数（不重复进入同一池）
    for pool, key in (("short", "short"), ("mid", "mid"), ("long", "long")):
        active = {m["code"] for m in list_members(pool, "active")}
        if pool in strategies:
            from . import factor
            candidates = factor.pick_today(pool, strategies[pool], top_k=sizes[pool])
        else:
            candidates = screener.get(key) or []
        for item in candidates:
            if len(active) >= sizes[pool]:
                break
            code = str(item.get("code", ""))
            if not code or code in active or db.member_exists(pool, code):
                continue
            add(pool, code, item.get("name", ""), today,
                item.get("price"), item.get("score"),
                "；".join((item.get("reasons") or [])[:3]))
            active.add(code)
            added.append((pool, code))

    # 2) 复盘：剔除走坏的（当天刚进池的不复盘，避免同一天进又出）
    for pool in POOLS:
        for m in list_members(pool, "active"):
            if (m.get("entry_date") or "") >= today:
                continue
            res = _review(pool, m["code"])
            if res:
                remove(pool, m["code"], today, res[0], res[1])
                removed.append((pool, m["code"]))

    return {"date": today, "added": added, "removed": removed,
            "pools": {p: len(list_members(p, "active")) for p in POOLS}}


# ── 事后验证 ─────────────────────────────────────────────────────
def mark_to_market() -> dict[str, float]:
    """当前在池股票的浮盈（进池价 → 最新价）。"""
    out: dict[str, float] = {}
    for m in list_members(None, "active"):
        q = adapter.get_quotes([m["code"]]).get(m["code"]) or {}
        cur = q.get("price")
        if cur and m.get("entry_price"):
            out[m["code"]] = round((cur / m["entry_price"] - 1) * 100, 2)
    return out


def _aggregate(rows: list[dict]) -> dict:
    if not rows:
        return {"count": 0, "hits": 0, "win_rate": None, "avg_return": None, "median": None}
    rets = [r["return_pct"] for r in rows if r.get("return_pct") is not None]
    hits = sum(1 for r in rows if r.get("hit"))
    return {
        "count": len(rows),
        "hits": hits,
        "win_rate": round(hits / len(rows) * 100, 1),
        "avg_return": round(statistics.mean(rets), 2) if rets else None,
        "median": round(statistics.median(rets), 2) if rets else None,
    }


def evaluate() -> dict:
    """选股策略有效性的完整统计：已出池的实盘收益 + 在池的浮盈。"""
    hist = history()
    mtm = mark_to_market()

    by_pool = {p: _aggregate([h for h in hist if h["pool"] == p]) for p in POOLS}
    overall = _aggregate(hist)

    active_ret = list(mtm.values())
    verdict = "观察中"
    if overall["count"] >= 5:
        if overall["win_rate"] is not None and overall["win_rate"] >= 55 and (overall["avg_return"] or 0) > 0:
            verdict = "有效：精选股票策略能选出正收益标的"
        elif overall["win_rate"] is not None and overall["win_rate"] < 45:
            verdict = "无效：命中率偏低，选股策略需要调整"
        else:
            verdict = "待观察：样本或收益尚不足以判断"

    return {
        "verdict": verdict,
        "overall": overall,
        "by_pool": by_pool,
        "active_count": sum(1 for _ in list_members(None, "active")),
        "active_avg_return": round(statistics.mean(active_ret), 2) if active_ret else None,
        "history_count": len(hist),
    }
