"""市场环境打分（0-10）：成交额 vs 30日均量 + 涨跌家数比 + 指数20日线。"""
from __future__ import annotations

from typing import Optional

from .. import config
from ..schemas import MarketEnv, Temperature


def score_market_env(
    turnover_ratio: Optional[float] = None,
    adv_decl_ratio: Optional[float] = None,
    index_above_ma20: Optional[bool] = None,
) -> MarketEnv:
    """计算市场环境 0-10 分。

    - 成交额 / 30日均量：>=1.2 +4，>=0.8 +2，>=0.6 +1
    - 涨跌家数比：>=1.5 +4，>=1.0 +2
    - 指数站上20日线：+2
    """
    cfg = config.get_market_env()
    score = 0.0
    detail = {}

    if turnover_ratio is not None:
        if turnover_ratio >= cfg["turnover_ratio"]["hot"]:
            score += 4
            detail["turnover"] = "放量"
        elif turnover_ratio >= cfg["turnover_ratio"]["mild"]:
            score += 2
            detail["turnover"] = "温和"
        elif turnover_ratio >= 0.6:
            score += 1
            detail["turnover"] = "缩量"

    if adv_decl_ratio is not None:
        if adv_decl_ratio >= cfg["adv_decl_ratio"]["hot"]:
            score += 4
            detail["adv_decl"] = "普涨"
        elif adv_decl_ratio >= cfg["adv_decl_ratio"]["mild"]:
            score += 2
            detail["adv_decl"] = "涨多跌少"
        else:
            detail["adv_decl"] = "跌多涨少"

    if index_above_ma20:
        score += 2
        detail["index"] = "站上20日线"
    elif index_above_ma20 is False:
        detail["index"] = "跌破20日线"

    score = round(min(10.0, max(0.0, score)), 1)
    if score >= cfg["hot_min"]:
        temperature = Temperature.HOT
    elif score >= cfg["mild_min"]:
        temperature = Temperature.MILD
    else:
        temperature = Temperature.COLD

    return MarketEnv(
        score=score,
        temperature=temperature,
        turnover_ratio=turnover_ratio,
        adv_decl_ratio=adv_decl_ratio,
        index_above_ma20=index_above_ma20,
        detail=detail,
    )
