"""统一战法推荐：同一份回测，按用途加权。"""
from __future__ import annotations

import pytest

from app.engine import recommend


def _payload(ok=True, v=3):
    all_ = [
        {"name": "bollinger", "label": "布林带", "total_return": 30, "score": 85,
         "trade_count": 10, "max_drawdown": 8, "buy_hit_rate": 70, "sell_hit_rate": 72},
        {"name": "grid", "label": "网格做T", "total_return": 20, "score": 80,
         "trade_count": 40, "max_drawdown": 5, "buy_hit_rate": 78, "sell_hit_rate": 80},
        {"name": "value", "label": "长线持有", "total_return": 60, "score": 70,
         "trade_count": 1, "max_drawdown": 30, "buy_hit_rate": 50, "sell_hit_rate": 50},
    ]
    return {"ok": ok, "v": v, "stats": {"stability": 0.42, "median": 5.0}, "all": all_}


@pytest.fixture()
def stub(monkeypatch):
    rows = [{"code": "600000", "horizon": h, "payload": _payload()} for h in ("short", "mid", "long")]
    monkeypatch.setattr(recommend, "_rows_for", lambda code, day=None: [
        {**r, "strategy": a["name"], "label": a["label"], "horizon": r["horizon"],
         "total_return": a["total_return"], "score": a["score"], "trade_count": a["trade_count"],
         "max_drawdown": a["max_drawdown"], "buy_hit": a["buy_hit_rate"], "sell_hit": a["sell_hit_rate"],
         "median": 5.0, "stability": 0.42}
        for r in rows for a in r["payload"]["all"]])


def test_hold_prefers_composite(stub):
    r = recommend.recommend("600000", "hold")
    assert r["ok"] and r["strategy"] == "bollinger"     # 综合分最高


def test_trade_prefers_high_frequency_hit(stub):
    r = recommend.recommend("600000", "trade")
    assert r["strategy"] == "grid"                       # 次数多、命中高
    assert any("命中" in x for x in r["reasons"])


def test_buy_requires_stability(stub):
    r = recommend.recommend("600000", "buy")
    assert r["ok"]
    # 长线持有只有 1 笔，不满足 >=4 笔，应被排除
    assert r["strategy"] != "value"


def test_purposes_can_differ(stub):
    hold = recommend.recommend("600000", "hold")["strategy"]
    trade = recommend.recommend("600000", "trade")["strategy"]
    assert hold != trade            # 同一份数据，用途不同 → 答案不同


def test_empty_data():
    r = recommend.recommend("999999", "hold")
    assert r["ok"] is False
