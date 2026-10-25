"""卖飞防御三条件 + 今日待办 + 信号复盘的单元测试。"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.data import db
from app.engine import journal, sell, todo
from app.schemas import SellLevel


# ── 卖飞防御：三条件都是真数据了 ────────────────────────────────
def test_annual_breakout_detects_fresh_cross():
    from app import pipeline

    # 先造 250 根在 250 日线下方的 K 线，最后一根放量上穿
    hist = [{"date": f"d{i}", "close": 10.0, "volume": 1000} for i in range(250)]
    hist += [{"date": "d250", "close": 9.9, "volume": 1000},
             {"date": "d251", "close": 10.5, "volume": 3000}]
    assert pipeline._annual_breakout(hist) is True

    quiet = [{"date": f"d{i}", "close": 11.0, "volume": 1000} for i in range(252)]
    assert pipeline._annual_breakout(quiet) is False


def test_rps_uses_percentile_when_universe_large():
    from app import pipeline

    universe = list(range(0, 1000))       # 0..999
    assert pipeline._rps_60d(950, universe) == pytest.approx(95.0, abs=0.2)
    assert pipeline._rps_60d(500, universe) == pytest.approx(50.0, abs=0.2)
    assert pipeline._rps_60d(None, universe) is None


def test_rps_falls_back_to_bands_when_universe_small():
    from app import pipeline

    assert pipeline._rps_60d(45, [1, 2]) == 95.0
    assert pipeline._rps_60d(5, [1, 2]) == 50.0
    assert pipeline._rps_60d(-30, [1, 2]) == 15.0


def test_flight_conditions_are_graded():
    """满足一条也应该有帮助（不再是全中才生效）。"""
    ok, reasons = sell.check_flight_defense(95, None, False)
    assert ok is False and len(reasons) == 1
    assert "RPS60" in reasons[0]

    ok2, reasons2 = sell.check_flight_defense(95, 3, True)
    assert ok2 is True and len(reasons2) == 3


# ── 今日待办 ────────────────────────────────────────────────────
def _summary(with_red=True, can_buy=False):
    return {
        "account": {
            "total_assets": 1_000_000,
            "position_ratio": 90,
            "positions": [
                {"code": "600000", "name": "浦发银行", "shares": 10000,
                 "current_price": 10.0, "market_value": 100000},
                {"code": "600001", "name": "测试股", "shares": 2000,
                 "current_price": 50.0, "market_value": 100000},
            ],
        },
        "gate": {"total_max": 60, "can_buy": can_buy, "blockers": ["仓位超限"]},
        "sell": {
            "red": ([{"code": "600000", "name": "浦发银行", "level": "red",
                      "severity_score": 85, "reason": "硬止损", "suggested_price": 10.0}]
                    if with_red else []),
            "orange": [{"code": "600001", "name": "测试股", "level": "orange",
                        "severity_score": 70, "reason": "时间止损", "suggested_price": 50.0}],
            "yellow": [],
        },
        "buy": {"strong": [], "watch": []},
        "meta": {"as_of": "2026-10-23 15:00:00"},
    }


def test_todo_orders_sells_first():
    t = todo.build_todo(_summary())
    assert t["items"][0]["action"] == "开盘市价卖出"
    assert t["items"][1]["action"] == "今日内挂单卖出"
    assert t["counts"]["sell"] == 2


def test_todo_allocates_by_position_need():
    t = todo.build_todo(_summary())
    # 需要把仓位从 90% 降到 60% → 回笼 30 万
    assert t["position"]["need_cash"] == pytest.approx(300000, rel=0.01)
    shares = [i["shares"] for i in t["items"] if i["kind"] == "sell"]
    assert shares[0] == 10000          # 第一只全卖（10 万）
    assert shares[1] == 2000           # 第二只全卖（10 万）
    assert t["text"]
    assert "仓位：90%" in t["text"]


def test_todo_blocks_buy_when_position_over_limit():
    t = todo.build_todo(_summary(can_buy=False))
    assert any(i["kind"] == "block" for i in t["items"])


def test_todo_handles_no_signals():
    s = _summary(with_red=False)
    s["sell"]["orange"] = []
    t = todo.build_todo(s)
    assert t["counts"]["sell"] == 0
    assert t["items"][0]["kind"] == "block"


# ── 信号复盘 ────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _pin_trading_day(monkeypatch):
    """测试与真实日历解耦：默认当成交易日（否则周末跑测试文案会变）。"""
    from app.data import adapter
    monkeypatch.setattr(adapter, "is_trading_day", lambda d=None: True)


@pytest.fixture()
def clean_signals():
    db.clear_signals()
    yield
    db.clear_signals()


def test_record_from_summary_writes_rows(clean_signals):
    summary = _summary()
    n = journal.record_from_summary(summary, today="2026-10-23")
    assert n == 2
    rows = db.list_signals("2026-10-23", "2026-10-23")
    assert {r["code"] for r in rows} == {"600000", "600001"}
    assert all(r["side"] == "sell" for r in rows)


def test_record_is_idempotent(clean_signals):
    journal.record_from_summary(_summary(), today="2026-10-23")
    journal.record_from_summary(_summary(), today="2026-10-23")
    assert len(db.list_signals("2026-10-23", "2026-10-23")) == 2


def test_review_hit_logic(clean_signals, monkeypatch):
    """卖出信号之后跌了 = 命中；涨了 = 卖飞。"""
    old = (date.today() - timedelta(days=10)).isoformat()
    db.save_signal(old, "600000", "浦发银行", "sell", "red", 85, "硬止损", 10.0)
    db.save_signal(old, "600001", "测试股", "sell", "orange", 70, "时间止损", 50.0)

    d0 = (date.today() - timedelta(days=12)).isoformat()
    d1 = (date.today() - timedelta(days=5)).isoformat()
    # 信号日必须在中间，后面还要有 1 根 K 线才能算 T+1
    bars_down = [{"date": d0, "close": 10.0}, {"date": old, "close": 10.0}, {"date": d1, "close": 9.0}]
    bars_up = [{"date": d0, "close": 50.0}, {"date": old, "close": 50.0}, {"date": d1, "close": 55.0}]
    monkeypatch.setattr(journal.adapter, "get_stock_history",
                        lambda code, days=120: bars_down if code == "600000" else bars_up)

    r = journal.review(days=30, horizon=1)
    assert r["sell"]["count"] == 2
    assert r["sell"]["hit_rate"] == 50.0
    detail = {d["code"]: d for d in r["recent"]}
    assert detail["600000"]["hit"] is True
    assert detail["600001"]["hit"] is False


# ── 待办：深套反弹价 + 差额补足 + 同级别排序 ────────────────────
def test_todo_uses_rebound_limit_price():
    s = _summary()
    s["sell"]["red"][0]["reason"] = "硬止损；深套：反弹至 43.81 元（+3.0%）再减仓，不追低割肉"
    t = todo.build_todo(s)
    item = next(i for i in t["items"] if i["code"] == "600000")
    assert item["limit_price"] == 43.81
    assert "43.81" in item["action"]


def test_todo_sorts_same_level_by_market_value():
    s = _summary()
    s["account"]["positions"] = [
        {"code": "600001", "name": "小仓位", "shares": 100, "current_price": 10.0, "market_value": 1000},
        {"code": "600002", "name": "大仓位", "shares": 5000, "current_price": 10.0, "market_value": 50000},
    ]
    s["sell"]["red"] = [
        {"code": "600001", "name": "小仓位", "level": "red", "severity_score": 80, "reason": "x"},
        {"code": "600002", "name": "大仓位", "level": "red", "severity_score": 80, "reason": "y"},
    ]
    s["sell"]["orange"] = []
    t = todo.build_todo(s)
    sells = [i for i in t["items"] if i["kind"] == "sell"]
    assert sells[0]["code"] == "600002"          # 大仓位先动


def test_todo_reports_gap_when_sells_are_not_enough():
    s = _summary()
    # 只有一只 1 万的小仓位要卖，但需要回笼 30 万
    s["account"]["positions"] = [
        {"code": "600001", "name": "小仓位", "shares": 1000, "current_price": 10.0, "market_value": 10000},
        {"code": "600002", "name": "大仓位", "shares": 10000, "current_price": 10.0, "market_value": 100000},
    ]
    s["sell"]["red"] = [{"code": "600001", "name": "小仓位", "level": "red",
                         "severity_score": 80, "reason": "硬止损"}]
    s["sell"]["orange"] = []
    t = todo.build_todo(s)
    gap = [i for i in t["items"] if i["kind"] == "gap"]
    assert gap, "卖点不够时应该给出差额建议"
    assert gap[0]["code"] == "600002"            # 从最大持仓里补
    assert gap[0]["shares"] > 0
    assert t["gap"]["code"] == "600002"


def test_todo_no_gap_item_when_no_sells():
    s = _summary(with_red=False)
    s["sell"]["orange"] = []
    t = todo.build_todo(s)
    assert not [i for i in t["items"] if i["kind"] == "gap"]
