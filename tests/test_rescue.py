"""解套降本：回本距离、做 T 股数、区间、计划判定。"""
from __future__ import annotations

from app.engine import rescue
from app.schemas import Account, Position


def _acc(cost=60.0, price=42.0, shares=900, fin=0.0):
    return Account(positions=[Position(code="601127", name="赛力斯", shares=shares,
                                       cost_price=cost, current_price=price)],
                   cash=0.0, financing_balance=fin)


def _bars(n=40, base=42.0, amp=1.0):
    out = []
    for i in range(n):
        c = base + (i % 5 - 2) * amp
        out.append({"date": f"d{i}", "open": c, "high": c * 1.02, "low": c * 0.98,
                    "close": c, "volume": 1000})
    return out


def test_loss_and_gap():
    r = rescue.analyze(_acc(), {}, use_network=False)
    i = r["items"][0]
    assert i["loss_pct"] == round((42 / 60 - 1) * 100, 2)
    assert i["gap_pct"] == round((60 / 42 - 1) * 100, 2)
    assert i["loss_amount"] == round((60 - 42) * 900, 0)


def test_t_shares_is_lot_rounded():
    r = rescue.analyze(_acc(shares=900), {}, use_network=False)
    i = r["items"][0]
    assert i["t_shares"] == 200          # 900*0.3=270 → 200
    assert i["t_shares"] % 100 == 0


def test_small_position_cannot_t():
    r = rescue.analyze(_acc(shares=300), {}, use_network=False)
    i = r["items"][0]
    assert i["t_shares"] == 0
    assert i["plan"] == "仓位不足"


def test_profitable_position_marked():
    r = rescue.analyze(_acc(cost=20.0, price=26.0), {}, use_network=False)
    assert r["items"][0]["plan"] == "已盈利"


def test_pressured_position_should_reduce_not_t():
    r = rescue.analyze(_acc(), {"red": [{"code": "601127", "name": "赛力斯"}]}, use_network=False)
    i = r["items"][0]
    assert i["pressured"] is True
    assert i["plan"] == "优先减仓"


def test_zones_from_range(monkeypatch):
    from app.data import adapter
    monkeypatch.setattr(adapter, "get_stock_history", lambda code, days=0: _bars())
    r = rescue.analyze(_acc(), {}, use_network=True)
    i = r["items"][0]
    assert i["buy_zone"] is not None and i["sell_zone"] is not None
    assert i["buy_zone"] < i["sell_zone"]


def test_summary_projection(monkeypatch):
    from app.data import adapter
    monkeypatch.setattr(adapter, "get_stock_history", lambda code, days=0: _bars())
    r = rescue.analyze(_acc(), {}, use_network=True)
    s = r["summary"]
    assert s["loss_total"] > 0
    assert s["annual_t_gain"] >= 0
    assert s["loss_after_1y"] <= s["loss_total"]
    assert 0 <= s["coverage_pct"] <= 100
    assert s["verdict"]
