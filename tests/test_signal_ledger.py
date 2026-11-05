"""信号账本：照做会怎样。"""
from __future__ import annotations

import pytest

from app.engine import signal_ledger


def test_ledger_empty(monkeypatch):
    monkeypatch.setattr(signal_ledger.db, "_conn", lambda: _FakeConn([]))
    r = signal_ledger.build(30)
    assert r["count"] == 0 and r["avg_diff"] is None


def test_ledger_computes_verdicts(monkeypatch):
    rows = [
        ("2026-10-28", "000858", "五粮液", "sell", "red", 80, 71.25),
        ("2026-10-28", "002724", "海洋王", "sell", "yellow", 58, 13.25),
    ]
    monkeypatch.setattr(signal_ledger.db, "_conn", lambda: _FakeConn(rows))
    prices = {"000858": 75.82, "002724": 11.66}
    monkeypatch.setattr(signal_ledger.adapter, "get_quotes",
                        lambda codes: {c: {"price": prices[c]} for c in codes})
    r = signal_ledger.build(30)
    assert r["count"] == 2
    by = {x["code"]: x for x in r["items"]}
    assert by["000858"]["verdict"] == "卖飞"      # 卖了会少赚
    assert by["002724"]["verdict"] == "卖对"      # 卖了更好
    assert r["right_rate"] == 50.0


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_a, **_k):
        return self

    def fetchall(self):
        return self._rows

    def close(self):
        pass
