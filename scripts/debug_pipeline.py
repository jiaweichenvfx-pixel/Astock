"""核心流程 debug：搭一套真实被套持仓，跑完整分析，逐项核对。"""
from app import pipeline
from app.schemas import Account, Position

# 真实被套持仓（成本价高于现价，模拟满仓融资被套）
positions = [
    Position(code="600519", name="贵州茅台", shares=100, cost_price=1500.0, sector="白酒", buy_date="2026-05-20", last_high=1520.0),
    Position(code="300750", name="宁德时代", shares=200, cost_price=400.0, sector="锂电池", buy_date="2026-06-02", last_high=410.0),
    Position(code="600036", name="招商银行", shares=2000, cost_price=43.0, sector="银行", buy_date="2026-03-01", last_high=44.0),
    Position(code="601012", name="隆基绿能", shares=5000, cost_price=18.0, sector="光伏", buy_date="2026-04-15", last_high=18.5),
    Position(code="000858", name="五粮液", shares=1000, cost_price=95.0, sector="白酒", buy_date="2026-05-11", last_high=96.0),
    Position(code="002594", name="比亚迪", shares=500, cost_price=120.0, sector="汽车", buy_date="2026-05-28", last_high=122.0),
    Position(code="601899", name="紫金矿业", shares=2000, cost_price=38.0, sector="有色", buy_date="2026-04-01", last_high=39.0),
    Position(code="600900", name="长江电力", shares=2000, cost_price=30.0, sector="电力", buy_date="2026-01-15", last_high=31.0),
]
acc = Account(positions=positions, cash=50000.0, financing_balance=320000.0)

r = pipeline.run(account=acc, use_network=True)

print("=" * 70)
print("【账户】")
a = r["account"]
print(f"  持仓市值 {a['position_value']}  现金 {a['cash']}  总资产 {a['total_assets']}  净资产 {a['net_assets']}")
print(f"  融资余额 {a['financing_balance']}  融资比 {a['financing_ratio']}%  总仓位 {a['position_ratio']}%  现金比 {a['cash_ratio']}%")
print(f"  整体盈亏 {a['overall_pnl_ratio']}%")

print("=" * 70)
print("【健康度】")
h = r["health"]
print(f"  总分 {h['total']}  模式 {h['mode_label']}")
print(f"  融资分 {h['financing_score']}/25  盈亏分 {h['pnl_score']}/25  质量分 {h['quality_score']}/25  集中度 {h['concentration_score']}/15  环境 {h['market_env_score']}/10")
print(f"  分项和 = {h['financing_score']+h['pnl_score']+h['quality_score']+h['concentration_score']+h['market_env_score']}")
print(f"  颜色: {h['position_colors']}")

print("=" * 70)
print("【市场环境】")
m = r["market_env"]
print(f"  {m['temperature_label']} 分 {m['score']}  涨跌比 {m['adv_decl_ratio']}")

print("=" * 70)
print("【持仓质量评级】")
for c, q in r["quality"].items():
    print(f"  {q['name']:<6} {q['rating_label']:<5} ROE={q['roe_ttm']} 增速={q['growth_yoy']} 池={q['pool_label']}")

print("=" * 70)
print("【卖出信号】")
print(f"  共 {r['sell']['total']} 条  红 {len(r['sell']['red'])}  橙 {len(r['sell']['orange'])}  黄 {len(r['sell']['yellow'])}")
for s in r["sell"]["signals"]:
    print(f"  {s['level_label']} {s['name']:<6} [{s['mechanism_label']}] {s['severity_score']}分 | {s['reason'][:60]}")

print("=" * 70)
print("【买点】")
print(f"  strong {len(r['buy']['strong'])}  watch {len(r['buy']['watch'])}  hold {len(r['buy']['hold'])}")
for s in r["buy"]["signals"][:8]:
    print(f"  {s['level_label']} {s['name']:<6} 分 {s['score']} 信号 {s['signals']} | {s['reason'][:40]}")

print("=" * 70)
print("【仓位门控 + 降杠杆】")
print(f"  可买 {r['gate']['can_buy']} 阻塞 {r['gate']['blockers']}")
for st in r["deleverage"]["steps"]:
    print(f"  第{st['step']}步: 仓位→{st['target_total_position']}% 融资→{st['target_financing']}% 现金→{st['target_cash']}% | {st['time_window']}")

print("=" * 70)
print("【换股】")
for s in r["swap"]:
    print(f"  {s['from_name']} → {s['to_name']} ({s['match_type']})")
if not r["swap"]:
    print("  （无）")
