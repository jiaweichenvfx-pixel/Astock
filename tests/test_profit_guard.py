"""盈利保护监控。"""
from __future__ import annotations

import pytest

from app.engine import profit_guard


def test_state_lock_on_overbought():
    assert profit_guard._state(35.0, rsi=80, j=105, divergence=False, dd=-5)[0] == "lock"


def test_state_lock_on_divergence():
    assert profit_guard._state(25.0, rsi=60, j=50, divergence=True, dd=-8)[0] == "lock"


def test_state_lock_on_giveback():
    assert profit_guard._state(30.0, rsi=50, j=40, divergence=False, dd=-18)[0] == "lock"


def test_state_near_without_enough_profit():
    # 超买但浮盈不够 → 只观察，不喊卖出
    assert profit_guard._state(8.0, rsi=82, j=110, divergence=False, dd=-2)[0] == "near"


def test_state_weak_when_profit_erodes():
    s = profit_guard._state(12.0, rsi=25, j=0, divergence=False, dd=-32)
    assert s[0] == "weak"
    assert "回撤" in s[1]


def test_state_ok():
    assert profit_guard._state(15.0, rsi=50, j=50, divergence=False, dd=-3)[0] == "ok"


def test_oversold_does_not_lock():
    """RSI 超卖时不该喊锁利润——那是反弹起点，不是卖点。"""
    st, why = profit_guard._state(23.2, rsi=27, j=6, divergence=False, dd=-31.66)
    assert st == "weak"
    assert "超卖" in why


def test_drawdown_stop_skipped_when_oversold(monkeypatch):
    from app.engine import sell
    from app.schemas import Position
    monkeypatch.setattr(sell.config, "get_exit_rules", lambda: {
        "drawdown_stop": {"crash_pct": -30.0, "give_back_pct": -15.0, "profit_gate": 10.0}})
    pos = Position(code="600489", name="中金黄金", shares=100, cost_price=16.53,
                   current_price=20.36, rating=None)
    assert sell.check_drawdown_stop(pos, -32.0, rsi=27) is None      # 超卖：不喊崩塌
    assert sell.check_drawdown_stop(pos, -32.0, rsi=55) is not None  # 正常：该喊


# 黄金股附带金价上下文
def test_gold_context(monkeypatch):
    from app.engine import profit_guard
    from app.schemas import Account, Position

    monkeypatch.setattr(profit_guard.adapter, "is_gold_proxy", lambda c: str(c) == "600489")
    monkeypatch.setattr(profit_guard.adapter, "get_gold_snapshot", lambda: {
        "sge": 829.49, "comex": 3867.8, "comex_overnight_pct": -0.51,
        "sge_chg5": -5.85, "sge_dd_from_high": -6.18,
        "verdict": "隔夜 COMEX 跌 -0.51%，黄金股今日大概率承压"})
    monkeypatch.setattr(profit_guard.adapter, "get_stock_history",
                        lambda code, days=0: [{"date": f"2026-10-{i:02d}", "close": 10 + i * 0.1,
                                               "high": 11 + i * 0.1, "low": 10, "volume": 1} for i in range(1, 29)])

    acc = Account(positions=[Position(code="600489", name="中金黄金", shares=100,
                                      cost_price=14.0, current_price=19.0)],
                  cash=0.0, financing_balance=0.0)
    r = profit_guard.scan(acc)
    assert r["gold"] and r["gold"]["sge"] == 829.49
    assert r["rows"][0]["is_gold"] is True
