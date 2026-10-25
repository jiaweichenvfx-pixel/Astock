"""长/中/短线三档回测：档位参数、止损口径、情绪推荐规则。"""
from __future__ import annotations

from app.engine import horizon


# ── 档位定义 ─────────────────────────────────────────────────────
def test_three_horizons_exist():
    assert set(horizon.HORIZONS) == {"long", "mid", "short"}
    assert horizon.HORIZONS["long"]["period"] == "weekly"
    assert horizon.HORIZONS["mid"]["period"] == "daily"
    assert horizon.HORIZONS["short"]["period"] == "60"


def test_horizon_exit_params_differ():
    """长线不设止损（拿住），中线 -12%，短线有止盈和时间止损。"""
    assert horizon.HORIZONS["long"]["exit"]["stop_loss"] is None
    assert horizon.HORIZONS["mid"]["exit"]["stop_loss"] == -12
    assert horizon.HORIZONS["short"]["exit"]["stop_loss"] == -5
    assert horizon.HORIZONS["short"]["exit"]["take_profit"] == 8
    assert horizon.HORIZONS["short"]["exit"]["time_stop_days"] == 10


def test_exit_for_keeps_value_benchmark_stopless():
    f = horizon._exit_for("mid")
    assert f("value") == {"stop_loss": None, "take_profit": None, "time_stop_days": None}
    assert f("ma")["stop_loss"] == -12
    assert horizon._exit_for("short")("ma")["take_profit"] == 8


def test_short_horizon_uses_enough_bars_for_warmup():
    """短线 60 分钟取样不能太少，否则 MA60/动量 120 无法预热。"""
    assert horizon.HORIZONS["short"]["days"] >= 120
    assert horizon.HORIZONS["long"]["days"] >= 200


# ── 情绪 → 推荐档位 ──────────────────────────────────────────────
def _res(key, ret, ok=True):
    return {
        "ok": ok, "horizon": key, "label": horizon.HORIZONS[key]["label"],
        "period_label": horizon.HORIZONS[key]["period_label"],
        "best": {"total_return": ret},
    }


def test_recommend_hot_prefers_short():
    results = [_res("long", 10), _res("mid", 20), _res("short", 30)]
    rec = horizon._recommend({"temperature": "hot", "label": "火热"}, results)
    assert rec["horizon"] == "short"


def test_recommend_cold_prefers_long():
    results = [_res("long", 30), _res("mid", 20), _res("short", 10)]
    rec = horizon._recommend({"temperature": "cold", "label": "寒冷"}, results)
    assert rec["horizon"] == "long"


def test_recommend_mild_prefers_mid():
    results = [_res("long", 30), _res("mid", 20), _res("short", 10)]
    rec = horizon._recommend({"temperature": "mild", "label": "温和"}, results)
    assert rec["horizon"] == "mid"


def test_recommend_falls_back_when_sentiment_pick_loses_money():
    """情绪说做短线，但短线历史亏钱 → 退回历史最好的一档，并说明不一致。"""
    results = [_res("long", 40), _res("mid", 12), _res("short", -6)]
    rec = horizon._recommend({"temperature": "hot", "label": "火热"}, results)
    assert rec["horizon"] == "long"
    assert "不一致" in rec["why"]


def test_recommend_handles_missing_horizon():
    results = [_res("long", 10), {"ok": False, "horizon": "short", "label": "短线", "error": "K线不足"}]
    rec = horizon._recommend({"temperature": "hot", "label": "火热"}, results)
    assert rec["horizon"] == "long"


# ── 整体编排（不打网络）───────────────────────────────────────────
def test_compare_horizons_shape(monkeypatch):
    monkeypatch.setattr(horizon, "market_sentiment",
                        lambda: {"temperature": "cold", "label": "寒冷", "score": 2.0})

    def fake(code, name, h):
        base = {"long": 30, "mid": 12, "short": -5}[h]
        return {
            "ok": True, "horizon": h, "label": horizon.HORIZONS[h]["label"],
            "period": horizon.HORIZONS[h]["period"], "period_label": horizon.HORIZONS[h]["period_label"],
            "bars": 300, "first_date": "2020-01-01", "last_date": "2026-10-21",
            "best": {"name": "ma", "label": "双均线", "params": {"short": 5, "long": 20},
                     "total_return": base, "win_rate": 60.0, "max_drawdown": 10.0,
                     "trade_count": 5, "score": 70.0},
            "top": [], "plan": {"ok": True, "action": {"type": "hold", "label": "继续持有"}},
        }

    monkeypatch.setattr(horizon, "analyze_horizon", fake)

    r = horizon.compare_horizons("600000", "测试股")
    assert r["ok"] is True
    assert [h["horizon"] for h in r["horizons"]] == ["long", "mid", "short"]
    assert r["recommend"]["horizon"] == "long"
    assert r["sentiment"]["temperature"] == "cold"


# ── 换手门槛（短线必须够频繁）─────────────────────────────────────
def test_turnover_floor_matches_horizon():
    assert horizon.MIN_TRADES["short"] >= 5      # 2 个月至少 5 笔
    assert horizon.MIN_TRADES["mid"] >= 2
    assert horizon.MIN_TRADES["long"] == 1


def test_fast_params_are_faster_than_defaults():
    """快参的窗口必须比默认参数短，否则不叫快参。"""
    from app.engine.strategies import _params

    for name, fast in horizon.FAST_PARAMS.items():
        default = _params(name, None)
        if name == "ma":
            assert fast["short"] < default["short"] and fast["long"] < default["long"]
        elif name == "donchian":
            assert fast["entry"] < default["entry"]
        elif name == "bollinger":
            assert fast["n"] < default["n"]
        elif name == "momentum":
            assert fast["days"] < default["days"]


def test_turnover_helper_computes_hold_days():
    bars = [{"date": f"2026-01-{i + 1:02d}", "open": 10, "high": 10, "low": 10,
             "close": 10, "volume": 1000} for i in range(10)]

    class _BT:
        trades = [
            {"buy_date": "2026-01-01", "sell_date": "2026-01-04"},
            {"buy_date": "2026-01-06", "sell_date": "2026-01-10"},
        ]
        trade_count = 2

    t = horizon._turnover(bars, _BT(), "mid")
    assert t["trades"] == 2
    assert t["avg_hold_bars"] == 3.5     # (3 + 4) / 2
    assert t["avg_hold_days"] == 3.5
    assert t["cost_per_trade_pct"] > 0
    assert t["cost_drag_pct"] > 0
