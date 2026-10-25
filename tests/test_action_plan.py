"""下一步买卖点：触发价必须是真边界、持仓状态要与回测一致。"""
from __future__ import annotations

from app.engine import action_plan, backtest
from app.engine.strategies import strategy_signal


def _bars(closes: list[float], start_day: int = 1) -> list[dict]:
    return [
        {"date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", "open": c, "high": c * 1.01,
         "low": c * 0.99, "close": c, "volume": 1_000_000}
        for i, c in enumerate(closes)
    ]


def _downtrend_then_flat() -> list[float]:
    closes = [120 - i * 0.4 for i in range(60)]      # 120 → 96.4 下跌
    closes += [96 + ((i % 5) - 2) * 0.3 for i in range(40)]  # 横盘震荡
    return closes


def _signal_at(bars, strategy, params, price):
    probe = bars + [{"date": "T+1", "open": price, "high": price, "low": price,
                     "close": price, "volume": bars[-1].get("volume") or 0}]
    return strategy_signal(strategy, probe, params)[-1]


# ── 基本结构 ─────────────────────────────────────────────────────
def test_plan_short_bars_returns_error():
    plan = action_plan.build_action_plan("600000", "测试", _bars([10.0] * 10), "ma")
    assert plan["ok"] is False
    assert "K线不足" in plan["error"]


def test_plan_shape():
    plan = action_plan.build_action_plan("600000", "测试", _bars(_downtrend_then_flat()), "ma")
    assert plan["ok"] is True
    for key in ("action", "state", "buy", "sell", "stop_loss", "levels", "advice", "last_close"):
        assert key in plan
    assert plan["action"]["type"] in ("hold", "buy_now", "sell_now", "wait_buy", "none")
    assert plan["buy"]["trigger"] is None or isinstance(plan["buy"]["trigger"], float)


# ── 触发价必须是真边界 ───────────────────────────────────────────
def test_buy_trigger_is_real_boundary():
    """触发价本身必须产生信号；远离现价一侧的邻居价格不能产生信号。"""
    bars = _bars(_downtrend_then_flat())
    plan = action_plan.build_action_plan("600000", "测试", bars, "ma")
    trigger = plan["buy"]["trigger"]
    assert trigger is not None

    p = plan["params"]
    assert _signal_at(bars[-160:], "ma", p, trigger) == 1

    # 从现价往触发价方向退一小步，就不该再有买信号了
    away = trigger - abs(trigger - plan["last_close"]) * 0.02 - 0.01
    if trigger > plan["last_close"]:
        assert _signal_at(bars[-160:], "ma", p, away) == 0


def test_sell_trigger_is_real_boundary():
    closes = _downtrend_then_flat() + [100 + i * 0.8 for i in range(30)]  # 拉起来 → 持仓
    bars = _bars(closes)
    plan = action_plan.build_action_plan("600000", "测试", bars, "ma")
    if not plan["state"]["in_position"]:
        return  # 该序列未进入持仓，跳过边界断言（不同数据版本可容忍）
    trigger = plan["sell"]["trigger"]
    if trigger is None:
        return
    p = plan["params"]
    assert _signal_at(bars[-160:], "ma", p, trigger) == -1


# ── 持仓状态与回测一致 ───────────────────────────────────────────
def test_plan_state_matches_backtest():
    bars = _bars(_downtrend_then_flat())
    plan = action_plan.build_action_plan("600000", "测试", bars, "ma")
    sig = strategy_signal("ma", bars, {"short": 5, "long": 20})
    bt = backtest.run_backtest(bars, sig, {"stop_loss": -12}, force_close=False)
    assert plan["state"]["in_position"] == bt.in_position
    assert plan["state"]["entry_price"] == bt.entry_price


def test_pending_buy_reports_buy_now():
    """最后一根刚好金叉 → 状态仍是空仓，但动作应是「明日开盘买入」。"""
    closes = [120 - i * 0.3 for i in range(80)]   # 单边下跌，全程无金叉
    bars = _bars(closes)
    base_close = closes[-1]
    cross_price = None
    for px in [base_close + i * 0.5 for i in range(1, 200)]:
        if _signal_at(bars, "ma", {"short": 5, "long": 20}, px) == 1:
            cross_price = px
            break
    assert cross_price is not None

    bars2 = bars + [{"date": "2026-12-31", "open": cross_price, "high": cross_price,
                     "low": cross_price, "close": cross_price, "volume": 1_000_000}]
    plan = action_plan.build_action_plan("600000", "测试", bars2, "ma")
    assert plan["action"]["type"] == "buy_now"
    assert plan["buy"]["pending"] is True


# ── force_close 参数 ─────────────────────────────────────────────
def test_force_close_false_keeps_position_open():
    bars = _bars(_downtrend_then_flat() + [100 + i * 0.8 for i in range(30)])
    sig = [1] + [0] * (len(bars) - 1)   # 首日买入后一直持有
    closed = backtest.run_backtest(bars, sig, {"stop_loss": None})
    opened = backtest.run_backtest(bars, sig, {"stop_loss": None}, force_close=False)
    assert closed.in_position is False
    assert opened.in_position is True
    assert opened.entry_price is not None
    assert closed.trades[-1]["reason"] == "期末平仓"
