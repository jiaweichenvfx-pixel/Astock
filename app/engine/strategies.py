"""战法注册表 —— 唯一的战法定义文件。

⚠️ 加/改/删战法，只需改这一个文件：
  1. 在 STRATEGY_LABEL 加中文名
  2. 在 STRATEGIES 加 key（决定展示顺序）
  3. 在 STRATEGY_CATEGORY 加分类
  4. 在 _params 加默认参数（可选）
  5. 在 PARAM_GRIDS 加寻优网格（可选）
  6. 在 strategy_signal 加信号逻辑

其余模块（backtest.py 回测引擎、vote.py 实时投票）都从这里读取，不重复定义。
"""
from __future__ import annotations

from typing import Optional

from . import indicators as ind


# ── 战法清单 ─────────────────────────────────────────────────────
STRATEGY_LABEL = {
    "value": "长线持有",
    "ma": "双均线",
    "bollinger": "布林带",
    "momentum": "RPS动量",
    "grid": "网格做T",
    "macd": "MACD金叉",
    "rsi": "RSI超卖",
    "ma_align": "均线多头",
    "donchian": "唐奇安突破",
    "kdj": "KDJ金叉",
    "kdj_j": "J值负值反转",
    "kdj_jw": "J值周线共振",
    "cci": "CCI顺势",
    "combo": "低吸共振",
    "keltner": "ATR通道",
    "obv": "OBV量能",
    "ma_squeeze": "均线粘合突破",
}

STRATEGY_CATEGORY = {
    "value": "持有",
    "ma": "趋势",
    "bollinger": "震荡",
    "momentum": "趋势",
    "grid": "做T",
    "macd": "趋势",
    "rsi": "低吸",
    "ma_align": "趋势",
    "donchian": "突破",
    "kdj": "低吸",
    "kdj_j": "低吸",
    "kdj_jw": "低吸",
    "cci": "震荡",
    "combo": "低吸",
    "keltner": "突破",
    "obv": "趋势",
    "ma_squeeze": "突破",
}

STRATEGIES = [
    "value", "ma", "bollinger", "momentum", "grid", "macd", "rsi", "ma_align",
    "donchian", "kdj", "kdj_j", "kdj_jw", "cci", "combo", "keltner", "obv", "ma_squeeze",
]


# ── 参数默认值 + 寻优网格 ───────────────────────────────────────
def _params(name: str, params: Optional[dict]) -> dict:
    """各战法默认参数，可用 params 覆盖。"""
    defaults = {
        "ma": {"short": 5, "long": 20},
        "bollinger": {"n": 20, "k": 2.0},
        "momentum": {"days": 60, "strong": 20, "weak": -20},
        "grid": {"dev": 3},
        "macd": {"fast": 12, "slow": 26, "signal": 9},
        "rsi": {"oversold": 30, "overbought": 70},
        "kdj": {"n": 9},
        "kdj_j": {"n": 9, "thresh": -10, "exit": 90},
        "kdj_jw": {"n": 9, "thresh": -10, "exit": 90, "week_thresh": 0},
        "cci": {"n": 14},
        "donchian": {"entry": 20, "exit": 10},
        "keltner": {"n": 20, "k": 2.0},
        "ma_squeeze": {"threshold": 2.0},
        "combo": {"rsi_low": 35, "rsi_high": 65},
    }.get(name, {})
    if params:
        defaults = {**defaults, **params}
    return defaults


PARAM_GRIDS = {
    "ma": [{"short": 5, "long": 20}, {"short": 10, "long": 30}, {"short": 5, "long": 30}, {"short": 20, "long": 60}],
    "bollinger": [{"n": 20, "k": 2.0}, {"n": 30, "k": 2.0}, {"n": 20, "k": 2.5}, {"n": 50, "k": 2.0}],
    "momentum": [{"days": 60, "strong": 20, "weak": -20}, {"days": 90, "strong": 15, "weak": -15},
                 {"days": 60, "strong": 15, "weak": -15}, {"days": 120, "strong": 20, "weak": -20}],
    "grid": [{"dev": 2}, {"dev": 3}, {"dev": 5}],
    "macd": [{"fast": 12, "slow": 26, "signal": 9}, {"fast": 8, "slow": 17, "signal": 9}, {"fast": 10, "slow": 20, "signal": 9}],
    "rsi": [{"oversold": 30, "overbought": 70}, {"oversold": 20, "overbought": 80}, {"oversold": 35, "overbought": 65}],
    "kdj": [{"n": 9}, {"n": 14}, {"n": 5}],
    "kdj_j": [{"thresh": -10, "exit": 90}, {"thresh": 0, "exit": 80}, {"thresh": -15, "exit": 100}],
    "kdj_jw": [{"thresh": -10, "exit": 90, "week_thresh": 0},
               {"thresh": -10, "exit": 90, "week_thresh": -10}],
    "cci": [{"n": 14}, {"n": 20}, {"n": 10}],
    "donchian": [{"entry": 20, "exit": 10}, {"entry": 30, "exit": 15}, {"entry": 10, "exit": 5}],
    "keltner": [{"n": 20, "k": 2.0}, {"n": 20, "k": 3.0}, {"n": 30, "k": 2.0}],
    "ma_squeeze": [{"threshold": 1.5}, {"threshold": 2.0}, {"threshold": 3.0}],
    "combo": [{"rsi_low": 30, "rsi_high": 70}, {"rsi_low": 35, "rsi_high": 65}, {"rsi_low": 40, "rsi_high": 60}],
}


# ── 信号生成 ─────────────────────────────────────────────────────
def ma_cross_signals(bars: list[dict], short: int = 5, long: int = 20) -> list[int]:
    """双均线信号：金叉 +1，死叉 -1，其余 0。"""
    closes = [b["close"] for b in bars]
    s = ind.sma(closes, short)
    l = ind.sma(closes, long)
    signals = [0] * len(closes)
    for i in range(1, len(closes)):
        if s[i] is None or l[i] is None or s[i - 1] is None or l[i - 1] is None:
            continue
        if s[i - 1] <= l[i - 1] and s[i] > l[i]:
            signals[i] = 1   # 金叉
        elif s[i - 1] >= l[i - 1] and s[i] < l[i]:
            signals[i] = -1  # 死叉
    return signals


def _weekly_j(bars: list[dict], n: int = 9) -> list[Optional[float]]:
    """每个日线位置对应的「上一完整周」的 J 值（严格用已完成周，杜绝未来函数）。"""
    import bisect
    from datetime import datetime

    groups: list[dict] = []
    for i, b in enumerate(bars):
        try:
            d = datetime.strptime(str(b["date"])[:10], "%Y-%m-%d").date()
        except (ValueError, KeyError):
            continue
        key = d.isocalendar()[:2]
        if not groups or groups[-1]["key"] != key:
            groups.append({"key": key, "end": i, "high": b["high"], "low": b["low"], "close": b["close"]})
        else:
            g = groups[-1]
            g["high"] = max(g["high"], b["high"])
            g["low"] = min(g["low"], b["low"])
            g["close"] = b["close"]
            g["end"] = i

    K = D = 50.0
    ends, vals = [], []
    for g in groups:
        rng = g["high"] - g["low"]
        rsv = 50.0 if rng <= 0 else (g["close"] - g["low"]) / rng * 100
        K = 2 / 3 * K + 1 / 3 * rsv
        D = 2 / 3 * D + 1 / 3 * K
        ends.append(g["end"])
        vals.append(3 * K - 2 * D)

    out: list[Optional[float]] = [None] * len(bars)
    for i in range(len(bars)):
        pos = bisect.bisect_left(ends, i) - 1     # 最后一个 end < i 的周
        out[i] = vals[pos] if pos >= 0 else None
    return out


def strategy_signal(name: str, bars: list[dict], params: Optional[dict] = None) -> list[int]:
    """生成某战法在 bars 上的信号序列（+1 买 / -1 卖 / 0 观望）。params 覆盖默认参数。"""
    p = _params(name, params)
    closes = [b["close"] for b in bars]
    highs = [b.get("high", b["close"]) for b in bars]
    lows = [b.get("low", b["close"]) for b in bars]
    n = len(closes)

    if name == "ma":
        return ma_cross_signals(bars, p["short"], p["long"])

    if name == "value":
        # 长线持有 = 买入并持有（不择时，作为基准战法）
        sig = [0] * n
        if n > 0:
            sig[0] = 1
        return sig

    if name == "bollinger":
        b = ind.bollinger(closes, p["n"], p["k"])
        sig = [0] * n
        for i in range(n):
            if b["lower"][i] is None:
                continue
            if closes[i] <= b["lower"][i]:
                sig[i] = 1
            elif closes[i] >= b["upper"][i]:
                sig[i] = -1
        return sig

    if name == "momentum":
        sig = [0] * n
        for i in range(p["days"], n):
            m = (closes[i] / closes[i - p["days"]] - 1) * 100
            if m > p["strong"]:
                sig[i] = 1
            elif m < p["weak"]:
                sig[i] = -1
        return sig

    if name == "grid":
        ma20 = ind.sma(closes, 20)
        sig = [0] * n
        for i in range(n):
            if ma20[i] is None:
                continue
            dev = (closes[i] / ma20[i] - 1) * 100
            if dev < -p["dev"]:
                sig[i] = 1
            elif dev > p["dev"]:
                sig[i] = -1
        return sig

    if name == "macd":
        m = ind.macd(closes, p["fast"], p["slow"], p["signal"])
        dif, dea = m["dif"], m["dea"]
        sig = [0] * n
        for i in range(1, n):
            if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
                sig[i] = 1
            elif dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
                sig[i] = -1
        return sig

    if name == "rsi":
        rs = ind.rsi_series(closes, 14)
        sig = [0] * n
        for i in range(n):
            if rs[i] is None:
                continue
            if rs[i] < p["oversold"]:
                sig[i] = 1
            elif rs[i] > p["overbought"]:
                sig[i] = -1
        return sig

    if name == "ma_align":
        ma5 = ind.sma(closes, 5)
        ma10 = ind.sma(closes, 10)
        ma20 = ind.sma(closes, 20)
        ma60 = ind.sma(closes, 60)
        sig = [0] * n
        aligned = False
        for i in range(n):
            if None in (ma5[i], ma10[i], ma20[i], ma60[i]):
                aligned = False
                continue
            cur = ma5[i] > ma10[i] > ma20[i] > ma60[i]
            if not aligned and cur:
                sig[i] = 1
            elif aligned and not cur:
                sig[i] = -1
            aligned = cur
        return sig

    if name == "donchian":
        sig = [0] * n
        for i in range(p["entry"], n):
            hh = max(closes[i - p["entry"]:i])
            ll = min(closes[i - p["exit"]:i])
            if closes[i] > hh:
                sig[i] = 1
            elif closes[i] < ll:
                sig[i] = -1
        return sig

    if name == "kdj":
        k, d, j = ind.kdj(highs, lows, closes, p["n"])
        sig = [0] * n
        for i in range(1, n):
            if k[i] is None or d[i] is None or k[i - 1] is None or d[i - 1] is None:
                continue
            if k[i - 1] <= d[i - 1] and k[i] > d[i] and k[i] < 80:
                sig[i] = 1
            elif k[i - 1] >= d[i - 1] and k[i] < d[i] and k[i] > 20:
                sig[i] = -1
        return sig

    if name == "kdj_j":
        # J 值跌到负值区域后拐头买入，J 冲高卖出（经典超跌反转）
        k, d, j = ind.kdj(highs, lows, closes, p["n"])
        thresh, exit_j = p["thresh"], p["exit"]
        sig = [0] * n
        for i in range(1, n):
            if j[i] is None or j[i - 1] is None:
                continue
            # 只用 J：跌进负值区（或跌破阈值）且开始拐头 → 买
            if j[i] < thresh and j[i] > j[i - 1]:
                sig[i] = 1
            elif j[i] > exit_j:
                sig[i] = -1
        return sig

    if name == "kdj_jw":
        # 日线 J 拐头 + 周线 J 已进入低位（周线定方向，日线定时点）
        k, d, j = ind.kdj(highs, lows, closes, p["n"])
        weekly = _weekly_j(bars, p["n"])
        thresh, exit_j, wth = p["thresh"], p["exit"], p["week_thresh"]
        sig = [0] * n
        for i in range(1, n):
            if j[i] is None or j[i - 1] is None:
                continue
            wj = weekly[i]
            if j[i] < thresh and j[i] > j[i - 1] and wj is not None and wj < wth:
                sig[i] = 1
            elif j[i] > exit_j:
                sig[i] = -1
        return sig

    if name == "cci":
        c = ind.cci(highs, lows, closes, p["n"])
        sig = [0] * n
        for i in range(n):
            if c[i] is None:
                continue
            if c[i] < -100:
                sig[i] = 1
            elif c[i] > 100:
                sig[i] = -1
        return sig

    if name == "combo":
        # 低吸共振：布林下轨 + RSI 超卖 → 买；布林上轨 + RSI 超买 → 卖
        b = ind.bollinger(closes, 20, 2.0)
        rs = ind.rsi_series(closes, 14)
        sig = [0] * n
        for i in range(n):
            if b["lower"][i] is None or rs[i] is None:
                continue
            if closes[i] <= b["lower"][i] and rs[i] < p["rsi_low"]:
                sig[i] = 1
            elif closes[i] >= b["upper"][i] and rs[i] > p["rsi_high"]:
                sig[i] = -1
        return sig

    if name == "keltner":
        # ATR 通道（肯特纳通道）：突破上轨买，跌破下轨卖
        a = ind.atr(highs, lows, closes, p["n"])
        mid = ind.ema(closes, p["n"])
        sig = [0] * n
        for i in range(n):
            if a[i] is None:
                continue
            upper = mid[i] + p["k"] * a[i]
            lower = mid[i] - p["k"] * a[i]
            if closes[i] > upper:
                sig[i] = 1
            elif closes[i] < lower:
                sig[i] = -1
        return sig

    if name == "obv":
        # OBV 量能：OBV 均线金叉买，死叉卖（量价配合）
        vols = [b.get("volume") or 0 for b in bars]
        o = ind.obv(closes, vols)
        os = ind.sma(o, 20)
        ol = ind.sma(o, 60)
        sig = [0] * n
        for i in range(1, n):
            if os[i] is None or ol[i] is None or os[i - 1] is None or ol[i - 1] is None:
                continue
            if os[i - 1] <= ol[i - 1] and os[i] > ol[i]:
                sig[i] = 1
            elif os[i - 1] >= ol[i - 1] and os[i] < ol[i]:
                sig[i] = -1
        return sig

    if name == "ma_squeeze":
        # 均线粘合突破：MA5/10/20 收敛后突破上沿买，跌破下沿卖
        ma5 = ind.sma(closes, 5)
        ma10 = ind.sma(closes, 10)
        ma20 = ind.sma(closes, 20)
        sig = [0] * n
        for i in range(1, n):
            if None in (ma5[i], ma10[i], ma20[i], ma20[i - 1]):
                continue
            spread = (max(ma5[i], ma10[i], ma20[i]) - min(ma5[i], ma10[i], ma20[i])) / ma20[i] * 100
            if spread < p["threshold"]:  # 粘合
                if closes[i] > ma20[i] and closes[i - 1] <= ma20[i - 1]:
                    sig[i] = 1
                elif closes[i] < ma20[i] and closes[i - 1] >= ma20[i - 1]:
                    sig[i] = -1
        return sig

    return [0] * n
