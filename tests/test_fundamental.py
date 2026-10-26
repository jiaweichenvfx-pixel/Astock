"""基本面趋势：连续恶化 / 改善 / 平稳 的判定。"""
from __future__ import annotations

import pytest

from app.engine import fundamental


def _series(growths, roes=None):
    roes = roes or [10.0] * len(growths)
    return {"series": [{"date": f"202{i}-12-31", "roe": r, "growth": g}
                       for i, (g, r) in enumerate(zip(growths, roes))],
            "latest": {"date": "2025-12-31", "roe": roes[-1], "growth": growths[-1]},
            "consecutive_up": 0, "consecutive_down": 0, "report_date": "2025-12-31"}


def _stub(monkeypatch, data, up=0, down=0):
    monkeypatch.setattr(fundamental.adapter, "get_earnings_forecast", lambda codes: {})
    if data:
        data["consecutive_up"] = up
        data["consecutive_down"] = down
    monkeypatch.setattr(fundamental.adapter, "get_financial_trend", lambda code, periods=8: data)


def test_deteriorating(monkeypatch):
    _stub(monkeypatch, _series([72, 27, -8, -74, -196]), down=4)
    r = fundamental.assess("601127")
    assert r["trend"] == "deteriorating"
    assert r["score_delta"] < 0
    assert "连续" in r["reason"]


def test_weakening_but_positive(monkeypatch):
    _stub(monkeypatch, _series([80, 60, 45, 32]), down=3)
    r = fundamental.assess("300059")
    assert r["trend"] == "weakening"
    assert r["latest_growth"] > 0


def test_improving(monkeypatch):
    _stub(monkeypatch, _series([-20, -5, 10, 25]), up=3)
    r = fundamental.assess("600489")
    assert r["trend"] == "improving"
    assert r["score_delta"] > 0


def test_flat(monkeypatch):
    _stub(monkeypatch, _series([20, 21, 19, 20]), up=0, down=0)
    assert fundamental.assess("600000")["trend"] == "flat"


def test_no_data(monkeypatch):
    _stub(monkeypatch, None)
    r = fundamental.assess("01810")
    assert r["trend"] == "unknown"
    assert r["score_delta"] == 0


def test_scan_sorts_deteriorating_first(monkeypatch):
    data = {"600000": ("deteriorating", 3), "600001": ("flat", 0), "600002": ("improving", 2)}
    def fake(code, periods=8):
        trend, n = data[code]
        d = _series([10, 5, -5] if trend == "deteriorating" else [1, 2, 3])
        d["consecutive_down"] = n if trend == "deteriorating" else 0
        d["consecutive_up"] = n if trend == "improving" else 0
        return d
    monkeypatch.setattr(fundamental.adapter, "get_financial_trend", fake)
    res = fundamental.scan(list(data))
    assert res["deteriorating"] == ["600000"]
    assert res["improving"] == ["600002"]
    assert res["rows"][0]["code"] == "600000"


# ── 业绩预告 ─────────────────────────────────────────────────────
def test_forecast_positive_future_period(monkeypatch):
    _stub(monkeypatch, _series([20, 21, 22, 23]), up=0)
    monkeypatch.setattr(fundamental.adapter, "get_earnings_forecast",
                        lambda codes: {"600000": {"period": "20991231", "type": "预增",
                                                  "change_pct": 50.0, "notice_date": "2026-10-20"}})
    r = fundamental.assess("600000")
    assert r["forecast"]["period"] == "20991231"
    assert r["forecast"].get("positive") is True


def test_forecast_loss_warns_early(monkeypatch):
    _stub(monkeypatch, _series([20, 22, 24, 26]), up=0)
    monkeypatch.setattr(fundamental.adapter, "get_earnings_forecast",
                        lambda codes: {"600000": {"period": "20991231", "type": "首亏",
                                                  "change_pct": -180.0, "notice_date": "2026-10-20"}})
    r = fundamental.assess("600000")
    assert r["trend"] == "deteriorating"
    assert "业绩预告" in r["reason"]


def test_forecast_conflict_is_flagged(monkeypatch):
    series = _series([10, 8, 6, 5])
    series["series"][-1]["date"] = "2025-12-31"
    _stub(monkeypatch, series, down=3)
    monkeypatch.setattr(fundamental.adapter, "get_earnings_forecast",
                        lambda codes: {"600000": {"period": "20251231", "type": "预增",
                                                  "change_pct": 88.5, "notice_date": "2026-07-15"}})
    r = fundamental.assess("600000")
    assert r["forecast"]["conflict"] is True
    assert "不符" in r["forecast"]["note"]
