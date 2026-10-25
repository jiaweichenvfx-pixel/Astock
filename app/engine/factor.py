"""选股策略对比：短/中/长三套独立系统。

三档的「入选逻辑」和「回测方法」都不同，不是同一套公式换持有期：

| 维度 | 短线 | 中线 | 长线 |
|---|---|---|---|
| 股票池 | 高换手活跃股 | 流动性好的趋势股 | 大市值龙头 |
| 特征 | 当日涨幅/量比/换手/连涨 | 20-60日趋势/均线/距前高 | 60-120日趋势/年线/长期波动 |
| 策略 | 强势/放量突破/超跌反抽/涨停动能 | 均线多头/突破/动量/温和放量/低波动 | 年线趋势/60日动量/长期低波动 |
| 再平衡 | 每 2 交易日 | 每 5 交易日 | 每 20 交易日 |
| 持有期 | 5 日 | 20 日 | 60 日 |

结果按日期缓存，每天重算，自动采用每档历史最优策略。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from ..data import adapter

TOP_K = 5


# ── 三套股票池 ───────────────────────────────────────────────────
def _universe_hot(spot: dict) -> list[dict]:
    """短线池：高换手活跃。换手率优先，缺失则用成交额。"""
    rows = []
    for code, r in spot.items():
        name = r.get("name") or ""
        if "ST" in name.upper() or "退" in name:
            continue
        if code.startswith(("4", "8", "920")):
            continue
        if (r.get("price") or 0) < 3:
            continue
        rows.append({"code": code, "name": name,
                     "turnover": r.get("turnover") or 0, "turnover_rate": r.get("turnover_rate") or 0.0})
    rows.sort(key=lambda x: (-x["turnover_rate"], -x["turnover"]))
    return rows[:120]


def _universe_trend(spot: dict) -> list[dict]:
    """中线池：流动性好的趋势股（成交额靠前）。"""
    rows = []
    for code, r in spot.items():
        name = r.get("name") or ""
        if "ST" in name.upper() or "退" in name:
            continue
        if code.startswith(("4", "8", "920")):
            continue
        if (r.get("price") or 0) < 3 or (r.get("turnover") or 0) < 2e8:
            continue
        rows.append({"code": code, "name": name, "turnover": r.get("turnover") or 0,
                     "turnover_rate": 0.0})
    rows.sort(key=lambda x: -x["turnover"])
    return rows[:120]


def _universe_large(spot: dict) -> list[dict]:
    """长线池：大市值龙头。市值优先，缺失则用成交额。"""
    rows = []
    for code, r in spot.items():
        name = r.get("name") or ""
        if "ST" in name.upper() or "退" in name:
            continue
        if code.startswith(("4", "8", "920")):
            continue
        if (r.get("price") or 0) < 3:
            continue
        rows.append({"code": code, "name": name,
                     "turnover": r.get("turnover") or 0, "market_cap": r.get("market_cap") or 0.0})
    rows.sort(key=lambda x: (-x["market_cap"], -x["turnover"]))
    return rows[:120]


# ── 三套特征 ─────────────────────────────────────────────────────
def _ma(closes, i, n):
    return sum(closes[i - n + 1: i + 1]) / n if i + 1 >= n else None


def _chg(closes, i, n):
    return (closes[i] / closes[i - n] - 1) * 100 if closes[i - n] > 0 else None


def _streak(closes, i):
    k = 0
    j = i
    while j > 0 and closes[j] >= closes[j - 1]:
        k += 1
        j -= 1
    return k


def _vol_annual(closes, i, n=20):
    rets = [(closes[j] / closes[j - 1] - 1) for j in range(i - n + 1, i + 1) if closes[j - 1] > 0]
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    return (sum((r - m) ** 2 for r in rets) / len(rets)) ** 0.5 * (250 ** 0.5) * 100


def _features_short(closes, vols, i):
    if i < 10 or i >= len(closes):
        return None
    c = closes[i]
    v5 = sum(vols[i - 5: i]) / 5 if i >= 5 else (vols[i] or 1)
    return {
        "pct": (c / closes[i - 1] - 1) * 100 if closes[i - 1] > 0 else 0,
        "chg3": _chg(closes, i, 3), "chg5": _chg(closes, i, 5),
        "vol_ratio": (vols[i] / v5) if v5 else None,
        "streak": _streak(closes, i),
        "dist_high5": (max(closes[i - 5: i + 1]) / c - 1) * 100 if c else None,
        "close": c,
    }


def _features_mid(closes, vols, i):
    if i < 60 or i >= len(closes):
        return None
    c = closes[i]
    ma5, ma20 = _ma(closes, i, 5), _ma(closes, i, 20)
    v5 = sum(vols[i - 5: i]) / 5 if i >= 5 else 0
    return {
        "chg20": _chg(closes, i, 20), "chg60": _chg(closes, i, 60),
        "ma5": ma5, "ma20": ma20, "above_ma20": (c >= ma20) if ma20 else None,
        "ma5_gt_ma20": (ma5 >= ma20) if (ma5 and ma20) else None,
        "dist_high20": (max(closes[i - 20: i + 1]) / c - 1) * 100 if c else None,
        "vol_ratio": (vols[i] / v5) if v5 else None,
        "vol": _vol_annual(closes, i, 20),
        "close": c,
    }


def _features_long(closes, vols, i):
    if i < 250 or i >= len(closes):
        return None
    c = closes[i]
    ma60, ma250 = _ma(closes, i, 60), _ma(closes, i, 250)
    return {
        "chg60": _chg(closes, i, 60), "chg120": _chg(closes, i, 120),
        "ma60": ma60, "ma250": ma250,
        "above_ma250": (c >= ma250) if ma250 else None,
        "ma60_gt_ma250": (ma60 >= ma250) if (ma60 and ma250) else None,
        "vol": _vol_annual(closes, i, 60),
        "dist_high60": (max(closes[i - 60: i + 1]) / c - 1) * 100 if c else None,
        "close": c,
    }


# ── 三套策略 ─────────────────────────────────────────────────────
def _s_hot_money(f):
    return (f["chg5"] or 0) + 2 * (f["vol_ratio"] or 0) + (f["streak"] or 0)


def _s_vol_breakout(f):
    return (f["pct"] or 0) + (10 if (f["pct"] or 0) > 3 and (f["vol_ratio"] or 0) > 1.5 else 0) \
        - (f["dist_high5"] or 0)


def _s_reversal5(f):
    return -(f["chg5"] or 0) + (5 if (f["pct"] or 0) > 0 else 0)


def _s_mom3(f):
    return f["chg3"]


def _s_limit_up(f):
    return (f["pct"] or 0) + 2 * (f["vol_ratio"] or 0)


def _s_trend(f):
    s = (10 if f["above_ma20"] else 0) + (5 if f["ma5_gt_ma20"] else 0)
    return s + (f["chg20"] or 0) * 0.2


def _s_breakout(f):
    return -1 * (f["dist_high20"] or 0)


def _s_mom20(f):
    return f["chg20"]


def _s_mom60(f):
    return f["chg60"]


def _s_volume(f):
    return f["vol_ratio"]


def _s_lowvol(f):
    return -1 * (f["vol"] or 0)


def _s_combo_mid(f):
    s = (f["chg20"] or 0) + 2 * (f["vol_ratio"] or 0)
    return s + (10 if f["above_ma20"] else 0)


def _s_trend250(f):
    s = (10 if f["above_ma250"] else 0) + (5 if f["ma60_gt_ma250"] else 0)
    return s + (f["chg60"] or 0) * 0.1


def _s_mom60l(f):
    return f["chg60"]


def _s_lowvol60(f):
    return -1 * (f["vol"] or 0)


def _s_combo_long(f):
    s = (f["chg60"] or 0) + (f["chg120"] or 0) * 0.5
    return s + (10 if f["above_ma250"] else 0)


SYSTEMS = {
    "short": {
        "label": "短线", "universe": _universe_hot, "features": _features_short,
        "rebalance": 2, "forward": 5, "lookback": 120,
        "strategies": {
            "hot_money": (_s_hot_money, "强势(涨+量+连涨)"),
            "vol_breakout": (_s_vol_breakout, "放量突破"),
            "reversal5": (_s_reversal5, "超跌反抽"),
            "mom3": (_s_mom3, "3日动量"),
            "limit_up": (_s_limit_up, "涨停动能"),
        },
    },
    "mid": {
        "label": "中线", "universe": _universe_trend, "features": _features_mid,
        "rebalance": 5, "forward": 20, "lookback": 320,
        "strategies": {
            "trend": (_s_trend, "均线多头"),
            "breakout": (_s_breakout, "突破新高"),
            "mom20": (_s_mom20, "20日动量"),
            "mom60": (_s_mom60, "60日动量"),
            "volume": (_s_volume, "温和放量"),
            "lowvol": (_s_lowvol, "低波动"),
            "combo_mid": (_s_combo_mid, "组合(动量+量能+均线)"),
        },
    },
    "long": {
        "label": "长线", "universe": _universe_large, "features": _features_long,
        "rebalance": 20, "forward": 60, "lookback": 450,
        "strategies": {
            "trend250": (_s_trend250, "年线趋势"),
            "mom60": (_s_mom60l, "60日动量"),
            "lowvol60": (_s_lowvol60, "长期低波动"),
            "combo_long": (_s_combo_long, "组合(年线+动量)"),
        },
    },
}

HORIZONS = tuple(SYSTEMS)


# ── 回测 ─────────────────────────────────────────────────────────
def universe(horizon: str) -> list[dict]:
    spot = adapter.get_spot_snapshot_cached(3600) or {}
    return SYSTEMS[horizon]["universe"](spot)


def _load_series(code: str, lookback: int) -> tuple[list[float], list[float]]:
    bars = adapter.get_stock_history(code, days=lookback)
    if not bars:
        return [], []
    closes = [b["close"] for b in bars if b.get("close")]
    vols = [b.get("volume") or 0 for b in bars]
    return closes, vols


def rank_strategies(horizon: str = "mid", top_k: int = TOP_K) -> dict:
    cfg = SYSTEMS[horizon]
    uni = universe(horizon)
    strat_fns = cfg["strategies"]

    acc = {s: [] for s in strat_fns}
    baseline = []

    for item in uni:
        closes, vols = _load_series(item["code"], cfg["lookback"])
        if not closes:
            continue
        n = len(closes)
        for i in range(0, n - cfg["forward"], cfg["rebalance"]):
            f = cfg["features"](closes, vols, i)
            if f is None:
                continue
            fwd = closes[i + cfg["forward"]] / closes[i] - 1
            for s, (fn, _lab) in strat_fns.items():
                sc = fn(f)
                if sc is not None:
                    acc[s].append((sc, i, fwd))
            baseline.append((i, fwd))

    result = []
    for s, rows in acc.items():
        by_date = {}
        for sc, i, fwd in rows:
            by_date.setdefault(i, []).append((sc, fwd))
        rets, beats = [], []
        for i, lst in by_date.items():
            lst.sort(key=lambda x: -x[0])
            top = lst[:top_k]
            avg = sum(x[1] for x in top) / len(top)
            base = _base_at(baseline, i)
            rets.append(avg)
            if base is not None:
                beats.append(1 if avg > base else 0)
        if rets:
            wins = sum(1 for r in rets if r > 0)
            result.append({
                "strategy": s, "label": strat_fns[s][1],
                "avg_return": round(sum(rets) / len(rets) * 100, 2),
                "median": round(sorted(rets)[len(rets) // 2] * 100, 2),
                "win_rate": round(wins / len(rets) * 100, 1),
                "beat_ratio": round(sum(beats) / len(beats) * 100, 1) if beats else None,
                "samples": len(rets),
            })

    result.sort(key=lambda x: -x["avg_return"])
    base_avg = round(sum(x[1] for x in baseline) / len(baseline) * 100, 2) if baseline else None
    return {
        "horizon": horizon, "label": cfg["label"],
        "forward_days": cfg["forward"], "rebalance_days": cfg["rebalance"],
        "universe": len(uni), "baseline_avg": base_avg, "strategies": result,
    }


def _base_at(baseline: list[tuple[int, float]], i: int) -> Optional[float]:
    vals = [fwd for idx, fwd in baseline if idx == i]
    return sum(vals) / len(vals) if vals else None


def _rank_key(horizon: str, d: str) -> str:
    return f"factor2_{horizon}_{d}"


def get_rank(horizon: str, universe_size: Optional[int] = None) -> dict:
    """读当天缓存；没有就现算落库。"""
    d = adapter.data_day()
    cached = adapter._cache_get(_rank_key(horizon, d), 86400 * 20)
    if cached and cached.get("rows"):
        return cached["rows"]
    result = rank_strategies(horizon)
    adapter._cache_set(_rank_key(horizon, d), {"rows": result})
    return result


def best_strategy(horizon: str) -> str:
    rank = get_rank(horizon)
    top = (rank.get("strategies") or [None])[0]
    return top["strategy"] if top else "combo_mid"


def pick_today(horizon: str, strategy: str, top_k: int = 10) -> list[dict]:
    """用某套策略给今天打分，返回 top_k 候选。"""
    cfg = SYSTEMS[horizon]
    fn = cfg["strategies"].get(strategy)
    if fn is None:
        return []
    score_fn = fn[0]
    scored = []
    for item in universe(horizon):
        closes, vols = _load_series(item["code"], cfg["lookback"])
        if not closes:
            continue
        f = cfg["features"](closes, vols, len(closes) - 1)
        sc = score_fn(f) if f else None
        if sc is not None:
            scored.append({"code": item["code"], "name": item["name"],
                           "score": round(sc, 2), "price": closes[-1],
                           "reasons": [fn[1]]})
    scored.sort(key=lambda x: -x["score"])
    return scored[:top_k]
