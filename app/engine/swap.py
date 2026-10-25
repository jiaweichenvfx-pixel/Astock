"""模块8：智能换股配对（砍谁 → 买谁）。

配对逻辑：
- 同板块弱换强：劣质持仓 → 同板块强势候选
- 垃圾换核心：黑名单/预警持仓 → 卓越/合格候选
- 高估值换低估：由「弱换强」近似（财务估值字段接入后细化）

前提：先有卖出指令腾出资金（联动模块3 + 仓位门控）。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

from .. import config
from ..schemas import Account, BuySignal, QualityInfo, Rating, Pool, RATING_LABEL


@dataclass
class SwapPair:
    from_code: str
    from_name: str
    from_reason: str            # 为什么砍
    to_code: str
    to_name: str
    to_reason: str              # 为什么买
    match_type: str             # 同板块弱换强 / 垃圾换核心 / 高估值换低估
    to_score: int

    def to_dict(self) -> dict:
        return asdict(self)


def _is_weak(pos_rating: Optional[Rating], pnl_ratio: float) -> tuple[bool, str]:
    """判断持仓是否应被替换，返回 (是否, 原因)。"""
    if pos_rating == Rating.BLACKLIST:
        return True, "黑名单（ST/连续亏损）"
    if pos_rating == Rating.WARNING:
        return True, "预警（增速下滑/亏损）"
    if pnl_ratio < -15:
        return True, f"深套 {pnl_ratio}%"
    return False, ""


def _sector_of(code: str, name: str, watchlist: list[dict]) -> Optional[str]:
    for w in watchlist:
        if w["code"] == code:
            return w.get("sector")
    return None


def generate_swaps(
    account: Account,
    quality: dict[str, QualityInfo],
    buy_signals: list[BuySignal],
    watchlist: Optional[list[dict]] = None,
    max_pairs: int = 5,
) -> list[SwapPair]:
    """生成换股配对清单。

    卖出方：持仓中的弱质标的；买入方：自选池中「强烈推荐/伺机买入」候选。
    """
    watchlist = watchlist or []
    # 买入候选：非持仓代码、投票分达标（伺机买入线以上）且质量过关。
    # 注意：换股是「先卖劣质腾资金 → 再买优质」，故不看当前仓位门控（卖出后即释放额度）。
    held_codes = {p.code for p in account.positions}
    vote_watch = config.load_config()["strategy_params"]["vote_watch"]
    candidates = [
        s for s in buy_signals
        if s.code not in held_codes
        and s.score >= vote_watch
        and s.rating in (Rating.EXCELLENT, Rating.QUALIFIED)
    ]
    candidates.sort(key=lambda s: -s.score)

    # 卖出方：弱质持仓
    weak_positions = []
    for p in account.positions:
        q = quality.get(p.code)
        weak, reason = _is_weak(q.rating if q else None, p.pnl_ratio)
        if weak:
            weak_positions.append((p, q, reason))

    # 按紧急度排序（黑名单 > 预警 > 深套）
    def _rank(item):
        p, q, _ = item
        r = q.rating if q else None
        if r == Rating.BLACKLIST:
            return 0
        if r == Rating.WARNING:
            return 1
        return 2
    weak_positions.sort(key=_rank)

    pairs: list[SwapPair] = []
    used = set()

    for p, q, from_reason in weak_positions:
        if len(pairs) >= max_pairs:
            break
        sector = p.sector
        # 1) 同板块弱换强
        chosen = None
        match_type = ""
        if sector:
            for c in candidates:
                if c.code in used:
                    continue
                cs = _sector_of(c.code, c.name, watchlist)
                if cs == sector:
                    chosen, match_type = c, "同板块弱换强"
                    break
        # 2) 垃圾换核心 / 高估值换低估（无同板块候选时）
        if chosen is None:
            for c in candidates:
                if c.code in used:
                    continue
                cq_pool = c.pool
                if cq_pool in (Pool.CORE, Pool.HOT):
                    chosen, match_type = c, "垃圾换核心"
                    break
        if chosen is None:
            for c in candidates:
                if c.code not in used:
                    chosen, match_type = c, "高估值换低估"
                    break
        if chosen is None:
            continue
        used.add(chosen.code)
        pairs.append(
            SwapPair(
                from_code=p.code,
                from_name=p.name,
                from_reason=from_reason,
                to_code=chosen.code,
                to_name=chosen.name,
                to_reason=f"{RATING_LABEL[chosen.rating] if chosen.rating else ''} · 投票分 {chosen.score}",
                match_type=match_type,
                to_score=chosen.score,
            )
        )

    return pairs
