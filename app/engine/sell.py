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


def check_hard_stop(pos: Position, mode: Mode, ma20: Optional[float] = None) -> Optional[_Candidate]:
    """硬止损：跌破止损线 + 趋势确实走坏。

    两个前提，缺一不可：
      1) 价格跌破 成本 ×(1+止损%)；
      2) 价格在 20 日均线下方（趋势已破坏）。

    只看第 1 条会出错：一只从 −16% 反弹到 −7.6% 的票，价格虽然还在成本线下方，
    但当天可能是涨停、趋势向上——那不叫"止损"，那是"卖在起涨点"。
    深套（比止损线再深 deep_underwater_margin）的持仓同样不机械割肉。
    """
    m = config.get_mode(mode)
    stop_pct = m["stop_loss"]  # 负数，如 -3
    deep_margin = config.get_exit_rules()["stop_loss"]["deep_underwater_margin"]
    stop_price = round(pos.cost_price * (1 + stop_pct / 100), 2)
    cur = pos.current_price if pos.current_price is not None else pos.cost_price

    # 已深套：比止损线再深 deep_margin 个百分点，改由时间/基本面止损主导
    if pos.pnl_ratio < (stop_pct - deep_margin):
        return None
    # 趋势未破（仍在 20 日线上方）：不按成本线机械卖出
    if ma20 and cur > ma20:
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


def check_profit_protection(pos: Position, rsi: Optional[float], j: Optional[float],
                            divergence: bool) -> Optional[_Candidate]:
    """盈利保护：有浮盈时，超买信号优先用来「锁利润」。

    实测（50 只 × 640 个阶段高点，之后 60 日内跌超 20%）：
      RSI>75 触发时平均在顶部后 3 天，捕获率 122%、少跌 38.5pp
      MACD 顶背离平均晚 7 天，捕获率 110%、少跌 33.2pp
    远好于回撤类（-10% 捕获 78%、-20% 只有 52%）——回撤类要等跌下来才动。
    """
    if pos.pnl_ratio is None or pos.pnl_ratio < 20:
        return None
    cur = pos.current_price if pos.current_price is not None else pos.cost_price
    overbought = (rsi is not None and rsi >= 75) or (j is not None and j >= 100)
    if overbought:
        why = f"RSI {rsi:.0f}" if (rsi is not None and rsi >= 75) else f"J 值 {j:.0f}"
        return _Candidate(
            SellMechanism.TECHNICAL, 72,
            f"浮盈 {pos.pnl_ratio:+.0f}% 且 {why} 超买，先锁一半利润",
            "limit_order", cur,
        )
    if divergence and pos.pnl_ratio >= 20:
        return _Candidate(
            SellMechanism.TECHNICAL, 64,
            f"浮盈 {pos.pnl_ratio:+.0f}% 且 MACD 顶背离，减仓锁利润",
            "limit_order", cur,
        )
    return None


def check_drawdown_stop(pos: Position, dd_60d: Optional[float]) -> Optional[_Candidate]:
    """回撤止损（跟踪止损）：从 60 日最高价回撤过大就减。

    成本锚定的止损在深套账户上完全失效（成本线离现价太远，永远不触发），
    而回撤止损只看「离自己最高点跌了多少」，对任何成本都成立。

    - 回撤 ≥30%：趋势崩塌，橙色提示
    - 有浮盈且回撤 ≥15%：利润回吐，黄色提示
    """
    if dd_60d is None:
        return None
    rules = config.get_exit_rules().get("drawdown_stop") or {}
    crash = rules.get("crash_pct", -30.0)
    give_back = rules.get("give_back_pct", -15.0)
    profit_gate = rules.get("profit_gate", 10.0)

    cur = pos.current_price if pos.current_price is not None else pos.cost_price
    if dd_60d <= crash:
        return _Candidate(
            SellMechanism.TECHNICAL, 68,
            f"从 60 日最高价回撤 {abs(dd_60d):.0f}%，趋势崩塌",
            "limit_order", cur,
        )
    if pos.pnl_ratio >= profit_gate and dd_60d <= give_back:
        return _Candidate(
            SellMechanism.TECHNICAL, 55,
            f"浮盈从最高 {pos.pnl_ratio - dd_60d:+.0f}% 回吐到 {pos.pnl_ratio:+.0f}%"
            f"（回撤 {abs(dd_60d):.0f}%），锁住利润",
            "limit_order", cur,
        )
    return None


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
    n = trend.get("consecutive") or 0
    if n >= 2:
        why = f"连续 {n} 个季度利润下滑"
    elif g < 0 and n <= 1:
        why = "利润由正转负" if g > -30 else "利润大幅下滑"
    else:
        why = "利润走弱"
    return _Candidate(
        SellMechanism.FUNDAMENTAL,
        58,
        f"{why}（扣非同比 {g:+.1f}%），比硬性亏损提前预警",
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

        c = check_hard_stop(pos, mode, f.get("ma20"))
        if c:
            candidates.append(c)

        c = check_profit_protection(pos, f.get("rsi"), f.get("j"), bool(f.get("macd_divergence")))
        if c:
            candidates.append(c)

        c = check_drawdown_stop(pos, f.get("dd_60d"))
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

        # 今日涨停/大涨：不市价砸盘，改成限价（挂更高价等买盘）
        surge_note = ""
        gain = f.get("gain_pct")
        if gain is not None and gain >= 9.0:
            for c in candidates:
                c.execution = "limit_order"
            surge_note = f"（今日已涨 {gain:.1f}%，不市价卖，挂限价等买盘）"

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
                reason="；".join(reason_parts) + season_note + surge_note,
                suggested_price=suggested,
                execution=primary.execution,
                flight_defense=flight,
                flight_reasons=flight_reasons if flight else [],
            )
        )

    signals.sort(key=lambda s: -s.severity_score)
    return signals
