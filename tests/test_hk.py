"""港股支持：代码识别、腾讯行情解析、日线抓取、行情兜底。"""
from __future__ import annotations

import pytest

from app import api
from app.data import adapter


def test_is_hk_code():
    assert adapter.is_hk_code("01810") is True
    assert adapter.is_hk_code("00700") is True
    assert adapter.is_hk_code("600519") is False
    assert adapter.is_hk_code("0181") is False


def test_valid_code_accepts_both_markets():
    assert api.valid_code("600519") is True
    assert api.valid_code("01810") is True
    assert api.valid_code("12345 ") is True
    assert api.valid_code("1234") is False
    assert api.valid_code("abcdef") is False


def _hk_line():
    """腾讯港股返回 78 段（和 A 股 88 段不同）。"""
    parts = [""] * 78
    parts[0], parts[1], parts[2], parts[3] = "100", "小米集团-W", "01810", "26.800"
    parts[32] = "3.88"                    # 涨跌%
    parts[37] = "5820825617.800"          # 成交额（元）
    parts[39] = "14.99"                   # PE
    parts[45] = "6913.2935"               # 总市值（亿）
    parts[46] = "XIAOMI-W"                # 港股这格是英文名，不是 PB
    return 'v_hk01810="' + "~".join(parts) + '";'


def test_parse_hk_quote_fields():
    code, row = adapter.parse_tencent_quote(_hk_line())
    assert code == "01810"
    assert row["name"] == "小米集团-W"
    assert row["price"] == 26.8
    assert row["pct_chg"] == 3.88
    assert row["pe"] == 14.99
    assert row["market_cap"] == pytest.approx(6913.2935 * 1e8)
    assert row["amount_yi"] == pytest.approx(58.21, abs=0.01)
    assert row["pb"] is None          # 港股没有 PB 字段
    assert row["volume_ratio"] is None
    assert row.get("is_hk") is True


def test_get_quotes_falls_back_to_tencent(monkeypatch):
    monkeypatch.setattr(adapter, "get_spot_snapshot", lambda force=False: {"600519": {"name": "贵州茅台", "price": 1335.0}})
    monkeypatch.setattr(adapter, "get_tencent_quotes",
                        lambda codes: {"01810": {"name": "小米集团-W", "price": 26.8}})
    out = adapter.get_quotes(["600519", "01810"])
    assert out["600519"]["price"] == 1335.0
    assert out["01810"]["price"] == 26.8


def test_fetch_hk_history_parses_klines(monkeypatch):
    class Resp:
        def json(self):
            return {"data": {"hk01810": {"qfqday": [
                ["2026-10-22", "26.0", "26.2", "26.5", "25.9", "1000"],
                ["2026-10-23", "26.0", "26.8", "27.4", "25.88", "2000"],
            ]}}}

    monkeypatch.setattr("requests.get", lambda *a, **k: Resp())
    rows = adapter._fetch_hk_history("01810", 2)
    assert len(rows) == 2
    assert rows[-1]["close"] == 26.8
    assert rows[-1]["high"] == 27.4
