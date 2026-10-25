"""融资担保比例与平仓风险。"""
from __future__ import annotations

import pytest

from app.engine import margin
from app.schemas import Account, Position


def _acc(credit_value=1000.0, ordinary_value=2000.0, credit_cash=0.0, debt=400.0):
    return Account(
        positions=[
            Position(code="600000", name="信用票", shares=100,
                     cost_price=10.0, current_price=credit_value / 100, account_type="信用"),
            Position(code="600001", name="普通票", shares=100,
                     cost_price=20.0, current_price=ordinary_value / 100, account_type="普通"),
        ],
        cash=0.0, financing_balance=debt, credit_cash=credit_cash,
    )


def test_ratio_ignores_ordinary_account():
    """普通账户的钱不救信用账户，合并算会虚高。"""
    acc = _acc(credit_value=1000, ordinary_value=2000, debt=400)
    assert acc.credit_position_value == 1000
    assert acc.maintenance_ratio == 250.0        # 1000 / 400
    # 若错误地把普通账户算进去会是 3000/400 = 750%
    assert acc.maintenance_ratio != 750.0


def test_ratio_includes_credit_cash():
    acc = _acc(credit_value=1000, credit_cash=200, debt=400)
    assert acc.maintenance_ratio == 300.0        # (1000+200)/400


def test_no_margin_means_no_risk():
    acc = _acc(debt=0)
    r = margin.assess(acc)
    assert r["has_margin"] is False
    assert r["status"] == "none"
    assert r["drop_to_warning"] is None


def test_safe_status_and_drop_distances(monkeypatch):
    monkeypatch.setattr(margin, "rules", lambda: {"warning": 150.0, "liquidation": 130.0})
    acc = _acc(credit_value=2000, debt=1000)      # 200%
    r = margin.assess(acc)
    assert r["status"] == "safe"
    assert r["drop_to_warning"] == pytest.approx(25.0)     # 1 - 150/200
    assert r["drop_to_liquidation"] == pytest.approx(35.0)  # 1 - 130/200


def test_warning_status(monkeypatch):
    monkeypatch.setattr(margin, "rules", lambda: {"warning": 150.0, "liquidation": 130.0})
    acc = _acc(credit_value=1400, debt=1000)      # 140%
    r = margin.assess(acc)
    assert r["status"] == "warning"
    assert "警戒线" in r["note"]


def test_danger_status(monkeypatch):
    monkeypatch.setattr(margin, "rules", lambda: {"warning": 150.0, "liquidation": 130.0})
    acc = _acc(credit_value=1200, debt=1000)      # 120%
    r = margin.assess(acc)
    assert r["status"] == "danger"
    assert "强平" in r["note"]


def test_warm_status(monkeypatch):
    monkeypatch.setattr(margin, "rules", lambda: {"warning": 150.0, "liquidation": 130.0})
    acc = _acc(credit_value=1800, debt=1000)      # 180%
    r = margin.assess(acc)
    assert r["status"] == "warm"
    assert r["drop_to_warning"] == pytest.approx(16.7, abs=0.1)


def test_rules_read_from_config():
    r = margin.rules()
    assert r["warning"] > r["liquidation"] > 0
