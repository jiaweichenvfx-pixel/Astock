"""净值曲线与杠杆：回撤计算、回溯估算、真实账户末点。"""
from __future__ import annotations

import pytest

from app.data import db
from app.engine import equity


def test_max_drawdown_basic():
    series = [
        {"date": "2026-01-01", "net_assets": 100},
        {"date": "2026-01-02", "net_assets": 120},
        {"date": "2026-01-03", "net_assets": 90},
        {"date": "2026-01-04", "net_assets": 110},
    ]
    dd, peak, trough = equity._max_drawdown(series)
    assert dd == pytest.approx(-25.0)      # 120 → 90
    assert peak == "2026-01-02" and trough == "2026-01-03"


def test_max_drawdown_monotonic_up():
    series = [{"date": f"d{i}", "net_assets": 100 + i * 10} for i in range(5)]
    dd, _, _ = equity._max_drawdown(series)
    assert dd == 0.0


def test_estimate_series_uses_history(monkeypatch):
    bars = [{"date": "2026-01-01", "close": 10.0, "open": 10, "high": 10, "low": 10, "volume": 1},
            {"date": "2026-01-02", "close": 12.0, "open": 12, "high": 12, "low": 12, "volume": 1}]
    monkeypatch.setattr(equity.adapter, "get_stock_history", lambda code, days=0, period="daily": bars)
    monkeypatch.setattr(equity.adapter, "is_hk_code", lambda c: False)

    out = equity._estimate_series([{"code": "600000", "shares": 100}], cash=1000, financing=0, days=2)
    assert len(out) == 2
    assert out[0]["net_assets"] == 2000.0        # 100×10 + 1000
    assert out[1]["net_assets"] == 2200.0        # 100×12 + 1000


def test_estimate_series_converts_hk(monkeypatch):
    bars = [{"date": "2026-01-01", "close": 100.0, "open": 100, "high": 100, "low": 100, "volume": 1}]
    monkeypatch.setattr(equity.adapter, "get_stock_history", lambda code, days=0, period="daily": bars)
    monkeypatch.setattr(equity.adapter, "is_hk_code", lambda c: c == "01810")
    import app.config as app_config
    monkeypatch.setattr(app_config, "get_fx", lambda: {"hkd_cny": 0.85})

    out = equity._estimate_series([{"code": "01810", "shares": 100}], cash=0, financing=0, days=1)
    assert out[0]["net_assets"] == pytest.approx(8500.0)   # 100×100×0.85


def test_curve_prefers_real_account_values(monkeypatch):
    test_date = "2001-01-01"          # 用一个远古日期，避免污染真实快照
    db.delete_equity_snapshot(test_date)
    db.save_equity_snapshot(test_date, {"total_assets": 1000, "net_assets": 800,
                                           "position_value": 900, "cash": 100, "financing": 200,
                                           "position_ratio": 90, "financing_ratio": 20, "pnl_ratio": 0})
    account = {"total_assets": 1200, "net_assets": 900, "position_value": 1100, "cash": 100,
               "financing_balance": 300, "financing_ratio": 25, "positions": []}
    try:
        r = equity.curve(days=30, account=account)
    finally:
        db.delete_equity_snapshot(test_date)
    assert r["metrics"]["net_now"] == 900
    assert r["metrics"]["leverage_now"] == 25
    assert r["metrics"]["level"] in ("ok", "warn", "danger")
