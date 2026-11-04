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
