"""长线 / 中线 / 短线三档回测：不同周期、不同止损止盈、按市场情绪给建议。

为什么不能一套参数跑所有周期：
- 长线（周线）看的是趋势和估值回归，止损设 -12% 会在正常回撤里被洗出去 → 不设止损、拿住；
- 中线（日线）看波段，用 -12% 止损；
- 短线（60 分钟）看情绪和节奏，-12% 一周都碰不到 → 用 -5% 止损 / +8% 止盈 / 10 根时间止损。

市场情绪只影响「建议用哪一档」，不篡改历史数据：
- 普涨/放量 → 短线（趋势延续，快进快出）
- 温和 → 中线
- 跌多涨少/缩量 → 长线（少动，避免在震荡里反复止损）
若情绪推荐的那档历史上不赚钱，则退回历史最好的那档，并说明「情绪与历史不一致」。
"""
from __future__ import annotations

from typing import Optional

from ..data import adapter
from . import action_plan
from .backtest import analyze_strategies

# 各档：周期 / 取多少根 / 止损止盈 / 说明
HORIZONS: dict[str, dict] = {
    "long": {
        "label": "长线",
        "period": "weekly",
        "days": 300,
        "period_label": "周线",
        "desc": "约 6 年周线，看趋势与估值回归，不设固定止损",
        "exit": {"stop_loss": None, "take_profit": None, "time_stop_days": None},
    },
    "mid": {
        "label": "中线",
        "period": "daily",
        "days": 320,
        "period_label": "日线",
        "desc": "约 15 个月日线，看波段，止损 -12%",
        "exit": {"stop_loss": -12, "take_profit": None, "time_stop_days": None},
    },
    "short": {
        "label": "短线",
        "period": "60",
        "days": 200,
        "period_label": "60分钟",
        "desc": "约 2 个月 60 分钟线，看情绪与节奏，止损 -5% / 止盈 +8% / 10 根时间止损",
        "exit": {"stop_loss": -5, "take_profit": 8, "time_stop_days": 10},
    },
}

# 短线专用快参：把日线节奏的参数压到 60 分钟上（换手明显变高，但实测常常更差）
FAST_PARAMS = {
    "ma": {"short": 3, "long": 8},
    "bollinger": {"n": 10, "k": 2.0},
    "donchian": {"entry": 8, "exit": 4},
    "macd": {"fast": 6, "slow": 13, "signal": 5},
    "grid": {"dev": 1.5},
    "kdj": {"n": 5},
    "cci": {"n": 9},
    "keltner": {"n": 10, "k": 2.0},
    "ma_squeeze": {"threshold": 1.0},
    "combo": {"rsi_low": 30, "rsi_high": 70},
    "momentum": {"days": 20, "strong": 8, "weak": -8},
}

# 各档的「换手门槛」：短线至少要 5 笔（约 2 个月），否则不配叫短线
MIN_TRADES = {"long": 1, "mid": 2, "short": 5}

# 每档一年有多少根 K 线（用于年化换手）
BARS_PER_YEAR = {"long": 52, "mid": 250, "short": 250 * 4}
BARS_PER_DAY = {"long": 1 / 5, "mid": 1, "short": 4}   # 一根 K 线约等于几个交易日

# 情绪 → 推荐档位
SENTIMENT_FIT = {
    "hot": {"best": "short", "why": "普涨放量，趋势延续性强，短线快进快出效率最高"},
    "mild": {"best": "mid", "why": "情绪温和，波段最完整，日线级别最好做"},
    "cold": {"best": "long", "why": "跌多涨少/缩量，短周期噪声大、反复止损，宁可用长线拿住"},
}


def _exit_for(horizon: str):
    """返回给 analyze_strategies 用的止损回调：长线战法不设止损，其余用该档参数。"""
    preset = HORIZONS[horizon]

    def _f(name: str) -> dict:
        if name == "value":
            return {"stop_loss": None, "take_profit": None, "time_stop_days": None}
        return dict(preset["exit"])

    return _f


def market_sentiment() -> dict:
    """当前市场情绪（温度 + 涨跌比 + 成交额 + 指数位置）。

    打分逻辑复用 pipeline.compute_market_env，避免两处口径不一致。
    """
    from ..pipeline import compute_market_env

    env = compute_market_env()
    raw = adapter.get_market_env_raw() or {}
    above = env.index_above_ma20
    return {
        "temperature": env.temperature.value,
        "label": {"hot": "火热", "mild": "温和", "cold": "寒冷"}[env.temperature.value],
        "score": env.score,
        "adv_decl_ratio": raw.get("adv_decl_ratio"),
        "up": raw.get("up_count"),
        "down": raw.get("down_count"),
        "turnover_yi": raw.get("turnover_yi"),
        "index_above_ma20": above,
    }


def analyze_horizon(code: str, name: str, horizon: str) -> dict:
    """单档回测：取该周期的 K 线 → 15 套战法并排 → 最优战法 + 买卖点。"""
    preset = HORIZONS[horizon]
    bars = adapter.get_stock_history(code, days=preset["days"], period=preset["period"])
    if not bars or len(bars) < 40:
        return {
            "ok": False,
            "horizon": horizon,
            "label": preset["label"],
            "period_label": preset["period_label"],
            "error": f"该周期K线不足（{len(bars) if bars else 0} 根，需 ≥40）",
        }

    analyses = analyze_strategies(bars, exit_for=_exit_for(horizon))

    # 按档位的最小换手筛战法：短线 2 个月不到 5 笔的，不算短线战法
    floor = MIN_TRADES.get(horizon, 1)
    eligible = [a for a in analyses if a.trade_count >= floor]
    fallback = not eligible
    pool = analyses if fallback else eligible
    best = max(pool, key=lambda a: a.score)

    selection = {
        "min_trades": floor,
        "pool_size": len(pool),
        "analyzed": len(analyses),
        "fallback": fallback,
        "note": (f"没有战法达到「{floor} 笔」的换手门槛，已取综合分最高者（换手偏低）"
                 if fallback else f"先按「≥{floor} 笔」筛掉低频战法，再从 {len(pool)} 套里选综合分最高"),
    }

    from .backtest import run_backtest
    from .strategies import strategy_signal

    best_bt = run_backtest(bars, strategy_signal(best.name, bars),
                           _exit_for(horizon)(best.name), label=best.label)
    turnover = _turnover(bars, best_bt, horizon)

    fast_row = None
    fast = FAST_PARAMS.get(best.name)
    if fast:
        fast_bt = run_backtest(bars, strategy_signal(best.name, bars, fast),
                               dict(preset["exit"]), label=best.label)
        fast_row = {
            "params": fast,
            "total_return": fast_bt.total_return,
            "trade_count": fast_bt.trade_count,
            "win_rate": fast_bt.win_rate,
        }
    plan = action_plan.build_action_plan(
        code=code, name=name, bars=bars, strategy=best.name,
        params=None, exit_params=(None if best.name == "value" else dict(preset["exit"])),
        period_label=preset["period_label"],
    )
    default_params = plan.get("params") if plan.get("ok") else None

    return {
        "ok": True,
        "horizon": horizon,
        "label": preset["label"],
        "period": preset["period"],
        "period_label": preset["period_label"],
        "desc": preset["desc"],
        "bars": len(bars),
        "first_date": bars[0]["date"],
        "last_date": bars[-1]["date"],
        "best": {
            "name": best.name,
            "label": best.label,
            "params": default_params,
            "total_return": best.total_return,
            "capture_ratio": best.capture_ratio,
            "win_rate": _win_rate(bars, best.name, horizon),
            "max_drawdown": best.max_drawdown,
            "trade_count": best.trade_count,
            "score": best.score,
        },
        "selection": selection,
        "turnover": turnover,
        "fast_variant": fast_row,
        "top": [
            {"label": a.label, "name": a.name, "total_return": a.total_return,
             "max_drawdown": a.max_drawdown, "trade_count": a.trade_count, "score": a.score}
            for a in analyses[:3]
        ],
        "plan": plan,
    }


def _turnover(bars: list[dict], bt, horizon: str) -> dict:
    """换手画像：笔数、平均持有几根K线/几天、年化笔数、费用拖累。"""
    from .. import config

    idx = {b["date"]: i for i, b in enumerate(bars)}
    holds = []
    for t in bt.trades:
        i, j = idx.get(t["buy_date"]), idx.get(t["sell_date"])
        if i is not None and j is not None and j >= i:
            holds.append(j - i)
    avg_bars = round(sum(holds) / len(holds), 1) if holds else None
    bars_per_day = BARS_PER_DAY.get(horizon, 1)
    avg_days = round(avg_bars / bars_per_day, 1) if avg_bars is not None else None

    years = max(len(bars) / BARS_PER_YEAR.get(horizon, 250), 1e-6)
    per_year = round(bt.trade_count / years, 1) if bt.trade_count else 0.0

    cfg = config.get_backtest()
    fee_pct = (cfg["commission_rate"] * 2 + cfg["stamp_tax_rate"]
               + cfg["transfer_fee_rate"] * 2 + cfg["slippage"] * 2) * 100
    return {
        "trades": bt.trade_count,
        "avg_hold_bars": avg_bars,
        "avg_hold_days": avg_days,
        "trades_per_year": per_year,
        "cost_per_trade_pct": round(fee_pct, 3),
        "cost_drag_pct": round(fee_pct * bt.trade_count, 2),
    }


def _win_rate(bars: list[dict], strategy: str, horizon: str) -> Optional[float]:
    """最优战法在该周期下的胜率（analyze_strategies 不返回胜率，这里补一次回测）。"""
    from .backtest import run_backtest
    from .strategies import strategy_signal

    try:
        sig = strategy_signal(strategy, bars)
        ep = _exit_for(horizon)(strategy)
        return run_backtest(bars, sig, ep).win_rate
    except Exception:
        return None


def _recommend(sentiment: dict, results: list[dict]) -> dict:
    """按情绪推荐一档；若该档历史不赚钱，退回历史最好的一档。"""
    temp = sentiment.get("temperature", "mild")
    fit = SENTIMENT_FIT.get(temp, SENTIMENT_FIT["mild"])
    by_key = {r["horizon"]: r for r in results if r.get("ok")}
    preferred = by_key.get(fit["best"])

    fallback = None
    if preferred is None or preferred["best"]["total_return"] <= 0:
        fallback = max(by_key.values(), key=lambda r: r["best"]["total_return"]) if by_key else None
    pick = preferred if (preferred and fallback is None) else (fallback or preferred)

    if pick is None:
        return {"horizon": None, "label": None, "why": "无可用周期数据"}

    if fallback is not None and pick is not preferred:
        why = (f"当前情绪「{sentiment.get('label')}」本该做{HORIZONS[fit['best']]['label']}；"
               f"但该档历史上不赚钱，改用历史最好的{pick['label']}（情绪与历史不一致，注意）")
    else:
        why = f"当前情绪「{sentiment.get('label')}」→ {fit['why']}"

    return {"horizon": pick["horizon"], "label": pick["label"],
            "period_label": pick["period_label"], "why": why}


def compare_horizons(code: str, name: str) -> dict:
    """三档全跑一遍：长线（周线）/ 中线（日线）/ 短线（60分钟）+ 情绪建议。"""
    sentiment = market_sentiment()
    results = [analyze_horizon(code, name, h) for h in ("long", "mid", "short")]
    return {
        "ok": True,
        "code": code,
        "name": name,
        "sentiment": sentiment,
        "horizons": results,
        "recommend": _recommend(sentiment, results),
    }
