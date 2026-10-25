"""基本面趋势：看「连续几个季度在恶化」，而不是只看最新一期。

为什么需要：
现有卖出规则是「扣非增速 < −30%」的单期阈值 —— 等到触发时往往已经跌了很多。
真实的变化是渐进的：+72% → +27% → −8% → −74% → −196%。
如果只看最新一期，等看到 −196% 时，前面三个季度的信号全浪费了。

判定：
- 连续恶化 ≥2 期（每期比上期低 3pp 以上）且最新已转负 → 🔴 恶化预警
- 连续恶化 ≥2 期但最新仍为正 → 🟡 增速走弱
- 连续改善 ≥2 期 → 🟢 改善
- 其它 → ⚪ 平稳
"""
from __future__ import annotations

from typing import Optional

from ..data import adapter

TREND_SCORE = {"deteriorating": -20, "weakening": -8, "improving": 12, "flat": 0, "unknown": 0}
TREND_LABEL = {"deteriorating": "恶化预警", "weakening": "增速走弱",
               "improving": "改善", "flat": "平稳", "unknown": "无数据"}


def assess(code: str) -> dict:
    data = adapter.get_financial_trend(code)
    if not data or not data.get("series"):
        return {"code": code, "trend": "unknown", "label": TREND_LABEL["unknown"],
                "consecutive": 0, "latest_growth": None, "latest_roe": None,
                "score_delta": 0, "reason": "无财务数据", "series": []}

    series = data["series"]
    latest = series[-1]
    down, up = data["consecutive_down"], data["consecutive_up"]
    g = latest.get("growth")

    if down >= 2 and (g is not None and g < 0):
        trend = "deteriorating"
        reason = f"基本面连续 {down} 期恶化，最新扣非同比 {g:+.1f}%（已转负）"
    elif down >= 2:
        trend = "weakening"
        reason = f"增速连续 {down} 期走弱（仍为正 {g:+.1f}%）"
    elif up >= 2:
        trend = "improving"
        reason = f"增速连续 {up} 期改善（最新 {g:+.1f}%）"
    else:
        trend = "flat"
        reason = f"基本面平稳（最新扣非同比 {g:+.1f}%）" if g is not None else "基本面平稳"

    return {
        "code": code, "trend": trend, "label": TREND_LABEL[trend],
        "consecutive": down if trend in ("deteriorating", "weakening") else up,
        "latest_growth": g, "latest_roe": latest.get("roe"),
        "report_date": latest.get("date"),
        "score_delta": TREND_SCORE[trend],
        "reason": reason,
        "series": series[-6:],
    }


def assess_map(codes: list[str]) -> dict[str, dict]:
    """批量评估，返回 {code: assess}（供卖出信号使用）。"""
    return {c: assess(c) for c in codes}


def scan(codes: list[str]) -> dict:
    out = {}
    for code in codes:
        out[code] = assess(code)
    order = {"deteriorating": 0, "weakening": 1, "improving": 2, "flat": 3, "unknown": 4}
    rows = sorted(out.values(), key=lambda x: (order.get(x["trend"], 9), x["code"]))
    return {
        "rows": rows,
        "deteriorating": [r["code"] for r in rows if r["trend"] == "deteriorating"],
        "improving": [r["code"] for r in rows if r["trend"] == "improving"],
    }
