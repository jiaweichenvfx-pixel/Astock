"""精选选股：短线/长线推荐打分、条件过滤、数据源降级提示。"""
from __future__ import annotations

from app.data import adapter
from app.engine import screener

BASE = {
    "code": "600000", "name": "浦发银行", "price": 10.0, "pct_chg": 3.0,
    "turnover": 5e8, "volume_ratio": 2.0, "turnover_rate": 5.0, "chg_60d": 10.0,
}


# ── 短线：过滤条件 ───────────────────────────────────────────────
def test_short_pick_basic():
    pick = screener._short_pick(dict(BASE))
    assert pick is not None
    assert pick["score"] > 0


def test_short_pick_excludes_st():
    assert screener._short_pick({**BASE, "name": "ST某某"}) is None
    assert screener._short_pick({**BASE, "name": "退市某某"}) is None


def test_short_pick_excludes_bse():
    assert screener._short_pick({**BASE, "code": "830001"}) is None
    assert screener._short_pick({**BASE, "code": "920045"}) is None


def test_short_pick_excludes_illiquid():
    assert screener._short_pick({**BASE, "turnover": 3e7}) is None


def test_short_pick_limit_threshold_is_board_aware():
    """主板 10% 涨停买不进；创业板 20% 涨停，+9.9% 仍然可做。"""
    assert screener._short_pick({**BASE, "code": "600001", "pct_chg": 9.95}) is None
    assert screener._short_pick({**BASE, "code": "300001", "pct_chg": 9.95}) is not None
    assert screener._short_pick({**BASE, "code": "300001", "pct_chg": 19.95}) is None


def test_short_pick_rewards_volume_and_penalises_overheat():
    strong = screener._short_pick({**BASE, "volume_ratio": 2.5, "pct_chg": 5.0})
    weak = screener._short_pick({**BASE, "volume_ratio": 1.0, "pct_chg": -2.0})
    assert strong["score"] > weak["score"]


# ── 短线：历史补算 ───────────────────────────────────────────────
def test_history_metrics_computes_ratio_and_trend():
    closes = [10 + i * 0.1 for i in range(80)]
    vols = [1000] * 79 + [3000]
    bars = [{"date": f"d{i}", "open": c, "high": c, "low": c, "close": c, "volume": v}
            for i, (c, v) in enumerate(zip(closes, vols))]
    m = screener._history_metrics(bars)
    assert m["vol_ratio"] == 3.0
    assert m["above_ma20"] is True
    assert m["chg5"] is not None and m["chg5"] > 0


def test_history_metrics_needs_enough_bars():
    assert screener._history_metrics([{"date": "d", "close": 10, "volume": 1}]) is None


# ── 长线：财务打分 ───────────────────────────────────────────────
def test_long_pick_prefers_high_roe():
    good = screener._long_pick(dict(BASE), {"roe_ttm": 20, "growth_yoy": 15}, "卓越")
    weak = screener._long_pick(dict(BASE), {"roe_ttm": 5, "growth_yoy": 1}, "卓越")
    assert good["score"] > weak["score"]


def test_long_pick_penalises_extreme_growth():
    normal = screener._long_pick(dict(BASE), {"roe_ttm": 20, "growth_yoy": 30}, "卓越")
    extreme = screener._long_pick(dict(BASE), {"roe_ttm": 20, "growth_yoy": 700}, "卓越")
    assert extreme["score"] < normal["score"]
    assert any("异常" in r for r in extreme["reasons"])


def test_long_pick_excludes_st():
    assert screener._long_pick({**BASE, "name": "ST某某"}, None, "卓越") is None


# ── 扫描编排 ─────────────────────────────────────────────────────
def _fake_spot():
    return {
        "600000": {"name": "浦发银行", "price": 10.0, "pct_chg": 3.0, "turnover": 5e8,
                   "volume_ratio": None, "turnover_rate": None, "pe": None, "pb": None,
                   "market_cap": None, "chg_60d": None},
        "300001": {"name": "特锐德", "price": 20.0, "pct_chg": 6.0, "turnover": 8e8,
                   "volume_ratio": None, "turnover_rate": None, "pe": None, "pb": None,
                   "market_cap": None, "chg_60d": None},
    }


def test_scan_market_shape_and_degraded_flag(monkeypatch):
    monkeypatch.setattr(adapter, "get_spot_snapshot", lambda force=False: _fake_spot())
    monkeypatch.setattr(adapter, "get_financial_indicators", lambda code: None)
    monkeypatch.setattr(adapter, "get_stock_history", lambda code, days=0, period="daily": None)
    monkeypatch.setattr(adapter, "get_tencent_quotes", lambda codes: {})   # 补源也挂掉 → 全降级

    r = screener.scan_market()
    assert r["stocks_total"] == 2
    assert "short" in r and "long" in r
    # 快照缺量比 → 应标记数据源降级
    assert r["data_quality"]["degraded"] is True
    assert r["data_quality"]["note"]
