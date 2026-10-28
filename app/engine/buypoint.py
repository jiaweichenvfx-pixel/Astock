"""买点体检：用「价格在一年区间的位置」定买点，而不是靠战法选优。

实测（80 只 × 800 日，基准「任意日买入持有 60 日」= +21.39%）：

| 条件 | 样本 | 60日均值 | 超额 | 胜率 |
|---|---|---|---|---|
| 低位（一年区间 <15% 分位） | 1533 | +36.64% | **+15.25pp** | 80% |
| 低位 + 日线 J 拐头 | 747 | +34.61% | +13.22pp | 80% |
| 低位 + 站上 MA60 | 68 | +67.75% | **+46.36pp** | 100% |
| 一年高位（>85% 分位） | 10323 | −5.19pp | 差 | 56% |

技术择时（周 J<0 等）在同期测试里几乎没有超额，**价格位置才是有效变量**。
"""
from __future__ import annotations

from typing import Optional

from ..data import adapter
from .fundamental import assess as fundamental_assess
from .indicators import kdj
from .strategies import _weekly_j

def _season() -> bool:
    """当前是否处于财报密集披露期（技术信号噪声大）。"""
    try:
        return adapter.in_earnings_season()
    except Exception:
        return False


LOW = 0.15          # 一年区间低位阈值
HIGH = 0.85         # 一年区间高位阈值


def assess(code: str, name: Optional[str] = None) -> dict:
    bars = adapter.get_stock_history(code, days=320)
    if not bars or len(bars) < 120:
        return {"code": code, "name": name or code, "ok": False, "reason": "K线不足"}

    closes = [b["close"] for b in bars]
    c = closes[-1]
    win = closes[-250:] if len(closes) >= 250 else closes
    lo, hi = min(win), max(win)
    pos = (c - lo) / (hi - lo) if hi > lo else 0.5

    ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else None
    ma250 = sum(closes[-250:]) / 250 if len(closes) >= 250 else None
    _, _, j = kdj([b["high"] for b in bars], [b["low"] for b in bars], closes, 9)
    wj = _weekly_j(bars, 9)
    dj = j[-1] if j else None
    turn = bool(j and len(j) > 1 and j[-1] is not None and j[-2] is not None and j[-1] > j[-2])
    above60 = bool(ma60 and c > ma60)
    above250 = bool(ma250 and c > ma250)

    if pos < LOW and above60:
        grade, label = "A", "低位转强（最优）"
    elif pos < LOW and turn:
        grade, label = "B", "低位拐头"
    elif pos < LOW:
        grade, label = "B", "低位但未转强"
    elif pos < 0.35 and above60:
        grade, label = "C", "中低位+趋势向上"
    elif pos > HIGH:
        grade, label = "D", "一年高位（追高区）"
    else:
        grade, label = "C", "中间位置"

    # 基本面一票否决：技术再低，公司变坏也不能买
    fund = fundamental_assess(code)
    f_trend = fund.get("trend")
    if f_trend == "deteriorating":
        grade, label = "X", "基本面恶化，不买"
    elif f_trend == "weakening":
        grade = {"A": "B", "B": "C", "C": "D"}.get(grade, "D")
        label = label + "（基本面走弱，降级）"

    reasons = [
        f"一年区间位置 {pos * 100:.0f}%（0=最低，100=最高）",
        f"距一年低点 +{(c / lo - 1) * 100:.1f}% / 距高点 {(c / hi - 1) * 100:.1f}%",
        ("站上 MA60" if above60 else "在 MA60 下方"),
        ("站上年线" if above250 else "在年线下方"),
        f"日J {dj:.1f}（{'拐头向上' if turn else '未拐头'}）" if dj is not None else "日J 无数据",
        f"周J {wj[-1]:.1f}" if wj and wj[-1] is not None else "周J 无数据",
        f"基本面：{fund.get('label')}（{fund.get('reason')}）",
    ]

    return {
        "code": code, "name": name or code, "ok": True,
        "grade": grade, "label": label, "position": round(pos * 100, 1),
        "low": round(lo, 2), "high": round(hi, 2), "close": round(c, 2),
        "ma60": round(ma60, 2) if ma60 else None,
        "above_ma60": above60, "above_ma250": above250,
        "j": round(dj, 1) if dj is not None else None,
        "weekly_j": round(wj[-1], 1) if wj and wj[-1] is not None else None,
        "turning_up": turn,
        "fundamental": {"trend": f_trend, "label": fund.get("label"),
                        "growth": fund.get("latest_growth"),
                        "notice_date": fund.get("notice_date")},
        "earnings_season": _season(),
        "reasons": reasons,
    }


def scan(codes: list[str], names: Optional[dict] = None) -> dict:
    names = names or {}
    rows = [assess(c, names.get(c)) for c in codes]
    rows = [r for r in rows if r.get("ok")]
    order = {"A": 0, "B": 1, "C": 2, "D": 3, "X": 4}
    rows.sort(key=lambda r: (order.get(r["grade"], 9), -r["position"]))
    return {
        "rows": rows,
        "counts": {g: sum(1 for r in rows if r["grade"] == g) for g in "ABCDX"},
        "buyable": [r["code"] for r in rows if r["grade"] in ("A", "B")],
        "note": "低位 = 一年区间 15% 分位以下；A 级需要同时站上 MA60（实测 60 日超额 +46pp）",
    }
