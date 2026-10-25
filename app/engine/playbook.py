"""一键决策：回测选战法 → 寻优选参数 → 样本外验收 → 生成买卖点。

为什么要合起来：
- 回测（默认参数）横向比 15 套战法，回答「用哪一招」；
- 寻优（参数网格）纵向比同一招的参数，回答「这一招用多大劲」；
- 但寻优是在同一段历史上反复挑最优，天然过拟合，所以必须切一段样本外验证：
  前 70% 调参、后 30% 检验，只有样本外也赢的参数才采用，否则退回默认参数。

信号切片是安全的：strategy_signal 在第 i 根只用到 ≤i 的数据，
所以「全序列算信号 → 按时间切片」等价于「分段独立算」，不会偷看未来。
"""
from __future__ import annotations

from typing import Optional

from . import action_plan
from .backtest import _exit_params_for, analyze_strategies, run_backtest
from .strategies import PARAM_GRIDS, STRATEGY_LABEL, _params, strategy_signal

MIN_BARS = 120
OOS_MIN_BARS = 40
IS_RATIO = 0.7


def _segments(name: str, params: dict, bars: list[dict], split: int):
    """同一组参数，分别在样本内/样本外跑回测（信号按时间切片，无未来函数）。"""
    ep = _exit_params_for(name)
    label = STRATEGY_LABEL.get(name, name)
    if name == "value":
        # 长线持有只在序列首根买入；分段评估时应在每段起点建仓，否则样本外永远是 0 收益
        is_sig = [1] + [0] * (split - 1)
        oos_sig = [1] + [0] * (len(bars) - split - 1)
    else:
        sig = strategy_signal(name, bars, params)
        is_sig, oos_sig = sig[:split], sig[split:]
    is_bt = run_backtest(bars[:split], is_sig, ep, label=label)
    oos_bt = run_backtest(bars[split:], oos_sig, ep, label=label)
    return is_bt, oos_bt


def _decide(tuned: bool, is_gain: float, oos_edge: float) -> tuple[bool, bool, str]:
    """采用规则：调参必须在样本外也多赚 0.5 个点以上，否则退回默认参数。

    返回 (是否采用调参, 是否判定过拟合, 说明)。
    """
    if not tuned:
        return False, False, "默认参数已是网格最优，不用调"
    if oos_edge > 0.5:
        return True, False, f"调参后样本外仍多赚 {oos_edge} 个点，采用"
    overfit = is_gain >= 2
    note = ("样本内看着更好、样本外没延续 → 判定过拟合，退回默认参数"
            if overfit else "调参优势不足以覆盖噪声，用默认参数")
    return False, overfit, note


def _candidate(name: str, bars: list[dict], split: int, score: float) -> dict:
    """对单个战法：默认参数 vs 网格最优参数，用样本外表现决定采用谁。"""
    label = STRATEGY_LABEL.get(name, name)
    default = _params(name, None)
    grid = PARAM_GRIDS.get(name) or [{}]

    d_is, d_oos = _segments(name, default, bars, split)

    best_params, best_is = default, d_is
    best_oos = d_oos
    for prm in grid:
        i_bt, o_bt = _segments(name, prm, bars, split)
        if i_bt.total_return > best_is.total_return:
            best_params, best_is, best_oos = prm, i_bt, o_bt

    tuned = best_params != default
    is_gain = round(best_is.total_return - d_is.total_return, 2)
    oos_edge = round(best_oos.total_return - d_oos.total_return, 2)

    use_tuned, overfit, note = _decide(tuned, is_gain, oos_edge)
    use, use_oos = (best_params, best_oos) if use_tuned else (default, d_oos)

    full = run_backtest(bars, strategy_signal(name, bars, use), _exit_params_for(name),
                        label=label)

    return {
        "name": name,
        "label": label,
        "score": score,
        "default_params": default,
        "chosen_params": use,
        "default_is_return": d_is.total_return,      # 样本内（默认参数）
        "is_return": best_is.total_return,           # 样本内（调参后最好）
        "default_oos_return": d_oos.total_return,    # 样本外（默认参数）
        "oos_return": round(use_oos.total_return, 2),
        "oos_edge": oos_edge,
        "is_gain": is_gain,
        "overfit": overfit,
        "trade_count": full.trade_count,
        "max_drawdown": full.max_drawdown,
        "total_return": full.total_return,
        "note": note,
    }


def run_playbook(
    code: str,
    name: str,
    bars: list[dict],
    period_label: str = "daily",
    top_n: int = 3,
) -> dict:
    """回测 + 寻优 + 样本外 + 买卖点，一条流水线给出最终结论。"""
    if not bars or len(bars) < MIN_BARS:
        return {"ok": False, "error": f"K线不足（{len(bars) if bars else 0} 根，需 ≥{MIN_BARS}）"}

    split = int(len(bars) * IS_RATIO)
    oos_len = len(bars) - split
    warnings: list[str] = []
    if oos_len < OOS_MIN_BARS:
        warnings.append(f"样本外只有 {oos_len} 根，结论偏弱（建议 ≥{OOS_MIN_BARS} 根）")
    if bars[-1]["date"] < bars[0]["date"]:
        warnings.append("K线顺序异常")

    analyses = analyze_strategies(bars)
    candidates = [
        _candidate(a.name, bars, split, a.score)
        for a in analyses[:top_n] if a.name
    ]
    if not candidates:
        return {"ok": False, "error": "没有可用的战法候选"}

    # 排序：样本外收益优先，同分看综合分
    candidates.sort(key=lambda c: (-c["oos_return"], -c["score"]))
    chosen = candidates[0]

    if chosen["trade_count"] < 3:
        warnings.append(f"最终战法历史交易仅 {chosen['trade_count']} 笔，统计上不可靠")
    if chosen["overfit"]:
        warnings.append(f"「{chosen['label']}」参数调优未通过样本外验证，已退回默认参数")

    plan = action_plan.build_action_plan(
        code=code,
        name=name,
        bars=bars,
        strategy=chosen["name"],
        params=chosen["chosen_params"],
        period_label=period_label,
    )

    rank = next(i for i, a in enumerate(analyses) if a.name == chosen["name"]) + 1
    why = (
        f"综合分第 {rank} 名；样本外 {chosen['oos_return']:+.2f}%"
        f"（默认参数 {chosen['default_oos_return']:+.2f}%）"
    )

    return {
        "ok": True,
        "code": code,
        "name": name,
        "period": period_label,
        "bars": len(bars),
        "last_date": bars[-1]["date"],
        "split": {"is_bars": split, "oos_bars": oos_len, "ratio": IS_RATIO},
        "candidates": candidates,
        "chosen": {
            "name": chosen["name"],
            "label": chosen["label"],
            "params": chosen["chosen_params"],
            "oos_return": chosen["oos_return"],
            "total_return": chosen["total_return"],
            "max_drawdown": chosen["max_drawdown"],
            "trade_count": chosen["trade_count"],
            "why": why,
        },
        "plan": plan,
        "warnings": warnings,
    }
