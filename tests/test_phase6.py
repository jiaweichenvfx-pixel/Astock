"""Phase 6：指标 / 策略 / 投票 / 买点 单元测试。"""
from __future__ import annotations

from app.engine import buy, indicators as ind, vote
from app.schemas import BuySignal, Mode, Pool, QualityInfo, Rating


def test_sma():
    v = [1, 2, 3, 4, 5]
    out = ind.sma(v, 3)
    assert out[0] is None and out[1] is None
    assert abs(out[2] - 2.0) < 1e-9
    assert abs(out[4] - 4.0) < 1e-9


def test_ema():
    out = ind.ema([1, 2, 3], 3)
    assert out[0] == 1.0
    assert len(out) == 3


def test_rsi_bounds():
    # 单调上涨 → RSI = 100
    v = list(range(1, 40))
    assert ind.rsi(v, 14) == 100.0
    # 单调下跌 → RSI = 0
    v2 = list(range(40, 1, -1))
    assert ind.rsi(v2, 14) == 0.0


def test_bollinger():
    v = [float(i) for i in range(1, 50)]
    b = ind.bollinger(v, 20, 2.0)
    assert b["upper"][-1] > b["mid"][-1] > b["lower"][-1]


def test_momentum():
    v = [100.0] * 30 + [110.0] * 30 + [120.0] * 2
    m = ind.momentum_return(v, 30)
    assert m is not None and m > 0


def test_macd_divergence_false_on_trend():
    v = list(range(1, 100))
    assert ind.macd_bearish_divergence([float(x) for x in v]) is False


def test_ma_signal():
    tech = {"ma5": 10.0, "ma20": 9.0}
    assert vote.ma_signal(tech) == 1
    assert vote.ma_signal({"ma5": 8.0, "ma20": 9.0}) == -1
    assert vote.ma_signal({"ma5": None, "ma20": 9.0}) == 0


def test_bollinger_signal():
    assert vote.bollinger_signal({"price": 10, "boll_lower": 11, "boll_upper": 20}) == 1
    assert vote.bollinger_signal({"price": 25, "boll_lower": 11, "boll_upper": 20}) == -1
    assert vote.bollinger_signal({"price": 15, "boll_lower": 11, "boll_upper": 20}) == 0


def test_vote_weighted():
    sig = {"value": 1, "ma": 1, "bollinger": 0, "momentum": 1, "grid": 0}
    s = vote.weighted_vote(sig, Mode.ATTACK)  # 20+30+30 = 80
    assert s == 80


def test_buy_level():
    assert buy is not None
    lv, _ = vote.buy_level(50)
    assert lv == "strong"
    lv2, _ = vote.buy_level(15)
    assert lv2 == "watch"
    lv3, _ = vote.buy_level(0)
    assert lv3 == "hold"


def test_buy_quality_gate_blocks():
    tech = {"price": 10, "ma5": 11, "ma20": 10, "ma60": 9, "ma250": 8, "boll_lower": 9, "boll_upper": 12, "rsi": 50, "momentum_60": 10}
    q = QualityInfo(code="600000", name="A", rating=Rating.BLACKLIST, pool=Pool.BLACKLIST, is_st=True)
    sig = buy.generate_buy_signal("600000", "A", 10, tech, None, q, Mode.ATTACK, True)
    assert sig.level == "hold"  # 质量拦截


def test_buy_gate_blocks():
    tech = {"price": 10, "ma5": 11, "ma20": 10, "ma60": 9, "ma250": 8, "boll_lower": 9, "boll_upper": 12, "rsi": 50, "momentum_60": 30}
    q = QualityInfo(code="600000", name="A", rating=Rating.QUALIFIED, pool=Pool.WATCH)
    sig = buy.generate_buy_signal("600000", "A", 10, tech, None, q, Mode.ATTACK, False)
    assert sig.level == "hold"  # 仓位拦截
