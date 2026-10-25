"""买点体检：位置分级 + 基本面一票否决。"""
from __future__ import annotations

import pytest

from app.engine import buypoint


def _bars(n=300, rising=True):
    out, c = [], 20.0
    for i in range(n):
        c = c * (1.01 if rising else 0.99)
        out.append({"date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                    "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1000})
    return out


def _patch(monkeypatch, bars, fund="flat"):
    from app.data import adapter
    monkeypatch.setattr(adapter, "get_stock_history", lambda code, days=0: bars)
    monkeypatch.setattr(buypoint, "fundamental_assess",
                        lambda code: {"trend": fund, "label": fund, "reason": "test",
                                      "latest_growth": 5.0})


def test_high_position_is_warning(monkeypatch):
    _patch(monkeypatch, _bars(rising=True))
    r = buypoint.assess("600000", "测试")
    assert r["ok"]
    assert r["position"] > 85
    assert r["grade"] == "D"


def test_low_position_with_ma60_is_best(monkeypatch):
    bars = _bars(300, rising=False)          # 一路下跌
    for b in bars[-5:]:                      # 最后几天反弹站上 MA60
        b["close"] *= 1.25; b["high"] *= 1.25
    _patch(monkeypatch, bars)
    r = buypoint.assess("600000", "测试")
    assert r["position"] < 15
    assert r["grade"] in ("A", "B")


def test_fundamental_veto(monkeypatch):
    bars = _bars(300, rising=False)
    _patch(monkeypatch, bars, fund="deteriorating")
    r = buypoint.assess("600000", "测试")
    assert r["grade"] == "X"
    assert "基本面恶化" in r["label"]


def test_weakening_downgrades(monkeypatch):
    bars = _bars(300, rising=False)
    _patch(monkeypatch, bars, fund="flat")
    base = buypoint.assess("600000", "测试")["grade"]
    _patch(monkeypatch, bars, fund="weakening")
    weak = buypoint.assess("600000", "测试")["grade"]
    assert "降级" in buypoint.assess("600000", "测试")["label"]
    assert weak != base


def test_scan_sorts_and_counts(monkeypatch):
    _patch(monkeypatch, _bars(300, rising=False))
    res = buypoint.scan(["600000", "600001"], {})
    assert res["rows"]
    orders = {"A": 0, "B": 1, "C": 2, "D": 3, "X": 4}
    grades = [orders[r["grade"]] for r in res["rows"]]
    assert grades == sorted(grades)
    assert set(res["counts"]) == set("ABCDX")
