"""技术指标库（纯 Python，输入为按时间升序的收盘价序列）。"""
from __future__ import annotations

import math
from typing import Optional


def sma(values: list[float], n: int) -> list[Optional[float]]:
    """简单移动平均，前 n-1 个为 None。"""
    out: list[Optional[float]] = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    s = sum(values[:n])
    out[n - 1] = s / n
    for i in range(n, len(values)):
        s += values[i] - values[i - n]
        out[i] = s / n
    return out


def ema(values: list[float], n: int) -> list[float]:
    """指数移动平均。"""
    if not values:
        return []
    k = 2 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _std(window: list[float]) -> float:
    m = sum(window) / len(window)
    var = sum((x - m) ** 2 for x in window) / len(window)
    return math.sqrt(var)


def bollinger(values: list[float], n: int = 20, k: float = 2.0) -> dict:
    """布林带：返回 {mid, upper, lower}（含 None 前导）。"""
    mid = sma(values, n)
    upper: list[Optional[float]] = [None] * len(values)
    lower: list[Optional[float]] = [None] * len(values)
    for i in range(n - 1, len(values)):
        s = _std(values[i - n + 1: i + 1])
        upper[i] = mid[i] + k * s
        lower[i] = mid[i] - k * s
    return {"mid": mid, "upper": upper, "lower": lower}


def rsi(values: list[float], n: int = 14) -> Optional[float]:
    """RSI（Wilder 平滑），返回最新值。"""
    s = rsi_series(values, n)
    return s[-1] if s and s[-1] is not None else None


def rsi_series(values: list[float], n: int = 14) -> list[Optional[float]]:
    """RSI 全序列（Wilder 平滑），前 n 个为 None。"""
    out: list[Optional[float]] = [None] * len(values)
    if len(values) < n + 1:
        return out
    gains = [0.0] * (len(values) - 1)
    losses = [0.0] * (len(values) - 1)
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains[i - 1] = max(d, 0.0)
        losses[i - 1] = max(-d, 0.0)

    def _r(ag: float, al: float) -> float:
        return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)

    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    out[n] = _r(avg_gain, avg_loss)
    for i in range(n, len(gains)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n
        out[i + 1] = _r(avg_gain, avg_loss)
    return out


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """MACD：返回 {dif, dea, hist} 序列。"""
    ef = ema(values, fast)
    es = ema(values, slow)
    dif = [ef[i] - es[i] for i in range(len(values))]
    dea = ema(dif, signal)
    hist = [(dif[i] - dea[i]) * 2 for i in range(len(values))]
    return {"dif": dif, "dea": dea, "hist": hist}


def macd_bearish_divergence(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> bool:
    """顶背离：价格创近期新高，但 DIF 未同步创新高。"""
    if len(values) < 40:
        return False
    m = macd(values, fast, slow, signal)
    dif = m["dif"]
    recent_high = max(values[-5:])
    prior_high = max(values[-40:-5])
    if recent_high >= prior_high:
        dif_recent = max(dif[-5:])
        dif_prior = max(dif[-40:-5])
        return dif_recent < dif_prior
    return False


def momentum_return(values: list[float], n: int = 60) -> Optional[float]:
    """N 日收益率（%）。"""
    if len(values) < n + 1:
        return None
    base = values[-n - 1]
    if base == 0:
        return None
    return round((values[-1] / base - 1) * 100, 2)


def last_valid(seq: list[Optional[float]]) -> Optional[float]:
    for v in reversed(seq):
        if v is not None:
            return v
    return None


def kdj(highs: list[float], lows: list[float], closes: list[float], n: int = 9) -> tuple[list, list, list]:
    """KDJ 指标，返回 (K, D, J) 序列（前 n-1 为 None）。"""
    k_series: list[Optional[float]] = [None] * len(closes)
    d_series: list[Optional[float]] = [None] * len(closes)
    j_series: list[Optional[float]] = [None] * len(closes)
    prev_k, prev_d = 50.0, 50.0
    for i in range(len(closes)):
        if i < n - 1:
            continue
        hh = max(highs[i - n + 1:i + 1])
        ll = min(lows[i - n + 1:i + 1])
        rsv = (closes[i] - ll) / (hh - ll) * 100 if hh > ll else 50.0
        prev_k = (rsv + 2 * prev_k) / 3
        prev_d = (prev_k + 2 * prev_d) / 3
        k_series[i] = prev_k
        d_series[i] = prev_d
        j_series[i] = 3 * prev_k - 2 * prev_d
    return k_series, d_series, j_series


def cci(highs: list[float], lows: list[float], closes: list[float], n: int = 14) -> list[Optional[float]]:
    """CCI 顺势指标序列。"""
    tp = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(len(closes))]
    out: list[Optional[float]] = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        window = tp[i - n + 1:i + 1]
        ma = sum(window) / n
        md = sum(abs(x - ma) for x in window) / n
        out[i] = (tp[i] - ma) / (0.015 * md) if md != 0 else 0.0
    return out


def atr(highs: list[float], lows: list[float], closes: list[float], n: int = 14) -> list[Optional[float]]:
    """ATR（平均真实波幅，Wilder 平滑）序列。"""
    tr = [0.0] * len(closes)
    for i in range(len(closes)):
        if i == 0:
            tr[i] = highs[i] - lows[i]
        else:
            tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    out: list[Optional[float]] = [None] * len(closes)
    if len(closes) < n:
        return out
    out[n - 1] = sum(tr[:n]) / n
    for i in range(n, len(tr)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def obv(closes: list[float], volumes: list[float]) -> list[float]:
    """OBV 能量潮序列。"""
    out = [0.0] * len(closes)
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            out[i] = out[i - 1] + (volumes[i] or 0)
        elif closes[i] < closes[i - 1]:
            out[i] = out[i - 1] - (volumes[i] or 0)
        else:
            out[i] = out[i - 1]
    return out
