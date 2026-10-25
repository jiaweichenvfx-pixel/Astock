"""解套降本：针对被套持仓，算清楚「还差多少」「做 T 能降多少」「几次能解套」。

核心逻辑：
1. 每只被套的票，先算「回本还需要涨多少」；
2. 用近 20 日的震荡区间给一个可操作的做 T 区间（低吸价 / 高抛价）；
3. 单次做 T 的收益 = 价差 × 做 T 股数，摊回到全部持仓上就是「成本下降多少」；
4. 用「还差多少 ÷ 每次能降多少」算出需要多少轮 —— 这是解套的现实路径。

重要的现实约束（A 股 T+1）：
- 当天买的不能当天卖，所以做 T 必须**先卖后买**（用底仓），不能先买后卖；
- 融资盘不能用融资新增做 T 的仓位（那是加杠杆），只能用底仓滚动；
- 如果是「基本面崩塌」的票（红色/橙色卖点），做 T 等于在下跌中反复接刀，
  这种票应该优先减仓，而不是做 T —— 这里会直接标出来。
"""
from __future__ import annotations

import statistics
from typing import Optional

from ..data import adapter, db
from . import action_plan, recommend

T_RATIO = 0.30          # 做 T 用底仓的 30%
MIN_SPREAD = 0.008      # 最小价差 0.8%
MAX_SPREAD = 0.04       # 最大价差 4%
MAX_ROUNDS = 60         # 超过 60 轮就不现实了
# 实测：下跌市里这些战法后 60 日平均 −5% ~ −6.5%（网格做T / KDJ金叉 / CCI顺势）
DOWN_UNFRIENDLY = {
    "grid": "网格做T", "kdj": "KDJ金叉", "cci": "CCI顺势",
    "macd": "MACD金叉", "keltner": "ATR通道",
}
# 下跌市里唯一实测正期望的（J值类）
DOWN_FRIENDLY = {"kdj_j", "kdj_jw"}


def market_regime() -> dict:
    """当前市场环境：下跌 / 上涨 / 中性（用指数 MA20 + 涨跌比）。"""
    try:
        above = adapter.get_index_above_ma20()
        raw = adapter.get_market_env_raw() or {}
        ratio = raw.get("adv_decl_ratio")
    except Exception:
        above, ratio = True, None

    if not above and (ratio is None or ratio < 1.0):
        regime, label = "down", "下跌市（指数在 MA20 下方且跌多涨少）"
    elif above and (ratio is None or ratio >= 1.0):
        regime, label = "up", "上涨市"
    else:
        regime, label = "neutral", "震荡市"
    return {"regime": regime, "label": label, "index_above_ma20": above, "adv_decl_ratio": ratio}
MONTHLY_T = 4           # 现实假设：每月尝试做 T 4 次（每周 1 次）
HIT_DECAY = 0.62        # 实测：回测胜率 49% → 样本外 30%，按这个比例折算


def _range(bars: list[dict], n: int = 20) -> Optional[dict]:
    if not bars or len(bars) < n + 1:
        return None
    win = bars[-n:]
    highs = [b["high"] for b in win if b.get("high")]
    lows = [b["low"] for b in win if b.get("low")]
    closes = [b["close"] for b in win if b.get("close")]
    if not highs or not lows or not closes:
        return None
    hi, lo = max(highs), min(lows)
    atr = statistics.mean([(b["high"] - b["low"]) / b["close"]
                           for b in win if b.get("close")]) * 100
    return {"high": hi, "low": lo, "atr_pct": round(atr, 2), "close": closes[-1]}


T_COST_PCT = 0.302          # A 股往返成本：佣金万2.5×2 + 印花税 + 滑点


def _pick_t_strategy(code: str, rows: list[dict]) -> Optional[dict]:
    """从已回测的 15 套战法里，挑最适合「做 T」的那一套。

    做 T 不看总收益，看三件事：
    1. 买卖点命中率（卖在高处 / 买在低处准不准）
    2. 交易频率（一年给多少次机会）
    3. 单次平均价差能否覆盖 0.302% 的往返成本
    """
    best = None
    for row in rows:
        if row.get("code") != code:
            continue
        payload = row.get("payload") or {}
        if not payload.get("ok"):
            continue
        for a in payload.get("all") or []:
            trades = a.get("trade_count") or 0
            if trades < 4:                         # 样本太少，不可信
                continue
            hit = ((a.get("buy_hit_rate") or 0) + (a.get("sell_hit_rate") or 0)) / 2
            per_trade = (a.get("total_return") or 0) / trades
            if per_trade <= T_COST_PCT:            # 单次赚不回成本
                continue
            freq = min(100.0, trades * 8)
            edge = min(100.0, per_trade * 20)
            score = hit * 0.45 + freq * 0.25 + edge * 0.30
            # 做 T 优先用 60 分钟战法（日线战法的持仓周期太长，不是做 T）
            if row.get("horizon") == "short":
                score += 12
            if best is None or score > best["t_score"]:
                best = {
                    "strategy": a["name"], "label": a["label"],
                    "hit": round(hit, 1), "trades": trades,
                    "per_trade": round(per_trade, 2), "t_score": round(score, 1),
                    "horizon": row.get("horizon"),
                    "period": ("60分钟" if row.get("horizon") == "short" else "日线"),
                }
    return best


def analyze(account, sell: Optional[dict] = None, use_network: bool = True) -> dict:
    sell = sell or {}
    # 哪些票已经有「基本面/硬止损」卖点 → 不做 T，优先减仓
    pressured = set()
    for key in ("red", "orange"):
        for s in (sell.get(key) or []):
            if s.get("code"):
                pressured.add(s["code"])

    bt_rows = db.load_bt(adapter.data_day()) if use_network else []
    env = market_regime() if use_network else {"regime": "neutral", "label": "震荡市"}
    items = []
    for p in account.positions:
        cur = p.current_price
        cost = p.cost_price
        shares = p.shares
        if not cur or not cost or shares <= 0:
            continue

        loss_pct = round((cur / cost - 1) * 100, 2)
        gap_pct = round((cost / cur - 1) * 100, 2)          # 回本还需涨多少
        loss_amount = round((cost - cur) * shares, 0)       # 浮亏金额

        bars = adapter.get_stock_history(p.code, days=60) if use_network else None
        rng = _range(bars) if bars else None

        # 做 T 股数（100 股整手，最多 30% 底仓）
        t_shares = int(shares * T_RATIO // 100) * 100
        if t_shares < 100:
            t_shares = 0

        # 做 T 战法：优先用回测里「做 T 评分最高」的那套
        t_strat = None
        if bt_rows:
            rec = recommend.recommend(p.code, "trade")
            if rec.get("ok"):
                m = rec["metrics"]
                t_strat = {
                    "strategy": rec["strategy"], "label": rec["label"],
                    "hit": m.get("hit"), "trades": m.get("trade_count"),
                    "per_trade": m.get("per_trade"), "t_score": rec["rank_score"],
                    "horizon": rec["horizon"],
                    "period": ("60分钟" if rec["horizon"] == "short" else
                               "日线" if rec["horizon"] == "mid" else "周线"),
                }
        buy_zone = sell_zone = None
        trigger_source = "震荡区间分位"

        if t_strat and bars:
            exit_params = None
            try:
                from .horizon import _exit_for
                exit_params = _exit_for("mid")(t_strat["strategy"])
            except Exception:
                exit_params = None
            plan = action_plan.build_action_plan(
                code=p.code, name=p.name, bars=bars,
                strategy=t_strat["strategy"], exit_params=exit_params)
            if plan.get("ok"):
                b = (plan.get("buy") or {}).get("trigger")
                se = (plan.get("sell") or {}).get("trigger")
                if b and se and b < se:
                    width = (se - b) / cur
                    if width <= MAX_SPREAD * 1.5:      # 价差在可做 T 的量级
                        buy_zone, sell_zone = round(b, 2), round(se, 2)
                        trigger_source = f"{t_strat['label']}（{t_strat['period']}）买卖点"
                    else:
                        trigger_source = "战法价差过大，改用震荡区间"

        if buy_zone is None and rng:
            span = rng["high"] - rng["low"]
            buy_zone = round(rng["low"] + span * 0.25, 2)
            sell_zone = round(rng["low"] + span * 0.75, 2)
            if span / rng["close"] < 0.03:
                buy_zone = sell_zone = None

        # 单次能抓到的价差 = 日均振幅的一半（不是整个箱体宽度，那太乐观）
        spread = MIN_SPREAD
        if rng:
            spread = rng["atr_pct"] / 100 * 0.5
        if buy_zone and sell_zone and cur:
            band = (sell_zone - buy_zone) / cur      # 箱体宽度：能抓到的上限
            spread = min(spread, band) if spread > 0 else band
        spread = max(MIN_SPREAD, min(MAX_SPREAD, spread))
        spread_pct = round(spread * 100, 2)

        per_round_gain = spread * cur * t_shares               # 单次做 T 盈利（元）
        cost_drop = per_round_gain / shares                    # 摊到每股的成本下降
        cost_drop_pct = round(cost_drop / cost * 100, 2) if cost else 0

        rounds = None
        if cost_drop_pct > 0 and gap_pct > 0:      # 已盈利的票不需要「回本轮数」
            rounds = min(MAX_ROUNDS + 1, int(gap_pct / cost_drop_pct) + 1)

        # 计划：有卖点的票不做 T
        if gap_pct <= 0:
            plan, action = "已盈利", f"成本 {cost:.2f}，现价 {cur:.2f}，浮盈 {-loss_pct:.1f}%，可作减仓兑现来源"
        elif t_shares == 0:
            plan, action = "仓位不足", f"底仓 {shares} 股，做 T 至少 100 股（30% 不足 1 手），只能整仓处理"
        elif p.code in pressured:
            plan, action = "优先减仓", "该股已触发卖出信号，做 T 等于反复接刀，先减不再加"
        elif gap_pct < 3:
            plan, action = "接近回本", "已在成本附近，等回本或小幅减仓，不必做 T"
        elif env["regime"] == "down" and t_strat and t_strat["strategy"] not in DOWN_FRIENDLY:
            plan, action = "环境不适合做T", (
                f"{env['label']}：{t_strat['label']} 在这类行情里后 60 日均值 −5%~−6.5%，"
                "只等 J 值周线信号出现再做")
        elif t_strat and t_strat["t_score"] < 50:
            plan, action = "节奏不适合做T", (
                f"回测里最高做 T 分只有 {t_strat['t_score']}（{t_strat['label']}），"
                f"这套票没有稳定的高抛低吸节奏，硬做容易两边挨打")
        elif rounds is None or rounds > MAX_ROUNDS:
            plan, action = "做 T 难解套", f"按每次降本 {cost_drop_pct}% 算超过 {MAX_ROUNDS} 轮，考虑减仓换股"
        else:
            plan, action = "做 T 降本", f"每次低吸高抛 {t_shares} 股，约 {rounds} 轮可回本"

        # 做完 12 轮后的成本（三个月进度参考）
        cost_after_12 = max(0.0, cost - cost_drop * 12)
        items.append({
            "code": p.code, "name": p.name, "shares": shares,
            "cost": round(cost, 2), "price": round(cur, 2),
            "loss_pct": loss_pct, "gap_pct": gap_pct, "loss_amount": loss_amount,
            "t_shares": t_shares, "spread_pct": spread_pct,
            "per_round_gain": round(per_round_gain, 0),
            "cost_drop": round(cost_drop, 3),
            "cost_after_12": round(cost_after_12, 2),
            "cost_drop_pct": cost_drop_pct,
            "rounds": rounds,
            "buy_zone": buy_zone, "sell_zone": sell_zone,
            "atr_pct": rng["atr_pct"] if rng else None,
            "t_strategy": t_strat,
            "t_score": (t_strat or {}).get("t_score"),
            "trigger_source": trigger_source,
            "plan": plan, "action": action,
            "pressured": p.code in pressured,
        })

    items.sort(key=lambda x: (-x["loss_amount"], -x["gap_pct"]))

    # 汇总
    total_loss = round(sum(i["loss_amount"] for i in items), 0)
    weights = sum(i["loss_amount"] for i in items)
    avg_gap = (round(sum(i["gap_pct"] * i["loss_amount"] for i in items) / weights, 2)
               if weights else 0)
    # 能真正做 T 的：有底仓、没有卖点压制、且确实被套
    t_capable = [i for i in items
                 if i["t_shares"] > 0 and not i["pressured"]
                 and i["gap_pct"] > 3 and i["plan"] == "做 T 降本"]
    pressured_items = [i for i in items if i["pressured"]]
    total_round_gain = round(sum(i["per_round_gain"] for i in t_capable), 0)

    # 做 T 是近似对称的赌注：赢一次赚一个价差，输一次亏一个价差
    # 期望 = 价差 × (2×胜率 − 1)，胜率 < 50% 时做 T 本身就是亏的
    hits = [(i.get("t_strategy") or {}).get("hit") for i in t_capable]
    hits = [h for h in hits if h]
    win_is = (sum(hits) / len(hits) / 100) if hits else 0.45
    win_adj = win_is * HIT_DECAY                     # 按实测衰减折算到样本外
    ev_is = 2 * win_is - 1
    ev_adj = 2 * win_adj - 1

    annual_t_gain = round(total_round_gain * MONTHLY_T * 12 * max(0.0, ev_is), 0)
    annual_t_gain_adj = round(total_round_gain * MONTHLY_T * 12 * ev_adj, 0)
    coverage = round(annual_t_gain / abs(total_loss) * 100, 1) if total_loss else 0.0

    return {
        "date": adapter.data_day(),
        "market": env,
        "items": items,
        "summary": {
            "loss_total": total_loss,
            "avg_gap_pct": avg_gap,
            "count": len(items),
            "t_count": len(t_capable),
            "t_round_gain": total_round_gain,
            "pressured_count": len(pressured_items),
            "pressured_names": [i["name"] for i in pressured_items],
            "annual_t_gain": annual_t_gain,
            "coverage_pct": coverage,
            "monthly_t": MONTHLY_T,
            "t_win_rate": round(win_is * 100, 1),
            "t_win_rate_adj": round(win_adj * 100, 1),
            "annual_t_gain_adj": annual_t_gain_adj,
            "ev_factor": round(max(0.0, ev_is), 2),
            "loss_after_1y": round(max(0.0, total_loss - annual_t_gain), 0),
            "t_names": [i["name"] for i in t_capable],
            "verdict": (
                f"做T按回测胜率 {round(win_is*100,1)}% 折算样本外约 {round(win_adj*100,1)}%，"
                + ("期望为负 —— 做T只会加速亏损，必须减仓"
                   if ev_adj <= 0 else
                   "期望微薄，只能挑最确定的信号做")
                if coverage < 50 else
                "做 T 是主要解套路径，但胜率必须守住 55% 以上"
            ),
        },
        "rules": {
            "t_ratio": T_RATIO,
            "spread_range": [MIN_SPREAD * 100, MAX_SPREAD * 100],
            "max_rounds": MAX_ROUNDS,
            "note": "A 股 T+1：做 T 必须先用底仓高抛、再低吸买回；融资额度不能用于加仓做 T",
            "assumption": f"年化降本按每月成功 {MONTHLY_T} 次估算",
        },
    }
