"""选股因子回测：短/中/长三套独立系统。"""
from __future__ import annotations

import pytest

from app.engine import factor


def _series(n, drift=0.004):
    closes, vols, c = [], [], 100.0
    for i in range(n):
        c *= 1 + drift
        closes.append(c)
        vols.append(1000 + i % 3 * 100)
    return closes, vols


def _bars(closes, vols):
    return [{"date": f"d{i}", "open": c, "high": c, "low": c, "close": c,
             "volume": vols[i]} for i, c in enumerate(closes)]


def _spot(n=40):
    return {f"6000{i:02d}": {"name": f"股{i}", "turnover": 1e9 + i, "price": 50,
                             "turnover_rate": 5.0, "market_cap": 1e11 - i}
            for i in range(n)}


def _patch(monkeypatch, n=320, drift=0.004):
    closes, vols = _series(n, drift)
    monkeypatch.setattr(factor.adapter, "get_stock_history",
                        lambda code, days=0, period="daily": _bars(closes, vols))
    monkeypatch.setattr(factor.adapter, "get_spot_snapshot_cached",
                        lambda max_age=3600: _spot())


def test_three_systems_have_distinct_config():
    assert set(factor.HORIZONS) == {"short", "mid", "long"}
    assert factor.SYSTEMS["short"]["rebalance"] == 2
    assert factor.SYSTEMS["mid"]["rebalance"] == 5
    assert factor.SYSTEMS["long"]["rebalance"] == 20
    assert factor.SYSTEMS["short"]["forward"] == 5
    assert factor.SYSTEMS["long"]["forward"] == 60
    # 策略集合各不同
    assert set(factor.SYSTEMS["short"]["strategies"]) != set(factor.SYSTEMS["long"]["strategies"])
    # 长线需要更多预热
    assert factor.SYSTEMS["long"]["lookback"] > factor.SYSTEMS["mid"]["lookback"]


def test_short_features():
    closes, vols = _series(50)
    f = factor._features_short(closes, vols, 30)
    assert f is not None
    assert f["pct"] > 0 and f["chg5"] > 0
    assert f["streak"] >= 1


def test_long_features_need_250():
    closes, vols = _series(300)
    assert factor._features_long(closes, vols, 100) is None   # < 250 预热
    assert factor._features_long(closes, vols, 260) is not None
    assert factor._features_long(closes, vols, 260)["above_ma250"] is True


def test_rank_strategies_mid(monkeypatch):
    _patch(monkeypatch, 320)
    r = factor.rank_strategies("mid")
    assert r["horizon"] == "mid" and r["label"] == "中线"
    assert r["forward_days"] == 20 and r["rebalance_days"] == 5
    assert r["baseline_avg"] is not None
    labels = [s["strategy"] for s in r["strategies"]]
    assert "trend" in labels and "mom20" in labels


def test_rank_strategies_short(monkeypatch):
    _patch(monkeypatch, 120)
    r = factor.rank_strategies("short")
    assert r["forward_days"] == 5 and r["rebalance_days"] == 2
    assert "hot_money" in [s["strategy"] for s in r["strategies"]]


def test_pick_today(monkeypatch):
    _patch(monkeypatch, 320)
    picks = factor.pick_today("mid", "mom20", top_k=5)
    assert len(picks) == 5
    assert picks[0]["code"].startswith("6000")
    assert picks[0]["score"] >= picks[-1]["score"]


def test_pick_today_unknown_strategy(monkeypatch):
    _patch(monkeypatch, 320)
    assert factor.pick_today("mid", "不存在的策略", top_k=5) == []


def test_best_strategy_falls_back(monkeypatch):
    monkeypatch.setattr(factor, "get_rank", lambda h: {"strategies": []})
    assert factor.best_strategy("mid") == "combo_mid"
