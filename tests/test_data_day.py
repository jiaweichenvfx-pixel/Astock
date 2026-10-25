"""数据日期：必须跟随 A 股交易日，周末/节假日回退到上一个交易日。"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.data import adapter


@pytest.fixture()
def calendar(monkeypatch):
    # 2026-10-19(一) ~ 2026-10-23(五) 是交易日，24/25 周末休市，26(一) 开市
    days = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26"]
    monkeypatch.setattr(adapter, "get_trade_days", lambda: days)
    return days


def test_weekend_uses_friday(calendar):
    assert adapter.data_day(datetime(2026, 10, 25, 17, 0)) == "2026-10-23"


def test_saturday_uses_friday(calendar):
    assert adapter.data_day(datetime(2026, 10, 24, 10, 0)) == "2026-10-23"


def test_trading_day_after_close(calendar):
    assert adapter.data_day(datetime(2026, 10, 23, 16, 0)) == "2026-10-23"


def test_preopen_uses_previous_session(calendar):
    assert adapter.data_day(datetime(2026, 10, 26, 9, 0)) == "2026-10-23"


def test_intraday_uses_today(calendar):
    assert adapter.data_day(datetime(2026, 10, 26, 10, 30)) == "2026-10-26"


def test_no_calendar_falls_back_to_weekday(monkeypatch):
    monkeypatch.setattr(adapter, "get_trade_days", lambda: None)
    assert adapter.data_day(datetime(2026, 10, 25, 12, 0)) == "2026-10-23"   # 周日 → 周五


def test_engines_use_data_day(monkeypatch):
    """抽查几个引擎：默认日期不再是 date.today()，而是 data_day()。"""
    monkeypatch.setattr(adapter, "data_day", lambda: "2026-10-23")
    from app.engine import watchpool
    from app.engine import portfolio_bt

    r = watchpool.daily_update({"short": [], "mid": [], "long": []})
    assert r["date"] == "2026-10-23"
    assert portfolio_bt.get([], None)["date"] == "2026-10-23"


def test_next_trade_day(calendar):
    from datetime import date
    assert adapter.next_trade_day(date(2026, 10, 25)) == "2026-10-26"   # 周日 → 周一
    assert adapter.next_trade_day(date(2026, 10, 23)) == "2026-10-26"   # 周五 → 下周一


def test_todo_uses_next_trading_day_when_closed(calendar, monkeypatch):
    monkeypatch.setattr(adapter, "is_trading_day", lambda d=None: False)
    monkeypatch.setattr(adapter, "next_trade_day", lambda after=None: "2026-10-26")
    from app.engine import todo

    s = {"account": {"position_ratio": 95, "total_assets": 1e6, "positions": [
            {"code": "600000", "name": "测试", "shares": 1000,
             "current_price": 10.0, "market_value": 10000}]},
         "gate": {"total_max": 70, "can_buy": False, "blockers": ["仓位超上限"]},
         "sell": {"red": [], "yellow": [], "orange": [
             {"code": "600000", "name": "测试", "level": "orange",
              "reason": "深套：反弹至 10.5 元（+3%）再减仓"}]},
         "buy": {}, "meta": {"as_of": "2026-10-25 10:00"}}
    t = todo.build_todo(s)
    assert t["trading_day"] is False
    assert t["next_trading_day"] == "2026-10-26"
    assert "2026-10-26" in t["items"][0]["action"]
    assert "今日休市" in t["text"]


def test_todo_keeps_today_wording_when_open(calendar, monkeypatch):
    monkeypatch.setattr(adapter, "is_trading_day", lambda d=None: True)
    from app.engine import todo

    s = {"account": {"position_ratio": 95, "total_assets": 1e6, "positions": [
            {"code": "600000", "name": "测试", "shares": 1000,
             "current_price": 10.0, "market_value": 10000}]},
         "gate": {"total_max": 70, "can_buy": False, "blockers": []},
         "sell": {"red": [], "yellow": [], "orange": [
             {"code": "600000", "name": "测试", "level": "orange", "reason": "基本面止损"}]},
         "buy": {}, "meta": {"as_of": "2026-10-23 15:00"}}
    t = todo.build_todo(s)
    assert t["trading_day"] is True
    assert "今日内挂单卖出" in t["items"][0]["action"]
