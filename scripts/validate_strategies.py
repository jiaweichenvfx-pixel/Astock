"""跨市场类型验证：大盘/中盘/微盘/创业板/科创板 各跑一遍战法回溯。"""
from app.data import adapter
from app.engine import backtest

CASES = [
    ("600519", "大盘股·贵州茅台"),
    ("600031", "中盘股·三一重工"),
    ("603999", "微盘股·读者传媒"),
    ("300750", "创业板·宁德时代"),
    ("688981", "科创板·中芯国际"),
]

for code, label in CASES:
    h = adapter.get_stock_history(code, days=320)
    if not h or len(h) < 60:
        print(f"\n### {label} ({code}) 数据不足，跳过")
        continue
    closes = [b["close"] for b in h]
    yoy = (closes[-1] / closes[0] - 1) * 100
    # 波动率（日收益率标准差年化）
    import statistics
    rets = [(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes))]
    vol = statistics.stdev(rets) * (250 ** 0.5) * 100 if len(rets) > 1 else 0

    by_ret = backtest.compare_strategies(h)
    by_cap = backtest.compare_capture(h)
    cap = {r.label: r.capture_ratio for r in by_cap}

    print(f"\n### {label} ({code})  全年涨跌 {yoy:+.1f}%  年化波动 {vol:.0f}%")
    print(f"{'战法':<8}{'收益':>9}{'捕获率':>8}{'交易':>5}")
    for r in by_ret:
        print(f"{r.label:<8}{r.total_return:>8.1f}%{cap.get(r.label, 0):>7.1f}%{r.trade_count:>5}笔")
