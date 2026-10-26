"""统一的战法推荐：同一份回测数据（bt_cache），按用途用不同的权重排序。

为什么需要统一：
之前体检用「综合分」、一键决策用「样本内收益」、做 T 用「命中率+频率」、
自选池用的是另一套选股因子 —— 四个地方各排各的，用户看到的推荐对不上。

现在定义清楚：**只有一份回测数据，不同用途用不同评价函数**。

用途（purpose）：
- hold  ：判断「这只票该用什么节奏拿」→ 综合分（收益 35% + 捕获 30% + 命中 35%）
- trade ：做 T 降本 → 命中 45% + 频率 25% + 单次价差 30%（且必须跑赢往返成本）
- buy   ：新开仓 → 命中 40% + 稳定性 30% + 频率 20% + 单次价差 10%（拒绝排名不稳的）
"""
from __future__ import annotations

from typing import Optional

from ..data import db

T_COST_PCT = 0.302          # A 股往返成本
PURPOSE_LABEL = {"hold": "持有节奏", "trade": "做T降本", "buy": "新开仓"}


def _rows_for(code: str, day: Optional[str] = None) -> list[dict]:
    """取这只票当天在三个周期上的全部战法回测。"""
    from ..data import adapter
    day = day or adapter.data_day()
    out = []
    for r in db.load_bt(day, code):
        payload = r.get("payload") or {}
        if not payload.get("ok"):
            continue
        stats = payload.get("stats") or {}
        for a in payload.get("all") or []:
            out.append({
                "strategy": a["name"], "label": a["label"], "horizon": r["horizon"],
                "total_return": a.get("total_return") or 0,
                "score": a.get("score") or 0,
                "trade_count": a.get("trade_count") or 0,
                "max_drawdown": a.get("max_drawdown") or 0,
                "buy_hit": a.get("buy_hit_rate") or 0,
                "sell_hit": a.get("sell_hit_rate") or 0,
                "median": stats.get("median"),
                "stability": stats.get("stability"),
            })
    return out


def rank(code: str, purpose: str = "hold", day: Optional[str] = None) -> list[dict]:
    rows = _rows_for(code, day)
    if not rows:
        return []

    for r in rows:
        trades = r["trade_count"]
        hit = (r["buy_hit"] + r["sell_hit"]) / 2
        per_trade = r["total_return"] / trades if trades else 0
        freq = min(100.0, trades * 8)
        # 做 T 要的是「约 1% 的单次捕获」：太小覆盖不了成本，太大就不是 T 了
        edge = max(0.0, 100.0 * (1 - min(1.0, abs(per_trade - 1.2) / 1.2)))
        edge_small = min(100.0, max(0.0, per_trade) * 20)
        stab = r["stability"]

        r["per_trade"] = round(per_trade, 2)
        r["hit"] = round(hit, 1)

        if purpose == "trade":
            r["rank_score"] = round(hit * 0.45 + freq * 0.30 + edge * 0.25
                                    + (12 if r["horizon"] == "short" else 0), 1)
            r["eligible"] = trades >= 4 and per_trade > T_COST_PCT
        elif purpose == "buy":
            s = (stab if stab is not None else 0) * 100
            r["rank_score"] = round(hit * 0.40 + min(100.0, s * 0.30) + freq * 0.20 + edge_small * 0.10, 1)
            # 新开仓宁可少赚，也要排名能复现
            r["eligible"] = trades >= 4 and (stab is not None and stab >= 0.25) and per_trade > T_COST_PCT
        else:   # hold
            r["rank_score"] = round(r["score"] * 0.7 + hit * 0.3, 1)
            r["eligible"] = trades >= 2

    eligible = [r for r in rows if r["eligible"]]
    pool = eligible or rows
    pool.sort(key=lambda r: -r["rank_score"])
    return pool


def recommend(code: str, purpose: str = "hold", day: Optional[str] = None) -> dict:
    pool = rank(code, purpose, day)
    if not pool:
        return {"ok": False, "reason": "暂无回测数据", "purpose": purpose}

    top = pool[0]
    reasons = []
    if purpose == "trade":
        reasons.append(f"买卖点命中率 {top['hit']}%")
        reasons.append(f"{top['trade_count']} 笔样本")
        reasons.append(f"单次均 {top['per_trade']:+.2f}%（需 > {T_COST_PCT}% 覆盖成本）")
        if top["horizon"] == "short":
            reasons.append("60 分钟级，适合日内做 T")
    elif purpose == "buy":
        stab = top["stability"]
        reasons.append(f"买卖点命中率 {top['hit']}%")
        reasons.append(f"排名稳定性 {stab if stab is not None else '—'}"
                       + ("（可复现）" if (stab or 0) >= 0.25 else "（偏低）"))
        reasons.append(f"{top['trade_count']} 笔样本")
    else:
        reasons.append(f"综合分 {top['score']}")
        reasons.append(f"累计 {top['total_return']:+.1f}%")
        reasons.append(f"买卖点命中 {top['hit']}%")

    return {
        "ok": True,
        "purpose": purpose,
        "purpose_label": PURPOSE_LABEL.get(purpose, purpose),
        "code": code,
        "strategy": top["strategy"],
        "label": top["label"],
        "horizon": top["horizon"],
        "rank_score": top["rank_score"],
        "metrics": {k: top[k] for k in ("total_return", "score", "trade_count",
                                        "max_drawdown", "hit", "per_trade", "stability")},
        "reasons": reasons,
        "alternates": [{"strategy": r["strategy"], "label": r["label"],
                        "horizon": r["horizon"], "rank_score": r["rank_score"]}
                       for r in pool[1:4]],
    }
