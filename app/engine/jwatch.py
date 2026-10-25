"""J 值监控：日线 / 周线 J 的位置与「是否共振低位」。

实测结论（80 只 × 800 日，对比「随便哪天买入」基准 +5.33%）：
- 只日线 J<−10：超额 **−2.3%**（比随机买还差，接的是下跌中继）
- 只周线 J<0：超额 **+4.5%**，胜率 68%（真正有效）
- 日线+周线共振：超额 +2.1%，胜率 73%，但极罕见（800 天仅 30 次）
- 日+周+月三共振：800 天 0 次（等不到）

所以判定顺序是：**周线定方向，日线定时点**。
"""
from __future__ import annotations

from typing import Optional

from ..data import adapter
from .indicators import kdj
from .strategies import _weekly_j

DAILY_THRESH = -10.0     # 日线「超跌」线
WEEKLY_THRESH = 0.0      # 周线「低位」线
OVERBOUGHT = 100.0       # J 值超买（做 T 的高抛参考）


def snapshot(codes: list[str], names: Optional[dict] = None) -> dict:
    names = names or {}
    rows = []
    for code in codes:
        bars = adapter.get_stock_history(code, days=320)
        if not bars or len(bars) < 40:
            continue
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        closes = [b["close"] for b in bars]
        _, _, j = kdj(highs, lows, closes, 9)
        wj = _weekly_j(bars, 9)

        dj = j[-1] if j else None
        dj_prev = j[-2] if len(j) > 1 else None
        wj_now = wj[-1] if wj else None
        turning = (dj is not None and dj_prev is not None and dj > dj_prev)

        if wj_now is None or dj is None:
            status, label, order = "none", "数据不足", 9
        elif dj > OVERBOUGHT:
            status, label, order = "overbought", "J 值超买·高抛参考", 6
        elif wj_now < WEEKLY_THRESH and dj < DAILY_THRESH and turning:
            status, label, order = "resonance_buy", "共振买点", 1
        elif wj_now < WEEKLY_THRESH and dj < DAILY_THRESH:
            status, label, order = "resonance_wait", "共振低位·等拐头", 2
        elif wj_now < WEEKLY_THRESH and dj < 0:
            status, label, order = "weekly_daily_neg", "周线低位+日线负值", 3
        elif wj_now < WEEKLY_THRESH:
            status, label, order = "weekly_low", "周线低位", 4
        elif dj < DAILY_THRESH:
            status, label, order = "daily_deep", "仅日线超跌", 5
        elif dj < 0:
            status, label, order = "daily_neg", "日线负值", 5
        else:
            status, label, order = "normal", "正常", 7

        rows.append({
            "code": code, "name": names.get(code) or code,
            "j": round(dj, 1) if dj is not None else None,
            "j_prev": round(dj_prev, 1) if dj_prev is not None else None,
            "weekly_j": round(wj_now, 1) if wj_now is not None else None,
            "turning_up": turning,
            "status": status, "label": label, "order": order,
            "close": round(closes[-1], 2),
            "pct_chg": round((closes[-1] / closes[-2] - 1) * 100, 2) if len(closes) > 1 else None,
        })

    rows.sort(key=lambda r: (r["order"], r["weekly_j"] if r["weekly_j"] is not None else 999))
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    return {
        "date": adapter.data_day(),
        "thresholds": {"daily": DAILY_THRESH, "weekly": WEEKLY_THRESH},
        "rows": rows,
        "counts": counts,
        "note": "日线单独超跌是负超额（−2.3%），要等周线也进入低位才算真买点",
    }
