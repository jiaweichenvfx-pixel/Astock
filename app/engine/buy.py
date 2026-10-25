"""模块7（提前）：买点生成器 = 质量门控 + 多策略投票 + 仓位门控（三位一体）。"""
from __future__ import annotations

from typing import Optional

from ..schemas import BuySignal, Mode, Position, Pool, QualityInfo, Rating
from . import vote


def generate_buy_signal(
    code: str,
    name: str,
    price: Optional[float],
    tech: dict,
    position: Optional[Position],
    quality: Optional[QualityInfo],
    mode: Mode,
    gate_can_buy: bool,
    roe: Optional[float] = None,
    pe: Optional[float] = None,
) -> BuySignal:
    """生成单个标的的买点信号。

    三位一体确认：① 质量过关（卓越/合格）② 投票分达标 ③ 仓位门控通过。
    """
    signals = vote.compute_all_signals(tech, position, roe, pe)
    score = vote.weighted_vote(signals, mode)
    level, level_label = vote.buy_level(score)

    rating = quality.rating if quality else None
    pool = quality.pool if quality else None

    reasons: list[str] = []
    quality_ok = rating in (Rating.EXCELLENT, Rating.QUALIFIED)
    if not quality_ok:
        reasons.append(f"质量门控拦截（{getattr(rating, 'value', '未知')}）")
    if not gate_can_buy:
        reasons.append("仓位门控拦截：请先减仓")

    # 信号说明
    sig_desc = []
    for k in ("value", "ma", "bollinger", "momentum", "grid"):
        v = signals.get(k, 0)
        if v != 0:
            label = {"value": "长线价值", "ma": "双均线", "bollinger": "布林带", "momentum": "RPS动量", "grid": "网格做T"}[k]
            sig_desc.append(f"{label}{'+' if v > 0 else '-'}")

    # 质量或仓位不过关 → 强制观望
    if not quality_ok or not gate_can_buy:
        level, level_label = "hold", "观望"

    reason = "；".join(reasons) if reasons else ("信号：" + "、".join(sig_desc) if sig_desc else "无明确信号")

    return BuySignal(
        code=code,
        name=name,
        price=price,
        score=score,
        level=level,
        level_label=level_label,
        rating=rating,
        pool=pool,
        signals=signals,
        reason=reason,
        position_gate_pass=gate_can_buy,
    )
