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
