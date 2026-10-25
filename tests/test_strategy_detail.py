"""战法详情：K线切片、历史成交、下一步触发价。"""
from __future__ import annotations

import pytest

from app import api
from app.data import adapter


def _bars(n=160):
    """造一段有涨有跌的行情，保证唐奇安会开平仓。"""
    out, price = [], 10.0
    for i in range(n):
        # 前 60 根上涨、中间 40 根回落、后面再涨
        if i < 60:
            price *= 1.012
        elif i < 100:
            price *= 0.988
        else:
            price *= 1.010
        out.append({"date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                    "open": price * 0.995, "high": price * 1.01,
                    "low": price * 0.99, "close": price, "volume": 1000})
    return out


@pytest.fixture()
def fake(monkeypatch):
    monkeypatch.setattr(adapter, "get_stock_history",
                        lambda code, days=0, period="daily": _bars())
    monkeypatch.setattr(adapter, "get_quotes",
                        lambda codes: {c: {"name": "测试股"} for c in codes})


def test_detail_shape(fake):
    d = api.strategy_detail(code="600000", strategy="donchian", horizon="mid", bars=100)
    assert d["code"] == "600000" and d["name"] == "测试股"
    assert d["strategy"] == "donchian" and d["label"]
    assert len(d["bars"]) == 100                     # 只回最近 100 根
    assert d["bars"][-1]["date"] > d["bars"][0]["date"]
    for k in ("total_return", "win_rate", "max_drawdown", "trade_count"):
        assert k in d["summary"]
    assert d["plan"]["ok"] is True


def test_trades_are_within_window(fake):
    d = api.strategy_detail(code="600000", strategy="donchian", horizon="mid", bars=60)
    start = d["bars"][0]["date"]
    for t in d["trades"]:
        assert str(t["buy_date"]) >= str(start)


def test_plan_has_next_triggers(fake):
    d = api.strategy_detail(code="600000", strategy="ma", horizon="mid", bars=60)
    plan = d["plan"]
    assert plan["last_close"] > 0
    assert plan["action"]["type"] in ("hold", "buy_now", "sell_now", "wait_buy", "none")
    assert set(plan) >= {"action", "buy", "sell", "stop_loss", "advice", "levels"}


def test_unknown_horizon_rejected(fake):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        api.strategy_detail(code="600000", strategy="ma", horizon="weeklyy")
    assert e.value.status_code == 400


def test_short_history_rejected(monkeypatch):
    monkeypatch.setattr(adapter, "get_stock_history",
                        lambda code, days=0, period="daily": _bars(10))
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        api.strategy_detail(code="600000", strategy="ma", horizon="mid")
    assert e.value.status_code == 404
