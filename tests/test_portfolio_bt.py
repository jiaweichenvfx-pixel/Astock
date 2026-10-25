"""持仓回测持久化：按 日期×代码×周期 落库、只补缺失、不重复计算。"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.data import db
from app.engine import bt_store, portfolio_bt


class _FakeAnalysis:
    def __init__(self, i):
        self.name = f"s{i}"
        self.label = f"战法{i}"
        self.total_return = 100 - i * 10
        self.score = 90 - i
        self.trade_count = 3 + i
        self.max_drawdown = 5.0 + i
        self.buy_hit_rate = 50.0
        self.sell_hit_rate = 50.0


def _bars(n=60):
    return [{"date": f"2026-01-{i+1:02d}", "open": 10, "high": 10.5, "low": 9.5,
             "close": 10 + i * 0.1, "volume": 1000} for i in range(n)]


@pytest.fixture()
def fake_env(monkeypatch):
    calls = {"n": 0}

    def fake_analyze(bars, exit_for=None):
        calls["n"] += 1
        return [_FakeAnalysis(i) for i in range(1, 9)]

    monkeypatch.setattr(bt_store, "analyze_strategies", fake_analyze)
    monkeypatch.setattr(bt_store.adapter, "get_stock_history",
                        lambda code, days=0, period="daily": _bars(60))
    monkeypatch.setattr(bt_store, "HORIZONS", {
        "short": {"label": "短线", "period_label": "60分钟", "days": 200, "period": "60"},
        "mid": {"label": "中线", "period_label": "日线", "days": 320, "period": "daily"},
        "long": {"label": "长线", "period_label": "周线", "days": 300, "period": "weekly"},
    })
    yield calls


POS = [{"code": "600000", "name": "测试股"}]
# 用最近几天，避免被 prune_bt(90 天) 清掉
D1 = date.today().isoformat()
D2 = (date.today() - timedelta(days=1)).isoformat()
D3 = (date.today() - timedelta(days=2)).isoformat()
D4 = (date.today() - timedelta(days=3)).isoformat()
D5 = (date.today() - timedelta(days=4)).isoformat()
D6 = (date.today() - timedelta(days=5)).isoformat()


def test_compute_one_persists_all_strategies(fake_env):
    bt_store.compute_one("600000", "mid", D1)
    rows = db.load_bt(D1, "600000", "mid")
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["ok"] is True
    assert len(payload["all"]) == 8          # 全部战法都存下来
    assert payload["all"][0]["total_return"] == 90


def test_second_call_does_not_recompute(fake_env):
    bt_store.ensure(POS, D2)
    first = fake_env["n"]
    assert first == 3                        # 3 个周期各算一次

    bt_store.ensure(POS, D2)       # 再来一次
    assert fake_env["n"] == first            # 一次都没重算


def test_ensure_only_fills_missing(fake_env):
    bt_store.compute_one("600000", "mid", D3)
    stats = bt_store.ensure(POS, D3)
    assert stats["computed"] == 2            # short + long
    assert stats["reused"] == 1              # mid 复用


def test_load_day_trims_to_top5(fake_env):
    portfolio_bt.ensure_today(POS, today=D4)
    data = portfolio_bt.get(POS, today=D4)
    assert data["status"] == "ready"
    for h in data["items"][0]["horizons"].values():
        assert len(h["top"]) == 5
        assert "all" not in h


def test_short_history_is_stored_as_failure(fake_env, monkeypatch):
    monkeypatch.setattr(bt_store.adapter, "get_stock_history",
                        lambda code, days=0, period="daily": _bars(5))
    bt_store.compute_one("600001", "mid", D5)
    payload = db.load_bt(D5, "600001", "mid")[0]["payload"]
    assert payload["ok"] is False
    assert "K线不足" in payload["reason"]


def test_force_recomputes(fake_env):
    portfolio_bt.ensure_today(POS, today=D6)
    before = fake_env["n"]
    portfolio_bt.ensure_today(POS, today=D6, force=True)
    assert fake_env["n"] == before + 3       # 清掉后重算 3 个周期


def test_available_dates_listed(fake_env):
    portfolio_bt.ensure_today(POS, today=D1)
    data = portfolio_bt.get(POS, today=D1)
    assert D1 in data["available_dates"]
