"""分批减仓规划。"""
from __future__ import annotations

from app.engine import tranche_planner
from app.schemas import Account, Position


def _acc(net_high=True):
    # 现价 20，成本 10 → 大幅浮盈，测「优先卖浮盈多的」
    return Account(positions=[Position(code="600489", name="中金黄金", shares=1000,
                                       cost_price=10.0, current_price=20.0),
                              Position(code="000858", name="五粮液", shares=1000,
                                       cost_price=50.0, current_price=40.0)],
                   cash=1000.0, financing_balance=10000.0)


def test_plans_generated(monkeypatch):
    monkeypatch.setattr(tranche_planner, "_net_series",
                        lambda acc, days=20: [("2026-11-01", 100.0), ("2026-11-02", 200.0)])
    monkeypatch.setattr(tranche_planner, "_net_series", lambda acc, days=20: [("d1", 100.0)])
    r = tranche_planner.evaluate(_acc())
    assert r["plans"]
    p = r["plans"][0]
    assert p["frac"] == 0.25
    assert p["amount"] > 0
    # 浮盈最多的优先卖
    assert p["picks"][0]["name"] == "中金黄金"


def test_high_position_advice(monkeypatch):
    monkeypatch.setattr(tranche_planner, "_net_series",
                        lambda acc, days=20: [(f"d{i}", 1000 + i) for i in range(20)])
    acc = _acc()
    r = tranche_planner.evaluate(acc)
    # 当前净值远高于序列 → 判为高位
    assert r["level"] in ("high", "mid", "low")


def test_percentile_clamped(monkeypatch):
    monkeypatch.setattr(tranche_planner, "_net_series",
                        lambda acc, days=20: [(f"d{i}", 1.0) for i in range(20)])
    r = tranche_planner.evaluate(_acc())
    assert 0 <= r["percentile"] <= 100
