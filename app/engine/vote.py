"""实时投票引擎：5 策略加权投票（买点推荐用）+ 技术指标打包。

注意：这里的 5 个投票信号是「实时买点」用的简化版（基于当前技术指标快照），
与 strategies.py 的「战法回溯」信号（基于完整历史序列）是两套不同用途。
"""
from __future__ import annotations

from typing import Optional

from .. import config
from ..schemas import Mode, Position
from . import indicators as ind


# ── 技术指标打包（实时）─────────────────────────────────────────
def compute_tech(closes: list[float]) -> dict:
    """由收盘价序列计算全套技术指标（最新值）。"""
    p = config.load_config()["strategy_params"]
    ma5 = ind.last_valid(ind.sma(closes, p["ma_short"]))
    ma20 = ind.last_valid(ind.sma(closes, p["ma_long"]))
    ma60 = ind.last_valid(ind.sma(closes, 60))
    ma250 = ind.last_valid(ind.sma(closes, p["ma_annual"]))
    b = ind.bollinger(closes, p["bollinger_n"], p["bollinger_k"])
    return {
        "price": closes[-1] if closes else None,
        "ma5": ma5,
        "ma20": ma20,
        "ma60": ma60,
        "ma250": ma250,
        "boll_upper": ind.last_valid(b["upper"]),
        "boll_mid": ind.last_valid(b["mid"]),
        "boll_lower": ind.last_valid(b["lower"]),
        "rsi": ind.rsi(closes, p["rsi_n"]),
        "macd_hist": ind.macd(closes)["hist"][-1] if closes else None,
        "macd_divergence": ind.macd_bearish_divergence(closes),
        "momentum_60": ind.momentum_return(closes, p["momentum_days"]),
    }


# ── 5 个投票信号 ────────────────────────────────────────────────
def value_signal(tech: dict, roe: Optional[float] = None, pe: Optional[float] = None) -> int:
    """长线价值：年线之上 + (ROE 高 或 PE 低)。跌破年线则 -1。"""
    price, ma250, ma60 = tech.get("price"), tech.get("ma250"), tech.get("ma60")
    if price is None or ma250 is None:
        return 0
    if price <= ma250:
        return -1
    if (roe is not None and roe > 15) or (pe is not None and 0 < pe < 20):
        return 1
    if ma60 is not None and ma60 > ma250:
        return 1
    return 0


def ma_signal(tech: dict) -> int:
    """双均线：MA5 上穿/站上 MA20。"""
    ma5, ma20 = tech.get("ma5"), tech.get("ma20")
    if ma5 is None or ma20 is None:
        return 0
    return 1 if ma5 > ma20 else -1


def bollinger_signal(tech: dict) -> int:
    """布林带回归：触下轨超卖买，触上轨超买卖。"""
    price, upper, lower = tech.get("price"), tech.get("boll_upper"), tech.get("boll_lower")
    if price is None or upper is None or lower is None:
        return 0
    if price <= lower:
        return 1
    if price >= upper:
        return -1
    return 0


def momentum_signal(tech: dict) -> int:
    """RPS 动量：60 日强势买，弱势卖。"""
    p = config.load_config()["strategy_params"]
    m = tech.get("momentum_60")
    if m is None:
        return 0
    if m > p["momentum_strong"]:
        return 1
    if m < p["momentum_weak"]:
        return -1
    return 0


def grid_signal(position: Optional[Position], tech: dict) -> int:
    """网格做T（降本）：浮亏超阈值补仓，浮盈超阈值止盈。仅对已持仓有效。"""
    if position is None:
        return 0
    p = config.load_config()["strategy_params"]
    pnl = position.pnl_ratio
    if pnl <= p["grid_rebuy_pct"]:
        return 1
    if pnl >= p["grid_take_profit_pct"]:
        return -1
    return 0


def compute_all_signals(tech: dict, position: Optional[Position], roe=None, pe=None) -> dict[str, int]:
    return {
        "value": value_signal(tech, roe, pe),
        "ma": ma_signal(tech),
        "bollinger": bollinger_signal(tech),
        "momentum": momentum_signal(tech),
        "grid": grid_signal(position, tech),
    }


# ── 加权投票 ─────────────────────────────────────────────────────
def weighted_vote(signals: dict[str, int], mode: Mode) -> int:
    """按模式权重加权求和，归一化到 [-100, 100]。

    signals: {value: ±1/0, ma: ±1/0, bollinger: ±1/0, momentum: ±1/0, grid: ±1/0}
    """
    weights = config.get_strategy_weights(mode)
    total = sum(weights.values()) or 1
    score = sum(signals.get(k, 0) * weights.get(k, 0) for k in weights)
    return round(score / total * 100)


def buy_level(score: int) -> tuple[str, str]:
    """投票分 → 买点级别。"""
    p = config.load_config()["strategy_params"]
    if score >= p["vote_strong"]:
        return "strong", "🚀 强烈推荐"
    if score >= p["vote_watch"]:
        return "watch", "⭐ 伺机买入"
    return "hold", "观望"
