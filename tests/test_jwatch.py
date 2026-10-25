"""J 值监控：日线/周线位置与状态判定。"""
from __future__ import annotations

from app.engine import jwatch


def _bars(n=260, drift=0.0, wave=0.05):
    """造一段带波动的行情，保证 J 有正有负。"""
    import math
    out, c = [], 20.0
    for i in range(n):
        c *= (1 + drift + math.sin(i / 7) * wave / 100)
        out.append({"date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                    "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1000})
    return out


def test_snapshot_shape(monkeypatch):
    from app.data import adapter
    monkeypatch.setattr(adapter, "get_stock_history", lambda code, days=0: _bars())
    monkeypatch.setattr(adapter, "data_day", lambda: "2026-10-23")
    r = jwatch.snapshot(["600000"], {"600000": "测试"})
    assert r["rows"][0]["code"] == "600000"
    assert r["rows"][0]["j"] is not None
    assert r["rows"][0]["weekly_j"] is not None
    assert r["rows"][0]["status"] in (
        "resonance_buy", "resonance_wait", "weekly_daily_neg", "weekly_low",
        "daily_deep", "daily_neg", "overbought", "normal")


def test_overbought_detected(monkeypatch):
    from app.data import adapter
    # 先横盘、最后连续急涨 → 末端 J 冲到 100 以上
    bars = _bars(200)
    c = bars[-21]["close"]
    for i in range(-20, 0):
        c *= 1.03
        bars[i] = {**bars[i], "close": c, "high": c * 1.005, "low": c * 0.995, "open": bars[i]["close"]}
    monkeypatch.setattr(adapter, "get_stock_history", lambda code, days=0: bars)
    monkeypatch.setattr(adapter, "data_day", lambda: "2026-10-23")
    monkeypatch.setattr(jwatch, "OVERBOUGHT", 90.0)   # 用低阈值验证分支
    r = jwatch.snapshot(["600000"], {})
    row = r["rows"][0]
    assert row["j"] > 90
    assert row["status"] == "overbought"


def test_short_history_skipped(monkeypatch):
    from app.data import adapter
    monkeypatch.setattr(adapter, "get_stock_history", lambda code, days=0: [])
    r = jwatch.snapshot(["600000"], {})
    assert r["rows"] == []


def test_status_ordering(monkeypatch):
    from app.data import adapter
    bars = _bars()
    monkeypatch.setattr(adapter, "get_stock_history", lambda code, days=0: bars)
    monkeypatch.setattr(adapter, "data_day", lambda: "2026-10-23")
    names = {f"60000{i}": f"股{i}" for i in range(5)}
    r = jwatch.snapshot(list(names), names)
    orders = [x["order"] for x in r["rows"]]
    assert orders == sorted(orders)          # 按可操作性排序
