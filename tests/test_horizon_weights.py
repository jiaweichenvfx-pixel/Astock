"""战法周期权重：融资盘自动偏短线。"""
from __future__ import annotations

from app.engine import horizon_weights as hw
from app.schemas import Account, Position


def _acc(fin=0.0, net=1_000_000.0, cost=10.0, price=10.0, shares=30_000):
    a = Account(positions=[Position(code="600000", name="测试", shares=shares,
                                    cost_price=cost, current_price=price)],
                cash=0.0, financing_balance=fin)
    return a


def test_no_margin_uses_base_weights():
    a = _acc(fin=0)
    r = hw.compute(a, {"total": 80}, rate=0.065)
    w = r["weights"]
    assert w["short"] < w["long"] + 30          # 无融资时长线不被过度压缩
    assert abs(sum(w.values()) - 100) < 1
    assert r["daily_interest"] == 0


def test_margin_shifts_to_short():
    a = _acc(fin=220_000)
    r = hw.compute(a, {"total": 80})
    w = r["weights"]
    # 融资 22 万 / 净资产约 100 万 → 短线权重上升、长线下降
    assert w["short"] > w["long"]
    assert w["short"] > 40
    assert r["daily_interest"] > 0
    assert r["annual_interest"] > 0


def test_low_health_shifts_more_to_short():
    a = _acc(fin=220_000)
    healthy = hw.compute(a, {"total": 85})["weights"]
    weak = hw.compute(a, {"total": 25})["weights"]
    assert weak["short"] > healthy["short"]
    assert weak["long"] < healthy["long"]


def test_pool_sizes_follow_weights():
    a = _acc(fin=220_000)
    r = hw.compute(a, {"total": 30})
    ps = r["pool_size"]
    assert ps["short"] >= ps["mid"] >= ps["long"]
    assert all(6 <= v <= 16 for v in ps.values())


def test_break_even_and_hold_cost():
    a = _acc(fin=220_000)
    r = hw.compute(a, {"total": 60}, rate=0.065)
    assert r["break_even_annual_pct"] > 0
    assert r["hold_cost"]["60"] > r["hold_cost"]["20"]
    assert r["reasons"]      # 至少有「融资拖累」这条原因
