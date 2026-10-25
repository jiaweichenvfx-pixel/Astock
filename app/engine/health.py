"""模块1：账户健康度评分卡 + 模式判定。"""
from __future__ import annotations

from typing import Optional

from .. import config
from ..schemas import (
    Account,
    HealthScore,
    MarketEnv,
    PositionColor,
    Rating,
    SellMechanism,
)


def _match_rules(value: float, rules: list[dict]) -> float:
    """按规则表取分：支持 gt/gte/lt/lte 边界，顺序匹配第一个命中的。"""
    for rule in rules:
        if "gt" in rule and not (value > rule["gt"]):
            continue
        if "gte" in rule and not (value >= rule["gte"]):
            continue
        if "lt" in rule and not (value < rule["lt"]):
            continue
        if "lte" in rule and not (value <= rule["lte"]):
            continue
        return rule["score"]
    return 0.0


def score_financing_ratio(ratio: float) -> float:
    """融资比例 → 0-25 分。"""
    rules = config.get_scorecard()["financing_ratio"]
    return _match_rules(ratio, rules)


def score_pnl(ratio: float) -> float:
    """账户整体浮盈/浮亏 → 0-25 分。"""
    rules = config.get_scorecard()["pnl_ratio"]
    return _match_rules(ratio, rules)


def score_position_quality(colors: list[PositionColor]) -> float:
    """持仓质量：基础25分，按颜色扣分，扣到0止。"""
    deduct = config.get_scorecard()["position_color_deduction"]
    total = 25.0
    for c in colors:
        total -= deduct[c.value]
    return round(max(0.0, total), 1)


def score_concentration(n: int) -> float:
    """持仓集中度 → 0-15 分。"""
    sc = config.get_scorecard()["concentration"]
    for rule in sc["rules"]:
        if rule["min"] <= n <= rule["max"]:
            return rule["score"]
    return sc["default"]


def score_market_env_points(env_score: float) -> float:
    """市场环境 0-10 分 → 评分卡分值：火热10 / 温和7 / 寒冷3。"""
    if env_score >= 8:
        return 10.0
    if env_score >= 4:
        return 7.0
    return 3.0


def base_position_color(rating: Optional[Rating], pnl_ratio: float) -> PositionColor:
    """基础持仓颜色（浮亏程度 × 质量评级，不含卖出信号）。"""
    if rating == Rating.BLACKLIST or pnl_ratio < -25:
        return PositionColor.RED
    if pnl_ratio < -15:
        return PositionColor.ORANGE
    if rating == Rating.WARNING or pnl_ratio < -5:
        return PositionColor.YELLOW
    return PositionColor.GREEN


def refine_color(base: PositionColor, mechanisms: list[SellMechanism]) -> PositionColor:
    """根据卖出信号升级颜色：硬止损/基本面→红，时间/技术→橙。"""
    if base == PositionColor.RED:
        return PositionColor.RED
    if SellMechanism.HARD_STOP in mechanisms or SellMechanism.FUNDAMENTAL in mechanisms:
        return PositionColor.RED
    if SellMechanism.TIME_STOP in mechanisms or SellMechanism.TECHNICAL in mechanisms:
        return PositionColor.ORANGE
    return base


def compute_health(
    account: Account,
    market_env: MarketEnv,
    sell_signals_by_code: Optional[dict[str, list]] = None,
) -> HealthScore:
    """计算账户健康度总分 + 模式。

    account.positions 的 rating 字段需已由质量筛选器填充；
    sell_signals_by_code: {code: [SellSignal, ...]} 用于颜色升级（可选）。
    """
    w = config.get_scorecard()["weights"]
    financing_ratio = account.financing_ratio
    overall_pnl = account.overall_pnl_ratio

    fin_score = score_financing_ratio(financing_ratio)
    pnl_score = score_pnl(overall_pnl)

    mechanisms_by_code: dict[str, list[SellMechanism]] = {}
    if sell_signals_by_code:
        for code, sigs in sell_signals_by_code.items():
            mechanisms_by_code[code] = [s.mechanism for s in sigs]

    colors: dict[str, PositionColor] = {}
    blacklist_count = 0
    warning_count = 0
    for p in account.positions:
        base = base_position_color(p.rating, p.pnl_ratio)
        colors[p.code] = refine_color(base, mechanisms_by_code.get(p.code, []))
        if p.rating == Rating.BLACKLIST:
            blacklist_count += 1
        elif p.rating == Rating.WARNING:
            warning_count += 1

    quality_score = score_position_quality(list(colors.values()))
    concentration_score = score_concentration(len(account.positions))
    env_score = score_market_env_points(market_env.score)

    total = int(round(fin_score + pnl_score + quality_score + concentration_score + env_score))
    mode = config.mode_for_score(total)

    return HealthScore(
        mode=mode,
        total=total,
        financing_score=fin_score,
        pnl_score=pnl_score,
        quality_score=quality_score,
        concentration_score=concentration_score,
        market_env_score=env_score,
        financing_ratio=financing_ratio,
        overall_pnl_ratio=overall_pnl,
        position_colors=colors,
        blacklist_count=blacklist_count,
        warning_count=warning_count,
    )
