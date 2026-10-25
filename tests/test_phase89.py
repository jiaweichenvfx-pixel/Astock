"""Phase 8-9：换股配对 + 回测引擎 单元测试。"""
from __future__ import annotations

from app.engine import backtest, swap
from app.engine.strategies import STRATEGIES
from app.schemas import Account, BuySignal, Pool, Position, QualityInfo, Rating


def _bars(closes: list[float]) -> list[dict]:
    """由收盘价构造 OHLC 序列（开=昨收，高=+1%，低=-1%）。"""
    bars = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        bars.append({"date": f"2026-01-{i+1:02d}", "open": o, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1000})
        prev = c
    return bars


def test_ma_cross_signals():
    # 跌 → 涨 → 跌：应有金叉和死叉
    closes = [100 - i * 0.5 for i in range(40)] + [80 + i for i in range(40)] + [120 - i for i in range(40)]
    sig = backtest.ma_cross_signals(_bars(closes))
    assert 1 in sig and -1 in sig


def test_run_backtest_trades():
    # 跌后转涨 → 金叉买入，持有到期末，应盈利
    closes = [100 - i * 0.5 for i in range(30)] + [85 + i for i in range(90)]
    bars = _bars(closes)
    sig = backtest.ma_cross_signals(bars)
    r = backtest.run_backtest(bars, sig, {"stop_loss": -5, "take_profit": 15, "time_stop_days": 30})
    assert r.trade_count >= 1
    assert r.total_return > 0
    assert len(r.equity_curve) == len(bars)


def test_compare_exits_three():
    closes = [100 - i * 0.5 for i in range(30)] + [85 + i for i in range(90)]
    results = backtest.compare_exits(_bars(closes))
    assert len(results) == 3
    assert all(r.label for r in results)


def test_compare_strategies_fifteen():
    closes = [100 - i * 0.5 for i in range(30)] + [85 + i for i in range(90)]
    results = backtest.compare_strategies(_bars(closes))
    assert len(results) == len(STRATEGIES)
    assert results[0].total_return >= results[-1].total_return  # 已按收益降序
    assert all(r.label for r in results)


def test_strategy_signal_lengths():
    closes = [100 - i * 0.5 for i in range(30)] + [85 + i for i in range(90)]
    bars = _bars(closes)
    for name in backtest.STRATEGIES:
        sig = backtest.strategy_signal(name, bars)
        assert len(sig) == len(bars)


def test_find_swings():
    # 正弦波动（±10%，峰谷反转 18-22%）：Zigzag 以 12% 门槛应能识别多组高低点
    import math
    closes = [100 + 10 * math.sin(i * 0.3) for i in range(150)]
    highs, lows = backtest.find_swings(closes, min_pct=12)
    assert len(highs) >= 3
    assert len(lows) >= 3


def test_find_swings_filters_small_wiggle():
    # 只有 5% 的小波动：低于 12% 门槛，不应识别为波段
    import math
    closes = [100 + 3 * math.sin(i * 0.5) for i in range(120)]
    highs, lows = backtest.find_swings(closes, min_pct=12)
    assert len(highs) == 0 and len(lows) == 0


def test_compare_timing():
    closes = [100 - i * 0.5 for i in range(30)] + [85 + i for i in range(90)]
    results = backtest.compare_timing(_bars(closes))
    assert len(results) >= 1
    assert all(r.total_gap >= 0 for r in results)
    # 按贴近度升序（越小越准）
    gaps = [r.total_gap for r in results]
    assert gaps == sorted(gaps)


def test_compare_capture():
    closes = [100 - i * 0.5 for i in range(30)] + [85 + i for i in range(90)]
    results = backtest.compare_capture(_bars(closes))
    assert len(results) >= 1
    # 捕获率降序
    caps = [r.capture_ratio for r in results]
    assert caps == sorted(caps, reverse=True)
    assert all(r.trade_count >= 0 for r in results)


def test_analyze_strategies():
    closes = [100 - i * 0.5 for i in range(30)] + [85 + i for i in range(90)]
    results = backtest.analyze_strategies(_bars(closes))
    assert len(results) == len(STRATEGIES)
    # 命中率 0-100，综合分降序
    for r in results:
        assert 0 <= r.buy_hit_rate <= 100
        assert 0 <= r.sell_hit_rate <= 100
        assert 0 <= r.score <= 100
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_optimize_strategy():
    closes = [100 - i * 0.5 for i in range(30)] + [85 + i for i in range(90)]
    bars = _bars(closes)
    opts = backtest.optimize_strategy(bars, "ma", top=4)
    assert 1 <= len(opts) <= 4
    rets = [o.total_return for o in opts]
    assert rets == sorted(rets, reverse=True)


def test_optimize_all():
    closes = [100 - i * 0.5 for i in range(30)] + [85 + i for i in range(90)]
    results = backtest.optimize_all(_bars(closes))
    assert len(results) == len(STRATEGIES)
    rets = [r.total_return for r in results]
    assert rets == sorted(rets, reverse=True)


def test_swap_weak_to_strong():
    acc = Account(positions=[
        Position(code="600000", name="垃圾股", shares=100, cost_price=10, current_price=7, sector="白酒", rating=Rating.BLACKLIST),
    ], cash=0, financing_balance=0)
    quality = {"600000": QualityInfo(code="600000", name="垃圾股", rating=Rating.BLACKLIST, pool=Pool.BLACKLIST, is_st=True)}
    buys = [
        BuySignal(code="600519", name="贵州茅台", level="strong", score=60, rating=Rating.EXCELLENT, pool=Pool.CORE),
        BuySignal(code="000858", name="五粮液", level="watch", score=30, rating=Rating.QUALIFIED, pool=Pool.HOT),
    ]
    watch = [{"code": "600519", "name": "贵州茅台", "sector": "白酒"}]
    pairs = swap.generate_swaps(acc, quality, buys, watch)
    assert len(pairs) == 1
    assert pairs[0].from_code == "600000"
    assert pairs[0].to_code == "600519"  # 同板块弱换强
    assert pairs[0].match_type == "同板块弱换强"


def test_swap_no_candidate():
    acc = Account(positions=[
        Position(code="600000", name="垃圾股", shares=100, cost_price=10, current_price=7, rating=Rating.WARNING),
    ], cash=0, financing_balance=0)
    quality = {"600000": QualityInfo(code="600000", name="垃圾股", rating=Rating.WARNING, pool=Pool.WATCH)}
    pairs = swap.generate_swaps(acc, quality, [], [])
    assert pairs == []
