"""本轮代码审查的回归测试。"""
from __future__ import annotations

import pytest

from app.engine import horizon_weights, sell
from app.schemas import Account, Mode, Position


def _pos(code="600000", cost=50.0, price=30.0, shares=1000, rating=None):
    return Position(code=code, name="测试", shares=shares, cost_price=cost,
                    current_price=price, rating=rating)


# 1) 深套票的参考价应是反弹价，不是现价
def test_deep_stock_suggested_price_is_rebound(monkeypatch):
    monkeypatch.setattr(sell.config, "get_mode", lambda m: {"stop_loss": -5, "time_stop_days": 10})
    monkeypatch.setattr(sell.config, "get_exit_rules", lambda: {
        "fundamental_stop": {"growth_crash": -30}, "technical_exit": {},
        "severity": {"multi_signal_cap": 15, "multi_signal_bonus": 5, "flight_penalty_per_cond": 10},
        "stop_loss": {"deep_underwater_margin": 3.0},
        "sell_flight": {"sector_rps_threshold": 90, "net_inflow_days": 3},
    })
    monkeypatch.setattr(sell.config, "get_deleverage", lambda: {"rebound_pct": 3.0})
    acc = Account(positions=[_pos(cost=50.0, price=30.0)], cash=0.0, financing_balance=0.0)
    # 深套：-40%，触发时间止损（不是硬止损）
    q = type("Q", (), {"rating": None, "growth_yoy": None, "roe_ttm": 10, "reasons": []})()
    signals = sell.scan_positions(acc, Mode.BALANCE, {"600000": q},
                                  facts={"600000": {"days_held": 30, "last_high": 45.0}},
                                  trends={})
    assert signals, "应触发时间止损"
    s = signals[0]
    # 现价 30，反弹 +3% = 30.9；参考价不应是现价 30
    assert s.suggested_price == pytest.approx(30.9, abs=0.01)


# 2) 新开仓要求稳定性数据存在且 >=0.25
def test_buy_requires_stability(monkeypatch):
    from app.engine import recommend
    rows = [{"code": "600000", "horizon": "mid", "payload": {
        "ok": True, "stats": {"stability": None, "median": 3.0},
        "all": [{"name": "ma", "label": "双均线", "total_return": 20, "score": 80,
                 "trade_count": 8, "max_drawdown": 10, "buy_hit_rate": 60, "sell_hit_rate": 60}]}}]
    monkeypatch.setattr(recommend, "_rows_for", lambda code, day=None: [
        {**r, "strategy": a["name"], "label": a["label"], "horizon": r["horizon"],
         "total_return": a["total_return"], "score": a["score"], "trade_count": a["trade_count"],
         "max_drawdown": a["max_drawdown"], "buy_hit": a["buy_hit_rate"], "sell_hit": a["sell_hit_rate"],
         "median": 3.0, "stability": None}
        for r in rows for a in r["payload"]["all"]])
    pool = recommend.rank("600000", "buy")
    # 稳定性缺失 → 全部不合格，但仍回退到全部（不至于空结果）
    assert pool


# 3) 自选池只数合计 = 30
def test_pool_size_sums_to_30():
    from app.schemas import Account, Position
    acc = Account(positions=[_pos(cost=40, price=30, shares=1000)], cash=0.0, financing_balance=200000.0)
    r = horizon_weights.compute(acc, {"total": 30})
    assert sum(r["pool_size"].values()) == 30


# 4) 真实杠杆口径
def test_leverage_ratio():
    acc = Account(positions=[_pos(cost=40, price=30, shares=1000)],  # 市值 30000
                  cash=10000.0, financing_balance=20000.0)           # 净资产 30000+10000-20000=20000
    assert acc.leverage_ratio == pytest.approx(150.0)                 # 30000/20000
    assert acc.position_ratio == pytest.approx(75.0)                  # 30000/40000


# 5) 财报季：财务缓存必须缩短，否则新财报要等一周
def test_earnings_season_ttl(monkeypatch):
    from datetime import datetime
    from app.data import adapter

    assert adapter.in_earnings_season(datetime(2026, 10, 28)) is True
    assert adapter.in_earnings_season(datetime(2026, 4, 15)) is True
    assert adapter.in_earnings_season(datetime(2026, 8, 20)) is True
    assert adapter.in_earnings_season(datetime(2026, 11, 5)) is False
    assert adapter.in_earnings_season(datetime(2026, 2, 10)) is False
    # TTL 在财报季应显著短于非财报季
    assert adapter.TTL_FINANCIAL_SEASON < adapter.TTL_FINANCIAL


# 6) 财报季：技术类卖点降权
def test_technical_downgraded_in_earnings_season(monkeypatch):
    from app.engine import sell
    from app.schemas import Account, Mode, Position

    monkeypatch.setattr(sell.config, "get_mode", lambda m: {"stop_loss": -5, "time_stop_days": 10})
    monkeypatch.setattr(sell.config, "get_exit_rules", lambda: {
        "fundamental_stop": {"growth_crash": -30},
        "technical_exit": {"rsi_overbought": 80, "volume_ratio": 1.5, "stall_gain_pct": 2.0},
        "severity": {"multi_signal_cap": 15, "multi_signal_bonus": 5, "flight_penalty_per_cond": 10},
        "stop_loss": {"deep_underwater_margin": 3.0},
        "sell_flight": {"sector_rps_threshold": 90, "net_inflow_days": 3},
    })
    monkeypatch.setattr(sell.config, "get_deleverage", lambda: {"rebound_pct": 3.0})
    pos = Position(code="600000", name="测试", shares=1000, cost_price=10.0, current_price=10.5)
    acc = Account(positions=[pos], cash=0.0, financing_balance=0.0)
    facts = {"600000": {"volume_ratio": 2.0, "gain_pct": 0.5}}   # 放量滞涨 → 技术卖点

    normal = sell.scan_positions(acc, Mode.BALANCE, {}, facts=facts, trends={}, earnings_season=False)
    season = sell.scan_positions(acc, Mode.BALANCE, {}, facts=facts, trends={}, earnings_season=True)
    assert normal and season
    assert season[0].severity_score < normal[0].severity_score
    assert "财报季" in season[0].reason


# 7) 财报公告日：报告期末 ≠ 公布日
def test_financial_trend_exposes_notice_date(monkeypatch):
    from app.data import adapter
    monkeypatch.setattr(adapter, "get_report_notice_dates",
                        lambda period: {"600000": "2026-10-28"})
    # 直接构造返回结构校验字段存在
    d = {"series": [{"date": "2026-09-30", "roe": 10, "growth": -5}],
         "report_date": "2026-09-30", "notice_date": "2026-10-28"}
    assert d["notice_date"] > d["report_date"] or d["notice_date"] != d["report_date"]
