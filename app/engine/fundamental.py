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

BAD_TYPES = {"首亏", "预减", "增亏", "续亏"}
GOOD_TYPES = {"预增", "扭亏", "略增", "续盈"}

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

    prev_g = series[-2]["growth"] if len(series) > 1 else None
    first_negative = (g is not None and g < 0 and prev_g is not None and prev_g >= 0)

    if down >= 2 and (g is not None and g < 0):
        trend = "deteriorating"
        reason = f"基本面连续 {down} 期恶化，最新扣非同比 {g:+.1f}%（已转负）"
    elif first_negative:
        trend = "deteriorating"
        reason = f"扣非同比首次转负（{prev_g:+.1f}% → {g:+.1f}%），单季度恶化"
    elif g is not None and g <= -30:
        trend = "deteriorating"
        reason = f"扣非同比 {g:+.1f}%，深度下滑（即使不连续也要预警）"
    elif down >= 2:
        trend = "weakening"
        reason = f"增速连续 {down} 期走弱（仍为正 {g:+.1f}%）"
    elif up >= 2:
        trend = "improving"
        reason = f"增速连续 {up} 期改善（最新 {g:+.1f}%）"
    else:
        trend = "flat"
        reason = f"基本面平稳（最新扣非同比 {g:+.1f}%）" if g is not None else "基本面平稳"

    # 业绩预告：比正式财报早 1~3 个月；与已披露数据冲突时不采信
    forecast = None
    try:
        fc = adapter.get_earnings_forecast([code]).get(code)
    except Exception:
        fc = None
    if fc:
        actual_period = latest.get("date", "").replace("-", "")
        actual_g = latest.get("growth")
        fc["conflict"] = False
        if fc.get("period") == actual_period and fc.get("change_pct") is not None and actual_g is not None:
            # 同一报告期：预告与实际差 30pp 以上 → 数据可疑，以实际为准
            if abs(fc["change_pct"] - actual_g) > 30:
                fc["conflict"] = True
                fc["note"] = f"预告 {fc['change_pct']:+.1f}% 与实际 {actual_g:+.1f}% 不符，以实际为准"
        elif fc.get("period", "") > actual_period and not fc["conflict"]:
            # 未来报告期的预告：负数类直接提前预警
            if fc.get("type") in BAD_TYPES and abs(fc.get("change_pct") or 0) > 30:
                trend = "deteriorating"
                label = TREND_LABEL[trend]
                reason = (f"业绩预告{fc['type']} {fc['change_pct']:+.1f}%"
                          f"（{fc['notice_date']} 公告），早于正式财报")
            elif fc.get("type") in GOOD_TYPES:
                fc["positive"] = True
    forecast = fc

    return {
        "code": code, "trend": trend, "label": TREND_LABEL[trend],
        "forecast": forecast,
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
