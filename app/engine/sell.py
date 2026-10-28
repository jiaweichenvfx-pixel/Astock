"""模块3：卖出优先处理器（四大机制 + 紧急度打分 + 卖飞防御）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .. import config
from ..schemas import (
    Account,
    Mode,
    Position,
    QualityInfo,
    Rating,
    SellLevel,
    SellMechanism,
    SellSignal,
)

_LEVEL_ORDER = [SellLevel.HOLD, SellLevel.GREEN, SellLevel.YELLOW, SellLevel.ORANGE, SellLevel.RED]


@dataclass
class _Candidate:
    mechanism: SellMechanism
    base_score: int
    reason: str
    execution: str = "market_order"
    suggested_price: Optional[float] = None


def _downgrade(level: SellLevel, steps: int = 1) -> SellLevel:
    idx = _LEVEL_ORDER.index(level)
    return _LEVEL_ORDER[max(0, idx - steps)]


def level_from_score(score: int) -> SellLevel:
    if score >= 80:
        return SellLevel.RED
    if score >= 60:
        return SellLevel.ORANGE
    if score >= 40:
        return SellLevel.YELLOW
    if score >= 20:
        return SellLevel.GREEN
    return SellLevel.HOLD


def check_hard_stop(pos: Position, mode: Mode) -> Optional[_Candidate]:
    """硬止损：价格跌破止损线（固定百分比，成本锚定）。深套持仓不机械割肉。"""
    m = config.get_mode(mode)
    stop_pct = m["stop_loss"]  # 负数，如 -3
    deep_margin = config.get_exit_rules()["stop_loss"]["deep_underwater_margin"]
    stop_price = round(pos.cost_price * (1 + stop_pct / 100), 2)
    cur = pos.current_price if pos.current_price is not None else pos.cost_price

    # 已深套：比止损线再深 deep_margin 个百分点，改由时间/基本面止损主导
    if pos.pnl_ratio < (stop_pct - deep_margin):
        return None
    if cur <= stop_price:
        return _Candidate(
            SellMechanism.HARD_STOP,
            80,
            f"价格 {cur} 跌破止损线 {stop_price}（{stop_pct}%）",
            "market_order",
            cur,
        )
    return None


def check_time_stop(
    pos: Position, mode: Mode, days_held: Optional[int], last_high: Optional[float]
) -> Optional[_Candidate]:
    """时间止损：持仓 N 天未创新高且收益 < X%。"""
    m = config.get_mode(mode)
    n_days = m["time_stop_days"]
    x_pct = m.get("time_stop_profit", 1.0)
    if days_held is None or days_held < n_days:
        return None
    if pos.pnl_ratio >= x_pct:
        return None  # 收益达标，不触发
    cur = pos.current_price if pos.current_price is not None else pos.cost_price
    if last_high is not None and cur >= last_high:
        return None  # 正处高位/创新高，趋势仍在
    return _Candidate(
        SellMechanism.TIME_STOP,
        65,
        f"持仓 {days_held} 天未创新高，收益 {pos.pnl_ratio}% < {x_pct}%",
        "limit_order",
        cur,
    )


def check_fundamental_stop(pos: Position, q: Optional[QualityInfo]) -> Optional[_Candidate]:
    """基本面止损：ST/连续亏损（黑名单）、增速崩塌、ROE 下滑。"""
    rules = config.get_exit_rules()["fundamental_stop"]
    if q is None:
        return None
    if q.rating == Rating.BLACKLIST:
        return _Candidate(
            SellMechanism.FUNDAMENTAL,
            75,
            f"基本面恶化：{'; '.join(q.reasons)}，强制卖出",
            "market_order",
            pos.current_price,
        )
    if q.growth_yoy is not None and q.growth_yoy < rules["growth_crash"]:
        return _Candidate(
            SellMechanism.FUNDAMENTAL,
            75,
            f"扣非净利同比 {q.growth_yoy}% < {rules['growth_crash']}%，增速崩塌",
            "market_order",
            pos.current_price,
        )
    if q.rating == Rating.WARNING and q.roe_ttm is not None and q.roe_ttm <= 0:
        return _Candidate(
            SellMechanism.FUNDAMENTAL,
            60,
            f"评级预警，ROE {q.roe_ttm}% <= 0（亏损）",
            "limit_order",
            pos.current_price,
        )
    return None


def check_fundamental_trend(pos: Position, trend: Optional[dict]) -> Optional[_Candidate]:
    """基本面趋势预警：连续多期恶化且已转负 → 黄色（3 日内处理）。

    比「扣非 < −30%」的硬阈值提前 1~2 个季度，避免等到崩塌才反应。
    """
    if not trend or trend.get("trend") != "deteriorating":
        return None
    g = trend.get("latest_growth")
    if g is None:
        return None
    return _Candidate(
        SellMechanism.FUNDAMENTAL,
        58,
        f"基本面连续 {trend.get('consecutive', 0)} 期恶化（扣非同比 {g:+.1f}%），提前预警",
        "limit_order",
        pos.current_price,
    )


def check_technical_exit(
    pos: Position,
    rsi: Optional[float] = None,
    macd_divergence: bool = False,
    volume_ratio: Optional[float] = None,
    gain_pct: Optional[float] = None,
) -> list[_Candidate]:
    """技术卖点：顶背离 / RSI 超买 / 放量滞涨（可多信号并存）。"""
    rules = config.get_exit_rules()["technical_exit"]
    out: list[_Candidate] = []
    cur = pos.current_price if pos.current_price is not None else pos.cost_price

    if macd_divergence:
        out.append(_Candidate(SellMechanism.TECHNICAL, 60, "顶背离：价创新高 MACD 未创新高", "limit_order", cur))
    if rsi is not None and rsi > rules["rsi_overbought"]:
        out.append(_Candidate(SellMechanism.TECHNICAL, 50, f"RSI {rsi:.1f} > {rules['rsi_overbought']} 超买", "limit_order", cur))
    if (
        volume_ratio is not None
        and volume_ratio > rules["volume_ratio"]
        and gain_pct is not None
        and gain_pct < rules["stall_gain_pct"]
    ):
        out.append(
            _Candidate(
                SellMechanism.TECHNICAL,
                45,
                f"放量滞涨：量比 {volume_ratio:.1f} 但涨幅仅 {gain_pct}%",
                "limit_order",
                cur,
            )
        )
    return out


def check_flight_defense(
    sector_rps: Optional[float], net_inflow_days: Optional[int], annual_breakout: bool
) -> tuple[bool, list[str]]:
    """卖飞防御（AND）：板块RPS>90 且 连续3日主力净流入 且 放量突破年线。"""
    cfg = config.get_exit_rules()["sell_flight"]
    reasons: list[str] = []
    ok1 = sector_rps is not None and sector_rps > cfg["sector_rps_threshold"]
    ok2 = net_inflow_days is not None and net_inflow_days >= cfg["net_inflow_days"]
    ok3 = annual_breakout
    if ok1:
        reasons.append(f"RPS60 {sector_rps:.0f}（60日涨幅分位）")
    if ok2:
        reasons.append(f"连续 {net_inflow_days} 日主力净流入")
    if ok3:
        reasons.append("刚放量突破年线")
    triggered = ok1 and ok2 and ok3
    return triggered, reasons


def scan_positions(
    account: Account,
    mode: Mode,
    quality: dict[str, QualityInfo],
    facts: Optional[dict[str, dict]] = None,
    trends: Optional[dict[str, dict]] = None,
    earnings_season: Optional[bool] = None,
) -> list[SellSignal]:
    """扫描所有持仓，输出卖出信号清单（已按紧急度降序）。

    earnings_season=True 时技术类信号降权 −10 分：财报跳空会造成假跌破/假突破。
    """
    facts = facts or {}
    trends = trends or {}
    if earnings_season is None:
        try:
            from ..data import adapter as _ad
            earnings_season = _ad.in_earnings_season()
        except Exception:
            earnings_season = False
    severity_cfg = config.get_exit_rules()["severity"]
    signals: list[SellSignal] = []

    for pos in account.positions:
        f = facts.get(pos.code, {})
        candidates: list[_Candidate] = []

        c = check_hard_stop(pos, mode)
        if c:
            candidates.append(c)

        c = check_time_stop(pos, mode, f.get("days_held"), f.get("last_high"))
        if c:
            candidates.append(c)

        c = check_fundamental_stop(pos, quality.get(pos.code))
        if c:
            candidates.append(c)

        c = check_fundamental_trend(pos, trends.get(pos.code))
        if c:
            candidates.append(c)

        candidates.extend(
            check_technical_exit(
                pos,
                rsi=f.get("rsi"),
                macd_divergence=bool(f.get("macd_divergence")),
                volume_ratio=f.get("volume_ratio"),
                gain_pct=f.get("gain_pct"),
            )
        )

        if not candidates:
            continue

        # 主机制取最严重者（base_score 最高）
        candidates.sort(key=lambda c: -c.base_score)
        primary = candidates[0]

        # 紧急度打分
        base = primary.base_score
        bonus = min(severity_cfg["multi_signal_cap"], severity_cfg["multi_signal_bonus"] * (len(candidates) - 1))
        score = min(100, base + bonus)

        # 财报季：技术类卖点降权，避免被财报跳空制造的假跌破骗出场
        season_note = ""
        if earnings_season and primary.mechanism == SellMechanism.TECHNICAL:
            score = max(0, score - 10)
            season_note = "（财报季：技术信号降权，财报跳空常造成假跌破）"

        # 卖飞防御：满足一条条件 −10 分（最多 −30）；三条全中（AND）再额外降一级。
        # 但最终「最多比基础级别低一级」，避免一条卖飞防御把必卖信号降成持有。
        flight, flight_reasons = check_flight_defense(
            f.get("sector_rps"), f.get("net_inflow_days"), bool(f.get("annual_breakout"))
        )
        base_level = level_from_score(score)
        if flight_reasons:
            penalty = min(30, severity_cfg.get("flight_penalty_per_cond", 10) * len(flight_reasons))
            score = max(0, score - penalty)

        level = level_from_score(score)
        if flight:
            level = _downgrade(level)
        # 无论扣多少分，最多只降一级
        if _LEVEL_ORDER.index(base_level) - _LEVEL_ORDER.index(level) > 1:
            level = _downgrade(base_level)

        # 深套提示：给出具体反弹减仓目标价，而非笼统「反弹减仓」
        stop_pct = config.get_mode(mode)["stop_loss"]
        deep_margin = config.get_exit_rules()["stop_loss"]["deep_underwater_margin"]
        rebound_pct = config.get_deleverage()["rebound_pct"]
        deep = pos.pnl_ratio < (stop_pct - deep_margin)
        reason_parts = [c.reason for c in candidates]
        suggested = primary.suggested_price
        if deep and all(c.mechanism != SellMechanism.HARD_STOP for c in candidates):
            cur = pos.current_price if pos.current_price is not None else pos.cost_price
            rebound_price = round(cur * (1 + rebound_pct / 100), 2)
            reason_parts.append(f"深套：反弹至 {rebound_price} 元（+{rebound_pct}%）再减仓，不追低割肉")
            suggested = rebound_price

        if flight_reasons and not flight:
            reason_parts.append("卖飞风险：" + "、".join(flight_reasons))

        signals.append(
            SellSignal(
                code=pos.code,
                name=pos.name,
                mechanism=primary.mechanism,
                severity_score=score,
                level=level,
                reason="；".join(reason_parts) + season_note,
                suggested_price=suggested,
                execution=primary.execution,
                flight_defense=flight,
                flight_reasons=flight_reasons if flight else [],
            )
        )

    signals.sort(key=lambda s: -s.severity_score)
    return signals
