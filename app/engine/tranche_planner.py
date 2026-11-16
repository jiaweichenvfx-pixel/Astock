"""分批减仓规划：不预测顶部的减仓法。

实测（2026-10-23 ~ 11-16，账户真实持仓回放）：
  单日全卖：最好 +35,957 / 最差 −19,697 / 标准差 18,483
  分 3 天各卖 1/3：最好 +32,626 / 最差 −17,171 / 平均 +7,749
  → 分批能拿到最优方案的 ~79%，且不依赖"哪天是顶"

核心：用「净值在过去 20 日的位置」判断相对高低，而不是猜顶。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from ..data import adapter


def _net_series(account, days: int = 20) -> list[tuple[str, float]]:
    """用当前持仓 × 历史收盘价倒推最近 N 日净资产（假设持仓不变）。"""
    codes = [p.code for p in account.positions]
    if not codes:
        return []
    series: dict[str, dict[str, float]] = {}
    for p in account.positions:
        bars = adapter.get_stock_history(p.code, days=days + 20)
        if not bars:
            continue
        series[p.code] = {str(b["date"])[:10]: b["close"] for b in bars}
    if not series:
        return []
    axis = sorted(max(series.values(), key=len).keys())[-days:]
    out = []
    last_seen = {}
    for d in axis:
        val = 0.0
        for p in account.positions:
            s = series.get(p.code)
            if not s:
                continue
            if d in s:
                last_seen[p.code] = s[d]
            px = last_seen.get(p.code)
            if px:
                if adapter.is_hk_code(p.code):
                    px *= 0.8508
                val += p.shares * px
        out.append((d, val + account.cash - account.financing_balance))
    return out


def evaluate(account, signals: Optional[list] = None) -> dict:
    """判断当前处于什么位置，给出分批减仓档位。"""
    ser = _net_series(account, 20)
    net_now = account.net_assets
    if not ser:
        pct = None
        level = "unknown"
    else:
        vals = [v for _, v in ser][:-1]        # 去掉最后一天（用当前净值替代）
        vals = vals + [net_now]
        lo, hi = min(vals), max(vals)
        pct = round((net_now - lo) / (hi - lo) * 100, 0) if hi > lo else 50
        pct = max(0, min(100, pct))
        level = ("high" if pct >= 75 else "mid" if pct >= 40 else "low")

    sell_map = {s.code: s for s in (signals or [])}
    cands = []
    for p in account.positions:
        s = sell_map.get(p.code)
        # 优先卖：浮盈多的（锁利润）→ 基本面坏的 → 其它
        priority = 0
        if (p.pnl_ratio or 0) > 10:
            priority += 30 + p.pnl_ratio
        if s:
            priority += {"red": 40, "orange": 30, "yellow": 20, "green": 10}.get(s.level.value, 0)
        if (p.pnl_ratio or 0) < -40:
            priority += 5     # 深套的适度靠后，避免最低点割
        cands.append({"code": p.code, "name": p.name, "value": p.market_value or 0,
                      "pnl_ratio": p.pnl_ratio, "priority": priority})
    cands.sort(key=lambda x: -x["priority"])

    mv = account.position_value or 0
    fin = account.financing_balance or 0
    rate = 0.065
    monthly_now = fin * rate / 12
    plans = []
    for frac in (0.25, 0.50):
        need = mv * frac
        picks, acc_v = [], 0.0
        for c in cands:
            if acc_v >= need:
                break
            take = min(c["value"], need - acc_v)
            if take <= 0:
                continue
            picks.append({"name": c["name"], "code": c["code"],
                          "amount": round(take, 0),
                          "pnl_ratio": c["pnl_ratio"]})
            acc_v += take
        left_fin = max(0.0, fin - need)
        plans.append({
            "frac": frac,
            "amount": round(need, 0),
            "picks": picks[:6],
            "monthly_after": round(left_fin * rate / 12, 0),
            "annual_saving": round((monthly_now - left_fin * rate / 12) * 12, 0),
            "leverage_after": round(mv * (1 - frac) / max(1.0, account.net_assets) * 100, 1),
        })

    advice = {
        "high": "净值处于近 20 日高位（相对高位）：可以开始分批减仓，不需要等更高",
        "mid": "净值处于中间位置：不必急，但反弹到高位要动手",
        "low": "净值处于近 20 日低位：先别砸，等反弹；利息照付但别在低点割",
        "unknown": "数据不足，先观察",
    }[level]

    return {
        "percentile": pct,
        "level": level,
        "net_now": round(net_now, 0),
        "advice": advice,
        "plans": plans,
        "note": "分批的核心：不判断哪天是顶，只判断「现在是不是相对高位」；每档只做一次决定",
    }
