"""自选池（短/中/长）：入选、剔除、进出历史、事后验证。"""
from __future__ import annotations

import pytest

from app.data import db
from app.engine import watchpool


@pytest.fixture(autouse=True)
def _clean_pools():
    # 测试库是临时库，但仍清干净，避免用例间串扰
    conn = db._conn()
    conn.execute("DELETE FROM pool_members")
    conn.execute("DELETE FROM pool_history")
    conn.commit()
    conn.close()
    yield


def _bars(closes):
    return [{"date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
             "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1000}
            for i, c in enumerate(closes)]


def _screener():
    return {
        "short": [{"code": "600001", "name": "短A", "price": 10.0, "score": 90, "reasons": ["放量"]},
                  {"code": "600002", "name": "短B", "price": 20.0, "score": 85, "reasons": ["强势"]}],
        "mid": [{"code": "600003", "name": "中A", "price": 30.0, "score": 70, "reasons": ["站上20日线"]}],
        "long": [{"code": "600004", "name": "长A", "price": 40.0, "score": 60, "reasons": ["ROE优"]}],
    }


def test_add_and_list():
    watchpool.add("short", "600001", "短A", "2026-10-25", 10.0, 90.0, "放量")
    members = watchpool.list_members("short", "active")
    assert len(members) == 1
    assert members[0]["code"] == "600001" and members[0]["entry_score"] == 90.0


def test_remove_appends_history(monkeypatch):
    watchpool.add("short", "600001", "短A", "2026-10-25", 10.0, 90.0, "放量")
    watchpool.remove("short", "600001", "2026-10-26", 11.0, "跌破MA10")
    assert watchpool.list_members("short", "active") == []
    hist = watchpool.history()
    assert len(hist) == 1
    assert hist[0]["return_pct"] == 10.0       # (11/10 - 1) * 100
    assert hist[0]["hit"] == 1


def test_review_exits_below_ma(monkeypatch):
    # 短线看 MA10：需要 ≥25 根；前 24 根 20 元，最后一根大跌到 15 → 跌破 MA10
    closes = [20.0] * 24 + [15.0]
    monkeypatch.setattr(watchpool.adapter, "get_stock_history",
                        lambda code, days=0, period="daily": _bars(closes))
    res = watchpool._review("short", "600001")
    assert res is not None
    assert "跌破MA10" in res[1]


def test_review_keeps_healthy(monkeypatch):
    closes = [20.0 + i * 0.3 for i in range(30)]
    monkeypatch.setattr(watchpool.adapter, "get_stock_history",
                        lambda code, days=0, period="daily": _bars(closes))
    assert watchpool._review("short", "600001") is None


def test_daily_update_fills_pools(monkeypatch):
    monkeypatch.setattr(watchpool.adapter, "get_stock_history",
                        lambda code, days=0, period="daily": _bars([10.0 + i * 0.1 for i in range(40)]))
    r = watchpool.daily_update(_screener(), today="2026-10-25")
    assert {a[1] for a in r["added"]} == {"600001", "600002", "600003", "600004"}
    assert r["pools"]["short"] == 2 and r["pools"]["mid"] == 1 and r["pools"]["long"] == 1


def test_daily_update_no_duplicate(monkeypatch):
    monkeypatch.setattr(watchpool.adapter, "get_stock_history",
                        lambda code, days=0, period="daily": _bars([10.0 + i * 0.1 for i in range(40)]))
    watchpool.daily_update(_screener(), today="2026-10-25")
    r2 = watchpool.daily_update(_screener(), today="2026-10-26")
    assert r2["added"] == []                     # 已进池的不会再进


def test_evaluate_aggregates(monkeypatch):
    watchpool.add("short", "600001", "短A", "2026-10-20", 10.0, 90.0, "放量")
    watchpool.remove("short", "600001", "2026-10-22", 12.0, "出")   # +20% 命中
    watchpool.add("short", "600002", "短B", "2026-10-20", 20.0, 85.0, "强势")
    watchpool.remove("short", "600002", "2026-10-22", 18.0, "出")   # -10% 未命中
    ev = watchpool.evaluate()
    assert ev["overall"]["count"] == 2
    assert ev["overall"]["win_rate"] == 50.0
    assert ev["overall"]["avg_return"] == 5.0
    assert ev["by_pool"]["short"]["count"] == 2


def test_active_codes_shape(monkeypatch):
    watchpool.add("mid", "600003", "中A", "2026-10-25", 30.0, 70.0, "站上20日线")
    codes = watchpool.active_codes()
    assert {"code": "600003", "name": "中A"} in codes


# ── 市场环境闸门：下跌市暂停补充短线票 ─────────────────────────────
def test_down_market_pauses_short_entries(monkeypatch):
    monkeypatch.setattr(watchpool.adapter, "get_stock_history",
                        lambda code, days=0, period="daily": _bars([10.0 + i * 0.1 for i in range(40)]))
    r = watchpool.daily_update(_screener(), today="2026-10-25",
                               regime={"regime": "down", "label": "下跌市"})
    pools = {a[0] for a in r["added"]}
    assert "short" not in pools, "下跌市不应新增短线票"
    assert "mid" in pools or "long" in pools
    assert r["market"]["paused_pools"] == ["short"]


def test_up_market_allows_short_entries(monkeypatch):
    monkeypatch.setattr(watchpool.adapter, "get_stock_history",
                        lambda code, days=0, period="daily": _bars([10.0 + i * 0.1 for i in range(40)]))
    r = watchpool.daily_update(_screener(), today="2026-10-26",
                               regime={"regime": "up", "label": "上涨市"})
    assert "short" in {a[0] for a in r["added"]}
    assert r["market"]["paused_pools"] == []
