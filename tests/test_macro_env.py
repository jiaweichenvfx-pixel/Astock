"""外围宏观环境评分测试。"""
from __future__ import annotations

from app.engine import macro_env


def test_score_macro_empty_neutral():
    r = macro_env.score_macro({})
    assert r["rating"] == "neutral"
    assert 0 <= r["score"] <= 10


def test_score_macro_risk_on():
    data = {
        "us_stock": {"close": 5000, "ma20": 4900, "above_ma20": True, "chg_60d": 8.0},
        "gold": {"latest": 600, "chg_60d": -8.0},
        "fed_rate": {"latest": 3.5, "prev": 4.0},
        "us_cpi": {"latest": 1.8},
    }
    r = macro_env.score_macro(data)
    assert r["rating"] == "risk_on"
    assert r["score"] >= 6.5


def test_score_macro_risk_off():
    data = {
        "us_stock": {"close": 4800, "ma20": 5000, "above_ma20": False, "chg_60d": -10.0},
        "gold": {"latest": 800, "chg_60d": 15.0},
        "fed_rate": {"latest": 5.0, "prev": 4.5},
        "us_cpi": {"latest": 5.5},
    }
    r = macro_env.score_macro(data)
    assert r["rating"] == "risk_off"
    assert r["score"] <= 3.5
    assert r["advice"]
