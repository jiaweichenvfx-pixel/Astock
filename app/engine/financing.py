"""融资成本：每月底的利息是**现金支出**，不是账面浮亏。

它直接减少净资产、且不随行情好坏而暂停。所以必须单独算清楚：
- 每月要付多少
- 这笔钱相当于每年必须跑赢多少
- 还掉一部分能省多少
"""
from __future__ import annotations

import calendar
from datetime import date
from typing import Optional

from .. import config
from ..schemas import Account


def _cfg() -> dict:
    return config.load_config().get("financing") or {}


def _next_payment(today: Optional[date] = None) -> tuple[str, int]:
    """下一个付息日与剩余天数（默认月末）。"""
    today = today or date.today()
    last = calendar.monthrange(today.year, today.month)[1]
    d = date(today.year, today.month, last)
    if today > d:  # 已过本月最后一天（理论上不会）
        nxt = date(today.year + (1 if today.month == 12 else 0),
                   (today.month % 12) + 1, 1)
        d = date(nxt.year, nxt.month, calendar.monthrange(nxt.year, nxt.month)[1])
    return d.isoformat(), (d - today).days


def assess(account: Account, rate: Optional[float] = None) -> dict:
    cfg = _cfg()
    rate = float(rate or cfg.get("rate", 0.065))
    fin = float(account.financing_balance or 0)
    net = float(account.net_assets or 0)

    monthly = fin * rate / 12
    annual = fin * rate
    daily = fin * rate / 365
    pay_date, days_left = _next_payment()

    # 打平线：融资买的仓位需要多少年化收益才刚好覆盖利息
    breakeven_on_fin = rate * 100
    breakeven_on_net = (annual / net * 100) if net > 0 else 0.0

    options = []
    for repay in (50000, 80000, 100000, 150000):
        if repay >= fin:
            continue
        left = fin - repay
        m2 = left * rate / 12
        options.append({
            "repay": repay,
            "left": round(left, 2),
            "monthly_after": round(m2, 0),
            "annual_saving": round((monthly - m2) * 12, 0),
            "pct_of_net": round(repay / net * 100, 1) if net > 0 else None,
        })
    if fin > 0:
        options.append({
            "repay": round(fin, 2), "left": 0.0, "monthly_after": 0.0,
            "annual_saving": round(annual, 0),
            "pct_of_net": round(fin / net * 100, 1) if net > 0 else None,
        })

    if fin <= 0:
        verdict = "无融资负债，没有利息支出"
    elif breakeven_on_net >= 6:
        verdict = f"每年要先赚 {breakeven_on_net:.1f}% 才够付利息，相当于白干 {breakeven_on_net:.0f}% 的收益"
    else:
        verdict = f"利息负担 {breakeven_on_net:.1f}%/年，需要优先用卖点回笼资金偿还"

    return {
        "balance": round(fin, 2),
        "rate": rate,
        "daily": round(daily, 1),
        "monthly": round(monthly, 0),
        "annual": round(annual, 0),
        "next_payment_date": pay_date,
        "days_to_payment": days_left,
        "breakeven_on_financing_pct": round(breakeven_on_fin, 2),
        "breakeven_on_net_pct": round(breakeven_on_net, 2),
        "monthly_pct_of_net": round(monthly / net * 100, 2) if net > 0 else None,
        "options": options,
        "verdict": verdict,
        "note": "利息是现金支出，不会因为行情差而暂停；每月底前记得留足现金",
    }
