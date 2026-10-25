"""信号日志与事后验证。

为什么要记：卖出信号是一次性的，算完就没了，用户无法判断「红色必卖」到底准不准。
这里每天把信号落库，过几个交易日再回看：

- 卖出信号：之后股价跌了 → 卖对了（命中）；涨了 → 卖飞
- 买入信号：之后股价涨了 → 买对了（命中）

统计口径默认 T+5（5 个交易日后收盘价），可按档位汇总命中率。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from ..data import adapter, db

DEFAULT_HORIZON = 5       # 几个交易日后回看
DEFAULT_DAYS = 30         # 回看最近多少天的信号


def record_from_summary(summary: dict, today: Optional[str] = None) -> int:
    """把一次决策快照里的卖出/买入信号写进日志（同日重复跑不会重复写）。"""
    today = today or adapter.data_day()
    wrote = 0
    positions = {p.get("code"): p for p in summary.get("account", {}).get("positions", [])}

    sell_block = summary.get("sell", {})
    sell_signals = sell_block.get("signals")
    if sell_signals is None:      # 兼容只传 red/orange/yellow 的情况
        sell_signals = []
        for key in ("red", "orange", "yellow", "others"):
            sell_signals.extend(sell_block.get(key) or [])

    for sig in sell_signals:
        code = sig.get("code")
        price = (positions.get(code) or {}).get("current_price")
        if price is None:
            price = sig.get("suggested_price")
        db.save_signal(
            date=today, code=code, name=sig.get("name", ""), side="sell",
            level=sig.get("level", ""), score=float(sig.get("severity_score") or 0),
            reason=(sig.get("reason") or "")[:200], price=price or 0.0,
        )
        wrote += 1

    buy_block = summary.get("buy", {})
    buy_signals = buy_block.get("signals")
    if buy_signals is None:
        buy_signals = []
        for key in ("strong", "watch", "hold"):
            buy_signals.extend(buy_block.get(key) or [])

    for sig in buy_signals:
        db.save_signal(
            date=today, code=sig.get("code"), name=sig.get("name", ""), side="buy",
            level=sig.get("level", ""), score=float(sig.get("score") or 0),
            reason=(sig.get("reason") or "")[:200], price=sig.get("price") or 0.0,
        )
        wrote += 1
    return wrote


def _price_after(code: str, start: str, horizon: int) -> Optional[float]:
    """信号日之后第 horizon 个交易日的收盘价（不足则取最新一根）。"""
    bars = adapter.get_stock_history(code, days=120)
    if not bars:
        return None
    idx = None
    for i, b in enumerate(bars):
        if str(b["date"])[:10] >= start:
            idx = i
            break
    if idx is None or idx + 1 >= len(bars):
        return None
    target = min(idx + horizon, len(bars) - 1)
    if target <= idx:
        return None
    return bars[target]["close"]


def review(days: int = DEFAULT_DAYS, horizon: int = DEFAULT_HORIZON) -> dict:
    """回看最近 days 天的信号，统计各档位命中率。"""
    since = (date.today() - timedelta(days=days)).isoformat()
    today = date.today().isoformat()
    rows = [r for r in db.list_signals(since) if r["date"] < today]

    detail = []
    for r in rows:
        after = _price_after(r["code"], r["date"], horizon)
        ret = None
        if after and r["price"]:
            ret = round((after / r["price"] - 1) * 100, 2)
        hit = None
        if ret is not None:
            hit = (ret < 0) if r["side"] == "sell" else (ret > 0)
        detail.append({**r, "price_after": after, "ret_pct": ret, "hit": hit})

    def aggregate(side: str) -> dict:
        items = [d for d in detail if d["side"] == side and d["hit"] is not None]
        if not items:
            return {"count": 0, "hit_rate": None, "avg_ret": None}
        hits = sum(1 for d in items if d["hit"])
        returns = [d["ret_pct"] for d in items]
        return {
            "count": len(items),
            "hit_rate": round(hits / len(items) * 100, 1),
            "avg_ret": round(sum(returns) / len(returns), 2),
        }

    by_level: dict[str, dict] = {}
    for d in detail:
        if d["hit"] is None:
            continue
        key = f'{d["side"]}:{d["level"]}'
        rec = by_level.setdefault(key, {"side": d["side"], "level": d["level"],
                                        "count": 0, "hits": 0, "rets": []})
        rec["count"] += 1
        rec["hits"] += 1 if d["hit"] else 0
        rec["rets"].append(d["ret_pct"])
    for rec in by_level.values():
        rec["hit_rate"] = round(rec["hits"] / rec["count"] * 100, 1) if rec["count"] else None
        rec["avg_ret"] = round(sum(rec["rets"]) / len(rec["rets"]), 2) if rec["rets"] else None
        rec.pop("rets", None)

    today_count = len(db.list_signals(today, today))
    return {
        "today_recorded": today_count,
        "since": since,
        "days": days,
        "horizon": horizon,
        "total": len(detail),
        "pending": sum(1 for d in detail if d["hit"] is None),
        "sell": aggregate("sell"),
        "buy": aggregate("buy"),
        "by_level": sorted(by_level.values(), key=lambda r: (r["side"], -(r["hit_rate"] or 0))),
        "recent": detail[:20],
        "note": f"统计口径：信号日收盘价 → {horizon} 个交易日后收盘价。"
                f"卖出信号跌了算命中，买入信号涨了算命中。",
    }
