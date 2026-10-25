"""缓存与预热：首屏只读缓存、过期打标、后台刷新不阻塞主请求。"""
from __future__ import annotations

import threading
import time
from datetime import datetime

import pytest

from app import api, warmup


def _fake_summary() -> dict:
    return {
        "account": {"positions": []},
        "health": {"total": 0, "mode": "defense", "mode_label": "防守模式"},
        "sell": {"total": 0, "red": [], "orange": [], "yellow": [], "others": [], "signals": []},
    }


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    # 测试里不要真的起后台刷新线程（会走网络）
    monkeypatch.setattr(api, "kick_refresh", lambda: False)
    api.invalidate_cache(keep_data=False)
    yield
    api.invalidate_cache(keep_data=False)


# ── summary 缓存 ─────────────────────────────────────────────────
def test_summary_is_served_from_cache(monkeypatch):
    calls = {"n": 0}

    def fake_run(use_network=True):
        calls["n"] += 1
        return _fake_summary()

    monkeypatch.setattr(api.pipeline, "run", fake_run)

    first = api.get_summary()
    second = api.get_summary()

    assert calls["n"] == 1                 # 只算了一次
    assert first["meta"]["as_of"]
    assert first["meta"]["stale"] is False
    assert second["meta"]["age_sec"] >= 0


def test_summary_marks_stale_when_old(monkeypatch):
    monkeypatch.setattr(api, "_summary_ttl", lambda: 10.0)
    monkeypatch.setattr(api.pipeline, "run", lambda use_network=True: _fake_summary())

    api.get_summary()
    api._cache["ts"] = time.time() - 100
    out = api.get_summary()

    assert out["meta"]["stale"] is True
    assert out["meta"]["age_sec"] >= 100


def test_force_recomputes(monkeypatch):
    calls = {"n": 0}

    def fake_run(use_network=True):
        calls["n"] += 1
        return _fake_summary()

    monkeypatch.setattr(api.pipeline, "run", fake_run)
    api.get_summary()            # 第一次：真算
    api.get_summary()            # 第二次：命中缓存
    assert calls["n"] == 1
    api.get_summary(force=True)  # 强制重算
    assert calls["n"] == 2


# ── 选股缓存 ─────────────────────────────────────────────────────
def test_screener_is_cached(monkeypatch):
    import app.engine.screener as sc

    calls = {"n": 0}

    def fake_scan(top_n=20):
        calls["n"] += 1
        return {"stocks_total": 1, "short": [], "long": [], "hot": [], "core": [], "value": [], "active": []}

    monkeypatch.setattr(sc, "scan_market", fake_scan)
    first = api.get_screener()
    api.get_screener()
    assert calls["n"] == 1
    assert first["meta"]["as_of"]


# ── 手动刷新不阻塞 ───────────────────────────────────────────────
def test_refresh_returns_immediately(monkeypatch):
    api._cache["data"] = _fake_summary()
    api._cache["ts"] = time.time()

    def slow_bg():
        time.sleep(0.3)
        api._refresh_state["running"] = False

    def fake_kick():
        if api._refresh_state["running"]:
            return False
        api._refresh_state["running"] = True
        threading.Thread(target=slow_bg, daemon=True).start()
        return True

    monkeypatch.setattr(api, "kick_refresh", fake_kick)

    t0 = time.time()
    out = api.refresh()
    elapsed = time.time() - t0

    assert elapsed < 0.2                   # 没有等后台
    assert out["refreshing"] is True
    time.sleep(0.4)
    assert api._refresh_state["running"] is False


# ── 预热节奏 ─────────────────────────────────────────────────────
def test_in_trading_session_boundaries():
    assert warmup.in_trading_session(datetime(2026, 10, 23, 10, 0)) is True
    assert warmup.in_trading_session(datetime(2026, 10, 23, 9, 0)) is True
    assert warmup.in_trading_session(datetime(2026, 10, 23, 8, 59)) is False
    assert warmup.in_trading_session(datetime(2026, 10, 23, 16, 0)) is False
    assert warmup.in_trading_session(datetime(2026, 10, 24, 10, 0)) is False   # 周六


def test_warmer_warm_once_and_screener_throttle(monkeypatch):
    calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(api, "get_summary", lambda force=False: calls.append(("summary", force)) or {})
    monkeypatch.setattr(api, "get_screener", lambda force=False: calls.append(("screener", force)) or {})

    # 预热里还会跑持仓战法体检（很重），这里换成空实现
    import app.engine.portfolio_bt as pbt
    monkeypatch.setattr(pbt, "ensure_today", lambda positions, today=None: {"status": "ready"})

    w = warmup.Warmer()
    w.last_screener_ts = 0
    w.warm_once()
    assert ("summary", True) in calls
    assert ("screener", True) in calls
    assert w.runs == 1

    calls.clear()
    w.last_screener_ts = time.time()        # 刚扫过，10 分钟内不再扫
    w.warm_once()
    assert ("summary", True) in calls
    assert ("screener", True) not in calls
