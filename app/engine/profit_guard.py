"""盈利保护监控：浮盈达到门槛后，盯住超买信号，别让利润回吐。

实测依据（50 只 × 640 个阶段高点，之后 60 日内跌超 20%）：
  RSI>75 平均在顶部后 3 天触发，捕获 122% 的利润；MACD 顶背离 7 天、110%；
  而「从高点回撤 20%」才动手只能保住 52%——等回撤信号出来已经晚了。

所以对**有浮盈**的持仓，主动盯超买，而不是等它跌下来。
"""
from __future__ import annotations

from typing import Optional

from ..data import adapter
from .vote import compute_tech

WATCH_PNL = 10.0     # 浮盈超过这个值进入观察
PROTECT_PNL = 20.0   # 浮盈超过这个值，"锁利润"才真正有意义


def _state(pnl: float, rsi: Optional[float], j: Optional[float],
           divergence: bool, dd: Optional[float]) -> tuple[str, str]:
    """返回 (状态, 说明)。"""
    overbought = (rsi is not None and rsi >= 75) or (j is not None and j >= 100)
    if overbought and pnl >= PROTECT_PNL:
        return "lock", "超买 + 浮盈足够，建议卖一半锁利润"
    if divergence and pnl >= PROTECT_PNL:
        return "lock", "MACD 顶背离，建议减仓锁利润"
    if overbought:
        return "near", "已超买，但浮盈还不够厚，先观察"
    if dd is not None and dd <= -15 and pnl >= PROTECT_PNL:
        return "lock", f"从高点回撤 {abs(dd):.0f}%，锁住剩余利润"
    if dd is not None and dd <= -25:
        return "weak", f"已从高点回撤 {abs(dd):.0f}%，利润在缩水，趋势转弱"
    if (rsi is not None and rsi >= 65) or (j is not None and j >= 85):
        return "near", "接近超买区"
    return "ok", "趋势健康，继续持有"


def scan(account, watch_pnl: float = WATCH_PNL) -> dict:
    """扫描浮盈超过 watch_pnl 的持仓，给出锁利润状态。"""
    rows = []
    for p in account.positions:
        pnl = p.pnl_ratio or 0
        if pnl < watch_pnl:
            continue
        bars = adapter.get_stock_history(p.code, days=300)
        if not bars or len(bars) < 30:
            continue
        closes = [b["close"] for b in bars]
        t = compute_tech(closes, bars)
        state, why = _state(pnl, t.get("rsi"), t.get("j"), bool(t.get("macd_divergence")),
                            t.get("dd_60d"))
        rows.append({
            "code": p.code, "name": p.name,
            "pnl_ratio": round(pnl, 1),
            "price": p.current_price,
            "cost": p.cost_price,
            "shares": p.shares,
            "rsi": round(t["rsi"], 0) if t.get("rsi") is not None else None,
            "j": round(t["j"], 0) if t.get("j") is not None else None,
            "divergence": bool(t.get("macd_divergence")),
            "dd_60d": t.get("dd_60d"),
            "state": state, "why": why,
            "half_shares": max(100, (p.shares // 200) * 100) if p.shares >= 200 else p.shares,
        })
    order = {"lock": 0, "weak": 1, "near": 2, "ok": 3}
    rows.sort(key=lambda r: (order.get(r["state"], 9), -r["pnl_ratio"]))
    return {
        "rows": rows,
        "lock_count": sum(1 for r in rows if r["state"] == "lock"),
        "weak_count": sum(1 for r in rows if r["state"] == "weak"),
        "watch_count": len(rows),
        "watch_pnl": watch_pnl,
        "protect_pnl": PROTECT_PNL,
        "note": (f"浮盈超过 {watch_pnl:.0f}% 进入监控；超过 {PROTECT_PNL:.0f}% 时"
                 f"RSI>75 / J>100 / MACD 顶背离 出现就卖一半"),
    }
