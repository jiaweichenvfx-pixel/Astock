"""市场概况（顶部信息条）单元测试：交易时段判定 + 涨跌家数统计。"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.data import adapter
from app.engine import market_info


# ── 交易时段 ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "hhmm, expected",
    [
        ("08:30", ("pre", "盘前")),
        ("09:20", ("auction", "集合竞价")),
        ("10:00", ("open", "交易中")),
        ("11:45", ("noon", "午间休市")),
        ("14:00", ("open", "交易中")),
        ("15:30", ("post", "已收盘")),
    ],
)
def test_session_labels(hhmm, expected):
    h, m = map(int, hhmm.split(":"))
    assert market_info._session(datetime(2026, 10, 21, h, m), trading_day=True) == expected


def test_session_closed_on_holiday():
    assert market_info._session(datetime(2026, 10, 21, 10, 0), trading_day=False) == ("closed", "休市")


# ── 涨跌比描述 ───────────────────────────────────────────────────
@pytest.mark.parametrize(
    "ratio, label",
    [(3.0, "普涨"), (1.5, "涨多跌少"), (1.0, "涨跌均衡"), (0.6, "跌多涨少"), (0.2, "普跌")],
)
def test_breadth_label(ratio, label):
    assert market_info._breadth_label(ratio) == label


def test_breadth_label_none():
    assert market_info._breadth_label(None) == "暂无数据"


# ── 涨跌家数统计 ─────────────────────────────────────────────────
def test_market_breadth_counts(monkeypatch):
    fake = {
        "600000": {"name": "浦发银行", "pct_chg": 10.0, "turnover": 1e9},   # 主板涨停
        "300001": {"name": "特锐德", "pct_chg": 19.9, "turnover": 1e9},     # 创业板涨停
        "300002": {"name": "神州泰岳", "pct_chg": 5.0, "turnover": 1e9},    # 上涨
        "000001": {"name": "平安银行", "pct_chg": -10.0, "turnover": 1e9},  # 主板跌停
        "600001": {"name": "*ST 某某", "pct_chg": -5.0, "turnover": 1e9},   # ST 跌停
        "600002": {"name": "停牌股", "pct_chg": 0.0, "turnover": 0},
        "600003": {"name": "无数据", "pct_chg": None, "turnover": 1e9},
    }
    monkeypatch.setattr(adapter, "get_spot_snapshot", lambda force=False: fake)

    b = adapter.get_market_breadth()
    assert b["up"] == 3
    assert b["down"] == 2
    assert b["flat"] == 1
    assert b["limit_up"] == 2
    assert b["limit_down"] == 2
    assert b["total"] == 6
    assert b["adv_decl_ratio"] == round(3 / 2, 3)
    assert b["turnover_yi"] == 50.0  # 5 只 × 10 亿 = 50 亿（pct_chg 为 None 的不计入）


def test_build_market_info_shape(monkeypatch):
    monkeypatch.setattr(adapter, "is_trading_day", lambda d=None: True)
    monkeypatch.setattr(adapter, "latest_trade_day", lambda d=None: "2026-10-21")
    monkeypatch.setattr(
        adapter, "get_market_breadth",
        lambda fetch=True, max_age=1800: {"total": 10, "up": 7, "down": 3, "flat": 0, "limit_up": 1, "limit_down": 0,
                 "adv_decl_ratio": 2.333, "up_ratio": 70.0, "turnover_yi": 12345, "avg_pct": 1.2},
    )
    monkeypatch.setattr(
        adapter, "get_index_snapshot",
        lambda: [{"code": "sh000001", "name": "上证指数", "price": 3713.03,
                  "chg": -22.34, "pct": -0.598, "amount_yi": 6428}],
    )

    info = market_info.build_market_info(datetime(2026, 10, 21, 17, 30, 5))
    assert info["date"] == "2026-10-21"
    assert info["weekday"] == "周三"
    assert info["session"] == "post"
    assert info["session_label"] == "已收盘"
    assert info["is_trading_day"] is True
    assert info["indices"][0]["name"] == "上证指数"
    assert info["breadth"]["adv_decl_ratio"] == 2.333
    assert info["breadth"]["label"] == "普涨"
    assert info["turnover_yi"] == 12345


def test_build_market_info_handles_missing_data(monkeypatch):
    """数据源全挂时不抛异常，字段降级为 None。"""
    monkeypatch.setattr(adapter, "is_trading_day", lambda d=None: False)
    monkeypatch.setattr(adapter, "latest_trade_day", lambda d=None: None)
    monkeypatch.setattr(adapter, "get_market_breadth", lambda **kw: {})
    monkeypatch.setattr(adapter, "get_index_snapshot", lambda: [])

    info = market_info.build_market_info(datetime(2026, 10, 21, 10, 0, 0))
    assert info["session"] == "closed"
    assert info["breadth"]["adv_decl_ratio"] is None
    assert info["indices"] == []
    assert info["turnover_yi"] is None


def test_breadth_cache_only_never_fetches(monkeypatch):
    """fetch=False 只读缓存：绝不应触发全市场抓取。"""
    def _boom(*a, **kw):
        raise AssertionError("fetch=False 不应调用 get_spot_snapshot()")

    monkeypatch.setattr(adapter, "get_spot_snapshot", _boom)
    monkeypatch.setattr(
        adapter, "get_spot_snapshot_cached",
        lambda max_age=1800: {"600000": {"name": "浦发银行", "pct_chg": 2.0, "turnover": 1e8}},
    )
    b = adapter.get_market_breadth(fetch=False)
    assert b["up"] == 1
    assert b["total"] == 1


def test_build_market_info_falls_back_to_fetch_when_no_cache(monkeypatch):
    """本地无快照时才真正抓取一次全市场。"""
    calls = []

    def _breadth(fetch=True, max_age=1800):
        calls.append(fetch)
        if fetch:
            return {"total": 3, "up": 2, "down": 1, "flat": 0, "limit_up": 0, "limit_down": 0,
                    "adv_decl_ratio": 2.0, "up_ratio": 66.7, "turnover_yi": 9000, "avg_pct": 0.5}
        return {}

    monkeypatch.setattr(adapter, "is_trading_day", lambda d=None: True)
    monkeypatch.setattr(adapter, "latest_trade_day", lambda d=None: "2026-10-21")
    monkeypatch.setattr(adapter, "get_market_breadth", _breadth)
    monkeypatch.setattr(adapter, "get_index_snapshot", lambda: [])

    info = market_info.build_market_info(datetime(2026, 10, 21, 10, 0, 0))
    assert calls == [False, True]
    assert info["turnover_yi"] == 9000
