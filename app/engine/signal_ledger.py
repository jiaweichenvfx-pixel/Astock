"""信号账本：系统给的建议，如果照做到今天会怎样。

用户实际没有执行任何信号，所以需要一个「影子账本」来回答：
系统到底行不行？——不依赖用户有没有动手。
"""
from __future__ import annotations

from typing import Optional

from ..data import adapter, db


def build(days: int = 30) -> dict:
    """汇总最近 N 天的卖出信号 + 若照做的结果。"""
    conn = db._conn()
    try:
        rows = conn.execute(
            "SELECT date, code, name, side, level, score, price FROM signals"
            " WHERE side='sell' AND date >= date('now', ?) ORDER BY date DESC",
            (f"-{max(1, min(days, 365))} day",)
        ).fetchall()
    finally:
        conn.close()

    quote_cache: dict[str, Optional[float]] = {}
    items = []
    seen = set()
    for d, code, name, side, level, score, px in rows:
        key = (d, code)
        if key in seen:
            continue
        seen.add(key)
        if code not in quote_cache:
            quote_cache[code] = (adapter.get_quotes([code]).get(code) or {}).get("price")
        now = quote_cache[code]
        if not now or not px:
            continue
        # >0：卖了更好（现在更低）；<0：卖飞了（现在更高）
        saved = (px - now) / px * 100
        items.append({
            "date": d, "code": code, "name": name, "level": level,
            "score": score, "signal_price": round(px, 2), "now_price": round(now, 2),
            "diff_pct": round(saved, 2),
            "verdict": "卖对" if saved > 0 else "卖飞",
        })

    if not items:
        return {"items": [], "count": 0, "avg_diff": None, "right_rate": None,
                "note": "还没有可验证的信号记录"}

    diffs = [x["diff_pct"] for x in items]
    right = sum(1 for d in diffs if d > 0)
    # 按等级
    by_level = {}
    for x in items:
        by_level.setdefault(x["level"], []).append(x["diff_pct"])
    level_stats = [{"level": k, "count": len(v),
                    "avg_diff": round(sum(v) / len(v), 2),
                    "right_rate": round(sum(1 for d in v if d > 0) / len(v) * 100, 0)}
                   for k, v in by_level.items()]

    avg = sum(diffs) / len(diffs)
    if avg > 0.5:
        note = f"若照做，平均多赚 {avg:.1f}% —— 这套卖点在统计上是有效的"
    elif avg < -0.5:
        note = (f"若照做，平均少赚 {abs(avg):.1f}% —— 这套卖点在统计上是无效的，"
                f"不执行反而更好。需要减少技术类信号，只保留基本面与硬止损。")
    else:
        note = f"若照做，平均差 {avg:+.1f}%，接近随机"

    return {
        "items": items[:30],
        "count": len(items),
        "avg_diff": round(avg, 2),
        "right_rate": round(right / len(items) * 100, 0),
        "level_stats": sorted(level_stats, key=lambda x: -x["count"]),
        "note": note,
    }
