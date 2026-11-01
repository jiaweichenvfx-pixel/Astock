"""行动卡：把分散的信号翻译成「现在该做什么」。

目标是让人一眼看懂，不需要理解 17 套战法、5 级信号、A/B/C 评级：
  1. 现在能不能买（一句话 + 理由）
  2. 要卖哪几只、卖多少股、挂什么价、卖掉之后有什么好处
  3. 为什么是现在
"""
from __future__ import annotations

import re
from typing import Optional


def _money(v: float) -> str:
    return f"{v:,.0f}"


def _yan(v: float) -> str:
    """金额转「万」的口语表达。"""
    return f"{v/10000:.1f} 万" if v >= 10000 else f"{v:,.0f} 元"


def _growth_from(reason: str) -> Optional[float]:
    m = re.search(r"扣非[^%-]*?(-?\d+(?:\.\d+)?)%", reason)
    return float(m.group(1)) if m else None


def _plain_why(sig: Optional[dict], fin: Optional[dict] = None) -> str:
    """把一个卖出信号翻译成人话（只讲最重要的那一句）。"""
    if not sig:
        return "触发卖出条件"
    mech = sig.get("mechanism")
    reason = sig.get("reason") or ""
    g = _growth_from(reason)

    if mech == "fundamental":
        if "由正转负" in reason:
            return "最新季度利润由正转负"
        if g is not None and g < 0:
            return f"最新季度利润同比下滑 {abs(g):.0f}%"
        return "基本面转差"
    if mech == "hard_stop":
        return "价格跌破止损线"
    if mech == "time_stop":
        return "持有很久没有表现，资金效率低"
    if mech == "technical":
        return "放量滞涨，短期涨不动了"
    return "触发卖出条件"


def build(summary: dict, todo: dict, financing: Optional[dict] = None,
          buypoint: Optional[dict] = None) -> dict:
    """组装行动卡。summary/todo/financing/buypoint 都来自各自接口。"""
    acc = summary.get("account") or {}
    gate = summary.get("gate") or {}
    signals = {s["code"]: s for s in ((summary.get("sell") or {}).get("signals") or [])}
    positions = {p["code"]: p for p in (acc.get("positions") or [])}

    fin_now = (financing or {}).get("monthly") or 0
    fin_balance = (financing or {}).get("balance") or acc.get("financing_balance") or 0
    rate = (financing or {}).get("rate") or 0.065

    # ── 卖：来自待办的真实挂单 ──
    sells = []
    total_amount = 0.0
    for it in (todo.get("items") or []):
        if it.get("action_type") == "buy" or it.get("action", "").startswith("买入"):
            continue
        shares = it.get("shares")
        price = it.get("limit_price") or (positions.get(it.get("code")) or {}).get("current_price")
        if not shares or not price:
            continue
        amount = float(shares) * float(price)
        total_amount += amount
        sig = signals.get(it.get("code"))
        pos = positions.get(it.get("code")) or {}
        sells.append({
            "code": it.get("code"),
            "name": it.get("name"),
            "shares": shares,
            "price": round(float(price), 2),
            "amount": round(amount, 0),
            "level": (sig or {}).get("level"),
            "why": (_plain_why(sig, pos) if sig
                    else "没有卖点，只为凑够减仓金额而卖（选最抗跌的）"),
            "detail": (("浮盈 +" if (pos.get("pnl_ratio") or 0) >= 0 else "浮亏 ")
                       + f"{abs(pos.get('pnl_ratio'))}%") if pos.get("pnl_ratio") is not None else "",
            "order": f"限价 {round(float(price),2)} 卖出 {shares} 股（挂单，不追跌）",
        })

    # ── 买：现在能不能买 ──
    can_buy = bool(gate.get("can_buy"))
    no_buy = []
    if not can_buy:
        pct = acc.get("position_ratio")
        lev = acc.get("financing_ratio")
        if pct and pct > (gate.get("total_max") or 70):
            no_buy.append(f"仓位 {pct}% 超过上限 {gate.get('total_max')}%")
        lmax = gate.get("leverage_max")
        if lev is not None and lmax is not None and lev > lmax:
            no_buy.append(f"融资 {lev}% 超过上限 {lmax}%" if lmax > 0
                          else f"当前是急救模式，不允许有任何融资（现 {lev}%）")
        if (acc.get("cash_ratio") or 0) < (gate.get("cash_min") or 30):
            no_buy.append(f"现金只剩 {acc.get('cash_ratio')}%，低于 {gate.get('cash_min')}%")
    buy_list = []
    if can_buy and buypoint:
        for r in (buypoint.get("rows") or []):
            if r.get("grade") in ("A", "B") and not r.get("vetoed"):
                buy_list.append({
                    "name": r.get("name"), "grade": r.get("grade"),
                    "price": r.get("price"), "why": r.get("summary") or "",
                })
        buy_list = buy_list[:3]

    # ── 卖掉之后的好处 ──
    left = max(0.0, fin_balance - total_amount)
    monthly_after = left * rate / 12
    saving = max(0.0, fin_now - monthly_after)

    # ── 一句话结论 ──
    n_red = (summary.get("sell") or {}).get("red") or 0
    if sells and not can_buy:
        verdict = f"现在只做一件事：按下面 {len(sells)} 笔挂单减仓，然后不要再买"
    elif sells and can_buy:
        verdict = f"先减仓 {len(sells)} 笔，卖完再看有没有值得买的"
    elif not can_buy:
        verdict = "现在不买，也不需要卖，等信号"
    else:
        verdict = "可以买，但要小仓位试"

    # 为什么是现在
    env = summary.get("market_env") or {}
    ctx = []
    if env.get("temperature_label"):
        ctx.append(f"大盘{env['temperature_label']}")
    if not env.get("index_above_ma20", True):
        ctx.append("指数在 20 日线下方")
    if (summary.get("health") or {}).get("total") is not None:
        h = summary["health"]
        ctx.append(f"健康度 {h['total']} 分（{h.get('mode_label','')}）")
    if (summary.get("meta") or {}).get("earnings_season"):
        ctx.append("财报季，技术信号噪声大")

    return {
        "date": todo.get("trade_date") or todo.get("date"),
        "verdict": verdict,
        "context": " · ".join(ctx),
        "earnings_season": bool((summary.get("meta") or {}).get("earnings_season")),
        "can_buy": can_buy,
        "no_buy_reasons": no_buy,
        "sells": sells,
        "sell_total": round(total_amount, 0),
        "buy": buy_list,
        "effect": {
            "financing_before": round(fin_balance, 0),
            "financing_after": round(left, 0),
            "monthly_before": round(fin_now, 0),
            "monthly_after": round(monthly_after, 0),
            "monthly_saving": round(saving, 0),
            "annual_saving": round(saving * 12, 0),
            "text": (f"卖出可回收 {_yan(total_amount)}；若全部还融资，"
                     f"月息 {_money(fin_now)} → {_money(monthly_after)} 元"
                     f"（每月省 {_money(saving)}，一年省 {_yan(saving*12)}）"),
        } if sells else {},
        "red_count": n_red,
        "note": "挂单是限价单，不保证成交；不追跌卖是纪律，不是必须成交",
    }
