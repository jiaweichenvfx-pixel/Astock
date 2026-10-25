"""模块4：质量筛选器（四档评级 × 五池正交）。"""
from __future__ import annotations

from typing import Optional

from .. import config
from ..schemas import Pool, QualityInfo, Rating


def is_diamond_bottom(
    pb: Optional[float],
    pe_percentile: Optional[float],
    consecutive_profit_years: int = 0,
) -> bool:
    """钻石底：破净 且 PE 历史分位 <10% 且 近3年连续盈利。"""
    db = config.get_quality()["diamond_bottom"]
    pb_ok = pb is not None and pb < 1.0
    pe_ok = pe_percentile is not None and pe_percentile < db["pe_percentile_lt"]
    profit_ok = consecutive_profit_years >= db["consecutive_profit_years"]
    return pb_ok and pe_ok and profit_ok


def classify_rating(
    roe_ttm: Optional[float],
    growth_yoy: Optional[float],
    is_st: bool = False,
    consecutive_loss_years: int = 0,
    pb: Optional[float] = None,
    pe_percentile: Optional[float] = None,
) -> tuple[Rating, list[str]]:
    """四档评级 + 触发原因。"""
    q = config.get_quality()
    reasons: list[str] = []

    # 黑名单：ST 或 连续亏损（ST 触发钻石底可豁免降为预警）
    if is_st or consecutive_loss_years >= 2:
        if is_st and is_diamond_bottom(pb, pe_percentile, 3):
            return Rating.WARNING, ["ST 但触发钻石底豁免（破净 + PE分位<10% + 连续盈利），降为预警"]
        if is_st:
            reasons.append("ST/*ST")
        if consecutive_loss_years >= 2:
            reasons.append(f"连续亏损 {consecutive_loss_years} 年")
        return Rating.BLACKLIST, reasons

    if roe_ttm is None:
        return Rating.QUALIFIED, ["财务数据缺失，默认合格"]

    # 卓越：ROE>15% 且 扣非净利同比>10%
    if roe_ttm > q["roe_excellent"] and growth_yoy is not None and growth_yoy > q["growth_excellent"]:
        return Rating.EXCELLENT, [f"ROE {roe_ttm}% > 15% 且增速 {growth_yoy}%"]

    # 预警：增速 < -10% 或 亏损（ROE <= 0 视为亏损）
    if (growth_yoy is not None and growth_yoy < q["growth_warning"]) or roe_ttm <= 0:
        if growth_yoy is not None and growth_yoy < q["growth_warning"]:
            reasons.append(f"扣非净利增速 {growth_yoy}% < -10%")
        if roe_ttm <= 0:
            reasons.append(f"ROE {roe_ttm}% <= 0（亏损）")
        return Rating.WARNING, reasons

    # 合格：非ST 且 盈利 且 增速 >= -10%
    return Rating.QUALIFIED, [f"ROE {roe_ttm}%，增速 {growth_yoy}%"]


def classify_pool(
    rating: Rating,
    rps: Optional[float] = None,
    sector_heat: bool = False,
    pe_percentile: Optional[float] = None,
    pb: Optional[float] = None,
) -> Pool:
    """五池分类（正交于评级）。"""
    if rating == Rating.BLACKLIST:
        return Pool.BLACKLIST
    if rating == Rating.EXCELLENT:
        return Pool.CORE
    # 合格/预警：按风格归类
    if (rps is not None and rps >= 90) or sector_heat:
        return Pool.HOT
    if (pe_percentile is not None and pe_percentile < 30) or (pb is not None and pb < 1.0):
        return Pool.VALUE
    return Pool.WATCH


def classify_stock(
    code: str,
    name: str,
    roe_ttm: Optional[float] = None,
    growth_yoy: Optional[float] = None,
    is_st: bool = False,
    market_cap: Optional[float] = None,
    pe_percentile: Optional[float] = None,
    pb: Optional[float] = None,
    audit_opinion: Optional[str] = None,
    consecutive_loss_years: int = 0,
    rps: Optional[float] = None,
    sector_heat: bool = False,
) -> QualityInfo:
    rating, reasons = classify_rating(
        roe_ttm, growth_yoy, is_st, consecutive_loss_years, pb, pe_percentile
    )
    pool = classify_pool(rating, rps, sector_heat, pe_percentile, pb)
    return QualityInfo(
        code=code,
        name=name,
        rating=rating,
        pool=pool,
        roe_ttm=roe_ttm,
        growth_yoy=growth_yoy,
        is_st=is_st,
        market_cap=market_cap,
        pe_percentile=pe_percentile,
        pb=pb,
        audit_opinion=audit_opinion,
        consecutive_loss_years=consecutive_loss_years,
        reasons=reasons,
    )
