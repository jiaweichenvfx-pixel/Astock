"""战法周期权重：融资盘不能长期拿着，权重必须按账户健康度动态调整。

核心逻辑：
1. 有融资 → 每天产生利息，长线持有的隐性成本随时间线性增长；
2. 融资占净资产越高，越应该偏短线（快进快出，减少利息暴露）；
3. 健康度越低（浮亏大/仓位满/触发卖点多），越应该偏短线防守；
4. 没融资、健康度高，长线权重才恢复到正常水平。

输出：
- weights：短/中/长 权重（百分比，合计 100）
- pool_size：据此分配的自选池只数（总 30 只）
- daily_interest / break_even_annual_pct：利息成本与「长线要赚多少才不亏利息」
"""
from __future__ import annotations

from typing import Optional

DEFAULT_RATE = 0.065       # 年化融资利率 6.5%
TOTAL_SLOTS = 30           # 自选池三池合计只数
BASE = {"short": 0.40, "mid": 0.35, "long": 0.25}


def _norm(w: dict) -> dict:
    """归一到 1，并保证每档至少 5%。"""
    w = {k: max(0.05, v) for k, v in w.items()}
    total = sum(w.values())
    return {k: v / total for k, v in w.items()}


def compute(account, health: Optional[dict] = None,
            rate: float = DEFAULT_RATE) -> dict:
    fin = float(getattr(account, "financing_balance", 0) or 0)
    net = float(getattr(account, "net_assets", 0) or 0)
    health = health or {}
    score = float(health.get("total") or 50)
    mode_label = health.get("mode_label") or health.get("mode") or ""

    fin_ratio = fin / net * 100 if net > 0 else 0.0
    w = dict(BASE)
    reasons: list[str] = []

    # 1) 融资拖累：把长线权重挪给短线
    if fin > 0:
        # 净资产为负（资不抵债）时按最大偏移处理，不能因为算不出比例就不调整
        shift = 0.22 if net <= 0 else min(0.22, fin_ratio / 100 * 0.45)
        w["long"] -= shift * 0.75
        w["short"] += shift * 0.55
        w["mid"] += shift * 0.20
        ratio_txt = f"{fin_ratio:.1f}%" if net > 0 else "资不抵债"
        reasons.append(
            f"融资 {fin / 1e4:.1f} 万（占净资产 {ratio_txt}）：长线每天在付利息，压低长线权重"
        )

    # 2) 健康度偏低：进一步偏短（快进快出、少暴露）
    if score < 40:
        s2 = (40 - score) / 40 * 0.12
        w["short"] += s2
        w["long"] -= s2 * 0.6
        w["mid"] -= s2 * 0.4
        reasons.append(f"健康度 {score:.0f} 分偏低（{mode_label or '防守'}）：优先短线，减少长期暴露")

    w = _norm(w)

    # 3) 利息成本
    daily = fin * rate / 365 if fin > 0 else 0.0
    break_even = (fin * rate / net * 100) if net > 0 else 0.0

    # 4) 自选池只数按权重分配（每档 6~16），并把余数补到权重最大的档，保证合计 = TOTAL_SLOTS
    sizes = {k: max(6, min(16, round(TOTAL_SLOTS * w[k]))) for k in ("short", "mid", "long")}
    delta = TOTAL_SLOTS - sum(sizes.values())
    if delta:
        anchor = max(sizes, key=lambda k: w[k])
        sizes[anchor] += delta

    return {
        "weights": {k: round(w[k] * 100, 1) for k in ("short", "mid", "long")},
        "pool_size": sizes,
        "financing": {"balance": fin, "ratio_pct": round(fin_ratio, 2), "rate": rate},
        "daily_interest": round(daily, 1),
        "monthly_interest": round(daily * 30, 0),
        "annual_interest": round(daily * 365, 0),
        "break_even_annual_pct": round(break_even, 2),
        "hold_cost": {
            "20": round(fin * rate / 365 * 20, 0),
            "60": round(fin * rate / 365 * 60, 0),
            "120": round(fin * rate / 365 * 120, 0),
        },
        "reasons": reasons,
        "note": (
            "长线持有要跑赢融资利息才算赚钱"
            if fin > 0 else "无融资，长线无利息成本"
        ),
    }
