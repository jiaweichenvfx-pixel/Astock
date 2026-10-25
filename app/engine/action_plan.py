"""下一步买卖点：把回测/寻优选出的战法，翻译成「明天什么价该买、什么价该卖」。

核心思路（与回测口径完全一致）：
1. 用相同规则复现持仓状态 —— 信号收盘判定、次日开盘成交、止损 -x% 盘中触发；
2. 把「明天可能的收盘价」当成新的一根 K 线追加到序列末尾，重算该战法信号；
3. 在现价 ±30% 区间逐档试探收盘价（先粗扫 0.3%，再在命中价附近细扫 0.02%），
   取离现价最近的买/卖触发价；
4. 再给出关键价位（均线/布林/20日高低/ATR）与止损价，方便人工确认。

触发价都是「收盘价条件」：明日收盘满足 → 后一交易日开盘按该战法买卖。
买点方向随战法类型不同：突破型是「站上 X 买」，低吸型是「跌到 X 买」，这里会自动分辨。
"""
from __future__ import annotations

from typing import Optional

from . import indicators as ind
from .backtest import _exit_params_for, run_backtest
from .strategies import STRATEGY_LABEL, _params, strategy_signal

COARSE_RANGE = 0.30    # 触发价试探区间 ±30%
COARSE_STEPS = 200     # 粗扫 0.3% 一档
FINE_STEPS = 40        # 细扫：命中价 ±1 粗档内 0.015% 一档
SCAN_BARS = 160        # 试探只用最近 160 根（够 MA60/动量120 预热）
MIN_BARS = 30


def _probe_signal(bars: list[dict], strategy: str, params: dict, price: float, volume: float) -> int:
    """把 price 当作新一根收盘 K 线追加，返回该战法在其上的信号（+1/-1/0）。"""
    probe = bars + [{
        "date": "T+1",
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "volume": volume,
    }]
    sig = strategy_signal(strategy, probe, params)
    return sig[-1] if sig else 0


def _scan_triggers(
    bars: list[dict], strategy: str, params: dict, last_close: float, volume: float
) -> tuple[Optional[float], Optional[float]]:
    """扫描现价 ±30% 的收盘价，返回 (最近买触发价, 最近卖触发价)。"""
    if last_close <= 0:
        return None, None
    lo, hi = last_close * (1 - COARSE_RANGE), last_close * (1 + COARSE_RANGE)
    step = (hi - lo) / COARSE_STEPS
    hits: dict[str, Optional[float]] = {"buy": None, "sell": None}
    gaps: dict[str, Optional[float]] = {"buy": None, "sell": None}

    for i in range(COARSE_STEPS + 1):
        price = lo + step * i
        s = _probe_signal(bars, strategy, params, price, volume)
        side = "buy" if s > 0 else ("sell" if s < 0 else None)
        if side is None:
            continue
        gap = abs(price - last_close)
        if gaps[side] is None or gap < gaps[side]:
            hits[side], gaps[side] = price, gap

    # 细扫：在粗扫命中价 ±1 粗档内找「离现价最近的合格价」（即触发边界）
    for side in ("buy", "sell"):
        px = hits[side]
        if px is None:
            continue
        best, best_gap = px, abs(px - last_close)
        for i in range(FINE_STEPS + 1):
            cand = px - step + 2 * step * i / FINE_STEPS
            if cand <= 0.01:
                continue
            s = _probe_signal(bars, strategy, params, cand, volume)
            ok = s > 0 if side == "buy" else s < 0
            gap = abs(cand - last_close)
            if ok and gap < best_gap:
                best, best_gap = cand, gap
        hits[side] = best
    return hits["buy"], hits["sell"]


def _levels(bars: list[dict]) -> list[dict]:
    """关键价位：均线 / 布林带 / 20 日高低 / ATR。"""
    closes = [b["close"] for b in bars]
    highs = [b.get("high", b["close"]) for b in bars]
    lows = [b.get("low", b["close"]) for b in bars]

    def last(seq):
        for v in reversed(seq):
            if v is not None:
                return round(v, 2)
        return None

    ma5, ma10, ma20, ma60 = (last(ind.sma(closes, n)) for n in (5, 10, 20, 60))
    boll = ind.bollinger(closes, 20, 2.0)
    atr14 = last(ind.atr(highs, lows, closes, 14))
    close = closes[-1]

    rows = [
        {"k": "现价", "v": round(close, 2)},
        {"k": "MA5", "v": ma5},
        {"k": "MA10", "v": ma10},
        {"k": "MA20", "v": ma20},
        {"k": "MA60", "v": ma60},
        {"k": "布林上轨", "v": last(boll["upper"])},
        {"k": "布林中轨", "v": last(boll["mid"])},
        {"k": "布林下轨", "v": last(boll["lower"])},
        {"k": "20日最高", "v": round(max(highs[-20:]), 2)},
        {"k": "20日最低", "v": round(min(lows[-20:]), 2)},
        {"k": "ATR14", "v": atr14},
    ]
    if atr14 and close:
        rows.append({"k": "ATR14 占价", "v": round(atr14 / close * 100, 2), "unit": "%"})
    return [r for r in rows if r["v"] is not None]


def build_action_plan(
    code: str,
    name: str,
    bars: list[dict],
    strategy: str,
    params: Optional[dict] = None,
    exit_params: Optional[dict] = None,
    period_label: str = "daily",
) -> dict:
    """生成「下一步买卖点」计划。bars 为升序 K 线（不足 MIN_BARS 根返回 error）。"""
    if not bars or len(bars) < MIN_BARS:
        return {"ok": False, "error": f"K线不足（{len(bars) if bars else 0} 根，需 ≥{MIN_BARS}）"}

    label = STRATEGY_LABEL.get(strategy, strategy)
    p = _params(strategy, params)
    ep = dict(exit_params) if exit_params else _exit_params_for(strategy)

    # 1. 复现当前持仓状态（不强平，保留期末持仓）
    sig = strategy_signal(strategy, bars, p)
    bt = run_backtest(bars, sig, ep, code=code, name=name, label=label, force_close=False)

    last_close = round(bars[-1]["close"], 2)
    last_date = bars[-1]["date"]
    volume = bars[-1].get("volume") or 0

    # 2. 最近一次信号（含多久之前）
    last_sig, last_sig_idx = 0, None
    for i in range(len(sig) - 1, -1, -1):
        if sig[i] != 0:
            last_sig, last_sig_idx = sig[i], i
            break

    # 3. 试探触发价（只用最近一段，够预热且更快）
    probe_bars = bars[-SCAN_BARS:] if len(bars) > SCAN_BARS else bars
    buy_px, sell_px = _scan_triggers(probe_bars, strategy, p, last_close, volume)

    def _dist(px):
        return None if px is None else round((px / last_close - 1) * 100, 2)

    in_pos = bool(bt.in_position)
    entry = bt.entry_price
    stop_pct = ep.get("stop_loss")
    stop_px = round(entry * (1 + stop_pct / 100), 2) if (in_pos and entry and stop_pct is not None) else None
    # 空仓时给一个「若明后天买入」的止损参考（按触发价/现价估算）
    ref_entry = entry if in_pos else (round(buy_px, 2) if buy_px else last_close)
    stop_hint = round(ref_entry * (1 + stop_pct / 100), 2) if stop_pct is not None else None

    pending_buy = (not in_pos) and sig[-1] > 0    # 今日收盘刚触发 → 明日开盘买
    pending_sell = in_pos and sig[-1] < 0         # 今日收盘刚触发 → 明日开盘卖

    buy = {
        "trigger": None if buy_px is None else round(buy_px, 2),
        "distance_pct": _dist(buy_px),
        "pending": pending_buy,
        "note": "",
    }
    sell = {
        "trigger": None if sell_px is None else round(sell_px, 2),
        "distance_pct": _dist(sell_px),
        "pending": pending_sell,
        "note": "",
    }

    # 4. 文案：方向随战法类型变化（突破型「站上」，低吸型「跌到」）
    if in_pos:
        buy["note"] = "已持仓，本战法不加仓"
        if pending_sell:
            sell["note"] = "今日收盘已触发卖出信号 → 明日开盘卖出"
        elif sell["trigger"] is not None:
            arrow = "≤" if sell["trigger"] < last_close else "≥"
            sell["note"] = f"明日收盘 {arrow} {sell['trigger']} 触发卖出，后日开盘卖出"
        else:
            sell["note"] = "±30% 内无卖出触发价，只靠止损纪律"
    else:
        sell["note"] = "空仓中，无持仓可卖"
        if pending_buy:
            buy["note"] = "今日收盘已触发买入信号 → 明日开盘买入"
        elif buy["trigger"] is not None:
            arrow = "≥" if buy["trigger"] > last_close else "≤"
            buy["note"] = f"明日收盘 {arrow} {buy['trigger']} 触发买入，后日开盘买入"
        else:
            buy["note"] = "±30% 内无买入触发价（该战法需要多条件共振）"

    # 5. 一句话结论 + 动作类型
    if strategy == "value":
        action = {"type": "hold", "label": "长线持有", "detail": "基准战法，买入并持有，不看买卖点"}
        advice = "长线持有是基准战法（买入不动），用它做对照，不必当成交易指令。"
    elif in_pos and pending_sell:
        action = {"type": "sell_now", "label": "明日开盘卖出",
                  "detail": f"今日收盘已触发卖出；成本 {entry}，浮盈 {bt.entry_pnl_pct}%"}
        advice = f"持仓中（{bt.entry_date} 买入 {entry}，浮盈 {bt.entry_pnl_pct}%）：今日已触发卖出信号，明日开盘卖出。"
    elif in_pos:
        bits = [f"持仓中（{bt.entry_date} 买入 {entry}，浮盈 {bt.entry_pnl_pct}%）"]
        if sell["trigger"] is not None:
            bits.append(f"明日收盘 {sell['trigger']} 是卖出线（{sell['distance_pct']}%）")
        if stop_px is not None:
            bits.append(f"止损 {stop_px}")
        action = {"type": "hold", "label": "继续持有",
                  "detail": f"下一卖出触发 {sell['trigger'] or '—'}；止损 {stop_px or '—'}"}
        advice = "；".join(bits) + "。"
    elif pending_buy:
        action = {"type": "buy_now", "label": "明日开盘买入",
                  "detail": f"今日收盘触发买入，参考买入价 {last_close}"}
        advice = f"空仓 + 今日收盘（{last_close}）已触发买入信号：明日开盘买入。"
    elif buy["trigger"] is not None:
        arrow = "≥" if buy["trigger"] > last_close else "≤"
        action = {"type": "wait_buy", "label": "空仓等待买点",
                  "detail": f"明日收盘 {arrow} {buy['trigger']} 触发买入"}
        advice = f"当前空仓：明日收盘 {arrow} {buy['trigger']}（{buy['distance_pct']}%）才买入，否则继续等。"
    else:
        action = {"type": "none", "label": "无触发价", "detail": "该战法当前无有效买点"}
        advice = "当前空仓，±30% 内无买入触发价：该战法需要更极端的条件，建议换战法或改参数。"

    return {
        "ok": True,
        "code": code,
        "name": name,
        "strategy": strategy,
        "label": label,
        "params": p,
        "period": period_label,
        "bars": len(bars),
        "last_date": last_date,
        "last_close": last_close,
        "action": action,
        "state": {
            "in_position": in_pos,
            "entry_price": entry,
            "entry_date": bt.entry_date or None,
            "pnl_pct": bt.entry_pnl_pct,
            "days_held": bt.entry_days_held if in_pos else None,
        },
        "last_signal": {
            "side": "buy" if last_sig > 0 else ("sell" if last_sig < 0 else None),
            "date": None if last_sig_idx is None else bars[last_sig_idx]["date"],
            "bars_ago": None if last_sig_idx is None else len(bars) - 1 - last_sig_idx,
        },
        "buy": buy,
        "sell": sell,
        "stop_loss": {
            "price": stop_px,
            "pct": stop_pct,
            "hint": stop_hint,
            "note": ("按买入价回撤" if stop_px is not None else
                     ("若近日买入则参考止损" if stop_hint is not None else "该战法不设固定止损")),
        },
        "levels": _levels(bars),
        "advice": advice,
        "basis": "触发价按收盘价推算（不含盘中振幅），成交价在次一交易日开盘 ± 滑点",
    }
