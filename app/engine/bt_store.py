"""回测结果持久化：按「日期 × 代码 × 周期」存进 SQLite，避免重复计算。

为什么单独建表（而不是用通用 cache 表）：
- 通用缓存带 TTL，过期就整条作废；这里要的是**历史留档**，可以回看昨天/上周的体检结果；
- 粒度更细：只补「没算过的那只票那个周期」，加一只新股不用重算全部；
- 可以直接按日期查、按代码查，便于以后做「战法排名变化」的对比。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

import statistics

from ..data import adapter, db
from .backtest import analyze_strategies, run_backtest
from .horizon import HORIZONS, _exit_for
from .strategies import STRATEGIES, strategy_signal

HORIZONS_ALL = ("short", "mid", "long")
CACHE_VERSION = 3          # 战法集合变化时 +1，旧缓存自动重算
KEEP_DAYS = 90


def _spearman(a: list[float], b: list[float]) -> float:
    """两个排名的相关系数（1 = 完全一致，0 = 毫无关系）。"""
    n = len(a)
    if n < 2:
        return 0.0

    def ranks(xs):
        order = sorted(range(n), key=lambda i: xs[i])
        r = [0] * n
        for pos, i in enumerate(order):
            r[i] = pos
        return r

    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    den = (sum((x - ma) ** 2 for x in ra) * sum((x - mb) ** 2 for x in rb)) ** 0.5
    return num / den if den else 0.0


def _stability(bars: list[dict], horizon: str) -> Optional[float]:
    """排名稳定性：把样本切成前后两半，各自给 15 套战法排名，算相关性。

    接近 0 说明「这票适合什么战法」的结论不可复现——前半段的第一名，后半段往往不是它。
    """
    if len(bars) < 120:
        return None
    half = len(bars) // 2
    exit_of = _exit_for(horizon)
    first, second = [], []
    for name in STRATEGIES:
        sig = strategy_signal(name, bars)
        first.append(run_backtest(bars[:half], sig[:half], exit_of(name)).total_return)
        second.append(run_backtest(bars[half:], sig[half:], exit_of(name)).total_return)
    return round(_spearman(first, second), 3)


def _payload(code: str, horizon: str) -> Optional[dict]:
    """真正跑一次回测，返回可序列化的 payload（含全部 15 套战法）。"""
    preset = HORIZONS[horizon]
    bars = adapter.get_stock_history(code, days=preset["days"], period=preset["period"])
    if not bars or len(bars) < 40:
        return {
            "ok": False, "label": preset["label"], "period_label": preset["period_label"],
            "reason": f"K线不足（{len(bars) if bars else 0} 根）", "bars": len(bars) if bars else 0,
        }

    analyses = analyze_strategies(bars, exit_for=_exit_for(horizon))
    returns = [a.total_return for a in analyses]
    positives = sum(1 for r in returns if r > 0)
    stats = {
        "positive": positives,
        "total": len(returns),
        "median": round(statistics.median(returns), 2),
        "buy_hold": round((bars[-1]["close"] / bars[0]["close"] - 1) * 100, 2),
        "stability": _stability(bars, horizon),
        "best_trades": analyses[0].trade_count,
    }

    return {
        "ok": True,
        "v": CACHE_VERSION,
        "stats": stats,
        "label": preset["label"],
        "period_label": preset["period_label"],
        "bars": len(bars),
        "first_date": bars[0]["date"],
        "last_date": bars[-1]["date"],
        "all": [{
            "name": a.name, "label": a.label,
            "total_return": a.total_return, "score": a.score,
            "trade_count": a.trade_count, "max_drawdown": a.max_drawdown,
            "buy_hit_rate": a.buy_hit_rate, "sell_hit_rate": a.sell_hit_rate,
        } for a in analyses],
    }


def compute_one(code: str, horizon: str, day: Optional[str] = None) -> dict:
    """算一只票一个周期并落库。"""
    day = day or adapter.data_day()
    payload = _payload(code, horizon)
    db.save_bt(day, code, horizon, payload.get("bars") or 0, payload)
    return payload


def ensure(positions: list[dict], day: Optional[str] = None) -> dict:
    """补齐当天缺失的 (代码, 周期)，已有的直接复用——这是「避免重复搜索」的关键。"""
    day = day or adapter.data_day()
    # 只把「完整」的行当作已算过：老版本没有 stats 的会被自动重算
    existing = set()
    for r in db.load_bt(day):
        payload = r.get("payload") or {}
        if payload.get("ok") is False or (payload.get("v") == CACHE_VERSION and "stats" in payload):
            existing.add((r["code"], r["horizon"]))
    computed, reused = 0, 0
    for p in positions:
        code = p["code"]
        for h in HORIZONS_ALL:
            if (code, h) in existing:
                reused += 1
                continue
            compute_one(code, h, day)
            computed += 1
    if computed:
        db.prune_bt(KEEP_DAYS)
    return {"date": day, "computed": computed, "reused": reused}


def load_day(day: Optional[str] = None, positions: Optional[list[dict]] = None) -> dict:
    """读某天的全部结果，组装成接口需要的结构。"""
    day = day or adapter.data_day()
    rows = {(r["code"], r["horizon"]): r for r in db.load_bt(day)}
    names = {p["code"]: p.get("name") for p in (positions or [])}

    items, missing = [], 0
    for code in dict.fromkeys([r["code"] for r in rows.values()] + [p["code"] for p in (positions or [])]):
        horizons = {}
        for h in HORIZONS_ALL:
            row = rows.get((code, h))
            if row and row.get("payload"):
                horizons[h] = row["payload"]
            else:
                missing += 1
                horizons[h] = {"ok": False, "label": HORIZONS[h]["label"],
                               "period_label": HORIZONS[h]["period_label"], "reason": "尚未计算"}
        items.append({"code": code, "name": names.get(code) or code, "horizons": horizons})

    order = [p["code"] for p in (positions or [])]
    items.sort(key=lambda x: order.index(x["code"]) if x["code"] in order else 999)
    return {"status": "ready" if not missing else "partial", "date": day,
            "count": len(items), "missing": missing, "items": items,
            "available_dates": db.bt_dates(30)}
