"""腾讯行情补充源：解析、批量缓存、与选股打分合并。"""
from __future__ import annotations

import pytest

from app.data import adapter, db
from app.engine import screener


def _drop_tx_cache(codes: list[str]) -> None:
    """清掉这些代码的腾讯缓存，避免测试被真实跑过的数据污染。"""
    conn = db._conn()
    for c in codes:
        conn.execute("DELETE FROM cache WHERE key=?", (f"tx_{c}",))
    conn.commit()
    conn.close()


def _line(code: str = "600519", **over) -> str:
    """按腾讯 88 段格式造一行（只填我们关心的下标）。"""
    parts = [""] * 88
    parts[0], parts[1], parts[2], parts[3] = "1", "贵州茅台", code, "1335.00"
    parts[32], parts[37], parts[38] = "1.25", "767722", "0.46"
    parts[39], parts[45], parts[46], parts[49] = "20.49", "16688.59", "6.64", "1.53"
    for k, v in over.items():
        parts[int(k[1:])] = v
    return f'v_sh{code}="' + "~".join(parts) + '";'


# ── 解析 ─────────────────────────────────────────────────────────
def test_parse_tencent_quote_fields():
    code, row = adapter.parse_tencent_quote(_line())
    assert code == "600519"
    assert row["name"] == "贵州茅台"
    assert row["price"] == 1335.0
    assert row["pct_chg"] == 1.25
    assert row["turnover_rate"] == 0.46
    assert row["pe"] == 20.49
    assert row["pb"] == 6.64
    assert row["market_cap"] == pytest.approx(16688.59 * 1e8)
    assert row["volume_ratio"] == 1.53
    assert row["amount_yi"] == pytest.approx(76.77, abs=0.01)


def test_parse_rejects_bad_lines():
    assert adapter.parse_tencent_quote("") is None
    assert adapter.parse_tencent_quote("garbage") is None
    assert adapter.parse_tencent_quote('v_sh600519="1~x~600519~' + "~" * 10 + '";') is None


def test_parse_zero_pe_becomes_none():
    _code, row = adapter.parse_tencent_quote(_line(p39="0.00"))
    assert row["pe"] is None


# ── 批量抓取 + 缓存 ──────────────────────────────────────────────
class _Resp:
    def __init__(self, text: str):
        self.status_code = 200
        self.content = text.encode("gbk")


def test_get_tencent_quotes_batches_and_caches(monkeypatch):
    codes = ["600123", "600124"]
    _drop_tx_cache(codes)
    calls = {"n": 0}

    def fake_get(url, **kw):
        calls["n"] += 1
        assert "q=" in url
        return _Resp(_line("600123") + "\n" + _line("600124"))

    monkeypatch.setattr("requests.get", fake_get)
    rows = adapter.get_tencent_quotes(codes)
    assert set(rows) == set(codes)
    assert rows["600123"]["pe"] == 20.49

    adapter.get_tencent_quotes(codes)          # 第二次走缓存
    assert calls["n"] == 1
    _drop_tx_cache(codes)


def test_get_tencent_quotes_survives_errors(monkeypatch):
    code = "600125"
    _drop_tx_cache([code])

    def boom(url, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr("requests.get", boom)
    assert adapter.get_tencent_quotes([code]) == {}
    _drop_tx_cache([code])


# ── 与选股合并 ───────────────────────────────────────────────────
def test_screener_merges_tencent_fields(monkeypatch):
    spot = {
        "600000": {"name": "浦发银行", "price": 10.0, "pct_chg": 3.0, "turnover": 5e8,
                   "volume_ratio": None, "turnover_rate": None, "pe": None, "pb": None,
                   "market_cap": None, "chg_60d": None},
    }
    tx = {
        "600000": {"name": "浦发银行", "price": 10.0, "pct_chg": 3.0, "turnover_rate": 5.0,
                   "pe": 12.0, "pb": 1.2, "market_cap": 500e8, "volume_ratio": 2.1, "amount_yi": 5.0},
    }
    monkeypatch.setattr(adapter, "get_spot_snapshot", lambda force=False: spot)
    monkeypatch.setattr(adapter, "get_financial_indicators", lambda code: {"roe_ttm": 18, "growth_yoy": 12})
    monkeypatch.setattr(adapter, "get_stock_history", lambda code, days=0, period="daily": None)
    monkeypatch.setattr(adapter, "get_tencent_quotes", lambda codes: tx)

    r = screener.scan_market()
    assert r["data_quality"]["degraded"] is False
    assert r["data_quality"]["supplemented"] is True
    assert r["short"], "补到量比后应该有短线候选"
    assert r["short"][0]["volume_ratio"] == 2.1
