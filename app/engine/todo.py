"""今日待办：把「卖出信号 + 买点 + 仓位目标」压成一张按顺序执行的清单。

排序逻辑：先卖后买、先急后缓。
- 红色卖出：开盘市价（最优先）
- 橙色卖出：今日内挂单
- 黄色卖出：3 日内处理
- 买点：只在仓位门控放行时出现
金额分配：按严重度依次减仓，直到总仓位降到上限为止（A 股 100 股整数倍）。
"""
from __future__ import annotations

import re
from typing import Optional

from ..data import adapter

REBOUND_RE = re.compile(r"反弹至\s*([\d.]+)\s*元")


def _lots(shares: float) -> int:
    """向下取整到 100 股（A 股最小交易单位）。"""
    return int(shares // 100) * 100


def build_todo(summary: dict) -> dict:
    acc = summary.get("account", {})
    gate = summary.get("gate", {})
    sell = summary.get("sell", {})
    buy = summary.get("buy", {})
    positions = {p.get("code"): p for p in acc.get("positions", [])}

    total_assets = acc.get("total_assets") or 0
    pos_ratio = acc.get("position_ratio") or 0
    target = gate.get("total_max") or 0
    need_value = max(0.0, (pos_ratio - target) / 100 * total_assets)
    need_total = need_value

    items: list[dict] = []

    # 休市时改成「下一交易日」口径，避免让人误以为今天能挂单
    try:
        trading_today = adapter.is_trading_day()
    except Exception:
        trading_today = True
    next_day = None if trading_today else adapter.next_trade_day()
    day_tag = "" if trading_today else (next_day or "下一交易日")

    # 1) 先按「级别 → 市值」排序：同级别时大仓位先动，避免同分时顺序随机
    ranked: list[dict] = []
    for signals, prio, action in (
        (sell.get("red", []), 1, "开盘市价卖出" if trading_today else f"{day_tag} 开盘市价卖出"),
        (sell.get("orange", []), 2, "今日内挂单卖出" if trading_today else f"{day_tag} 挂单卖出"),
        (sell.get("yellow", []), 3, "3 日内处理"),
    ):
        for s in signals:
            pos = positions.get(s.get("code")) or {}
            ranked.append({"sig": s, "priority": prio, "action": action,
                           "mv": pos.get("market_value") or 0})
    ranked.sort(key=lambda x: (x["priority"], -x["mv"]))
    sell4_value = sum(r["mv"] for r in ranked)

    for row in ranked:
        s = row["sig"]
        code = s.get("code")
        pos = positions.get(code) or {}
        price = pos.get("current_price") or s.get("suggested_price") or 0
        mv = pos.get("market_value") or 0
        shares = pos.get("shares") or 0
        reason = s.get("reason") or ""

        # 深套票：理由里已经给了「反弹至 X 元」，就按限价挂，别追低割肉
        m = REBOUND_RE.search(reason)
        limit_price = round(float(m.group(1)), 2) if m else None
        action = (f"限价 {limit_price} 挂单（反弹价）" if limit_price
                  else row["action"])
        if not trading_today and limit_price:
            action = f"{day_tag} {action}"

        suggest_shares = None
        if price and shares:
            if need_value > 0:
                sell_value = min(mv, need_value)
                lots = _lots(sell_value / price)
                if lots >= 100:
                    suggest_shares = min(lots, shares)
                    need_value = max(0.0, need_value - suggest_shares * price)
            else:
                suggest_shares = shares
                need_value = 0.0

        amount = (suggest_shares or 0) * price
        items.append({
            "priority": row["priority"],
            "kind": "sell",
            "level": s.get("level"),
            "code": code,
            "name": s.get("name"),
            "action": action,
            "price": round(price, 2) if price else None,
            "limit_price": limit_price,
            "shares": suggest_shares,
            "amount": round(amount, 0) if amount else None,
            "reason": reason,
        })

    sold_codes = {it["code"] for it in items}

    # 2) 卖点不够填平仓位目标时，明确点出缺口并从最大持仓里给建议
    gap_suggestion = None
    if ranked and need_value > 0:
        rest = [p for c, p in positions.items() if c not in sold_codes and (p.get("shares") or 0) > 0]
        rest.sort(key=lambda p: -(p.get("market_value") or 0))
        if rest:
            p = rest[0]
            price = p.get("current_price") or 0
            lots = _lots(min(need_value, p.get("market_value") or 0) / price) if price else 0
            lots = min(lots, p.get("shares") or 0)
            gap_suggestion = {"code": p.get("code"), "name": p.get("name"),
                              "shares": lots or None, "price": round(price, 2) if price else None}
            items.append({
                "priority": 4, "kind": "gap", "level": "gap",
                "code": p.get("code"), "name": p.get("name"),
                "action": "补足差额（该股无卖点，减最大持仓）",
                "price": round(price, 2) if price else None,
                "limit_price": None,
                "shares": lots or None,
                "amount": round((lots or 0) * price, 0) if lots else None,
                "reason": f"{len(ranked)} 只卖点只够回笼 {(sell4_value / 1e4):.1f} 万，"
                          f"距目标还差 {need_value / 1e4:.1f} 万；{p.get('name')} 是最大持仓且无卖点",
            })

    can_buy = bool(gate.get("can_buy"))
    if can_buy:
        for b in (buy.get("strong") or [])[:3]:
            items.append({
                "priority": 4, "kind": "buy", "level": "strong", "code": b.get("code"),
                "name": b.get("name"), "action": "买点候选（仓位允许）",
                "price": b.get("price"), "shares": None, "amount": None,
                "reason": b.get("reason") or b.get("rating_label"),
            })
    else:
        items.append({
            "priority": 5, "kind": "block", "level": "gate", "code": None, "name": None,
            "action": "买入被锁定：仓位超上限，先减仓",
            "price": None, "shares": None, "amount": None,
            "reason": "；".join(gate.get("blockers") or []) or f"仓位 {pos_ratio}% > 上限 {target}%",
        })

    items.sort(key=lambda x: x["priority"])

    lines = []
    for i, it in enumerate(items, 1):
        head = f"{i}. "
        if it["shares"]:
            head += f"{it['name']} {it['code']} {it['action']} {it['shares']} 股（约 {it['amount'] / 1e4:.1f} 万）"
        elif it["name"]:
            head += f"{it['name']} {it['code']} {it['action']}"
        else:
            head += it["action"]
        if it.get("price") and not it["shares"]:
            head += f"（参考价 {it['price']}）"
        lines.append(head)

    lines.append(
        f"仓位：{pos_ratio}% → 上限 {target}%"
        + (f"（需回笼约 {need_total / 1e4:.1f} 万）" if need_total > 0 else "（已达标）")
    )
    if not trading_today:
        lines.insert(0, f"【今日休市，以下为 {day_tag} 的计划】")

    return {
        "date": (summary.get("meta") or {}).get("as_of", "")[:10],
        "trade_date": adapter.data_day(),
        "items": items,
        "text": "\n".join(lines),
        "position": {"from": pos_ratio, "to": target,
                     "need_cash": round(need_total, 0)},
        "note": ("今日休市，清单按下一交易日安排；" if not trading_today else "")
                + "股数按 100 股整手向下取整；不足 1 手的不给建议股数，按信号自行处理",
        "trading_day": trading_today,
        "next_trading_day": next_day,
        "gap": gap_suggestion,
        "counts": {
            "sell": sum(1 for i in items if i["kind"] == "sell"),
            "buy": sum(1 for i in items if i["kind"] == "buy"),
        },
    }
