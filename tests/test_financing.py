"""融资成本：月息、打平线、还款方案。"""
from __future__ import annotations

from datetime import date

import pytest

from app.engine import financing
from app.schemas import Account, Position


def _acc(fin=200000.0, price=10.0, shares=10000, cash=20000.0):
    return Account(positions=[Position(code="600000", name="测试", shares=shares,
                                       cost_price=10.0, current_price=price)],
                   cash=cash, financing_balance=fin)


def test_monthly_annual_daily():
    r = financing.assess(_acc(fin=120000), rate=0.06)
    assert r["monthly"] == pytest.approx(600, abs=1)
    assert r["annual"] == pytest.approx(7200, abs=1)
    assert r["daily"] == pytest.approx(19.7, abs=0.5)


def test_breakeven_on_financing_is_rate():
    r = financing.assess(_acc(fin=100000), rate=0.065)
    assert r["breakeven_on_financing_pct"] == pytest.approx(6.5)


def test_repay_options_shrink_monthly():
    r = financing.assess(_acc(fin=200000), rate=0.065)
    opts = [o for o in r["options"] if o["repay"] < 200000]
    assert opts and opts[0]["monthly_after"] < r["monthly"]
    # 还得越多，省得越多
    savings = [o["annual_saving"] for o in opts]
    assert savings == sorted(savings)


def test_no_financing():
    r = financing.assess(_acc(fin=0))
    assert r["monthly"] == 0
    assert not r["options"]          # 无负债时不给还款方案
    assert "无融资" in r["verdict"]


def test_payment_countdown(monkeypatch):
    r = financing.assess(_acc(fin=100000))
    assert r["days_to_payment"] >= 0
    assert r["next_payment_date"]
