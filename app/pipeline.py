"""决策流水线：把 账户→行情→质量→健康度→卖出→仓位→买点 串成完整输出。

执行顺序（解决「健康度↔卖出」的循环依赖）：
1. 取行情填充现价
2. 质量评级（模块4）填充 rating/pool
3. 市场环境（模块1输入）
4. 基础颜色 → 健康度 → 模式（模块1）
5. 技术指标（历史日线 → MA/BOLL/RSI/MACD/RPS）
6. 卖出信号（模块3，用上一步模式 + 技术卖点）
7. 卖出信号精修颜色 → 最终健康度/模式（展示用）
8. 仓位门控 + 分步降杠杆路径（模块2）
9. 买点信号（模块5 投票 + 质量/仓位门控）
"""
from __future__ import annotations

from typing import Optional

from .data import adapter
from .engine import buy as buy_engine
from .engine import health as health_engine
from .engine import horizon_weights as hw_engine
from .engine import margin as margin_engine
from .engine import market_env as market_env_engine
from .engine import macro_env as macro_env_engine
from .engine import position as position_engine
from .engine import quality as quality_engine
from .engine import sell as sell_engine
from .engine import vote
from .engine import swap as swap_engine
from .schemas import Account, Mode, Position, QualityInfo, Rating
from . import config, storage

HISTORY_DAYS = 320  # 足够 MA250 + 动量 + 背离


def enrich_account(account: Account, quotes: Optional[dict[str, dict]] = None) -> Account:
    """用行情填充现价/名称，更新 last_high。"""
    quotes = quotes or adapter.get_quotes([p.code for p in account.positions])
    fx = config.get_fx().get("hkd_cny", 0.8508)
    for p in account.positions:
        q = quotes.get(p.code)
        if q:
            price = q.get("price")
            if price is not None:
                if adapter.is_hk_code(p.code):
                    price = round(price * fx, 4)     # 港股报价是港币，账户统一按人民币计价
                p.current_price = price
                if p.last_high is None or price > p.last_high:
                    p.last_high = price
            if q.get("name"):
                p.name = q["name"]
    return account


def _classify_code(code: str, name: str, q: dict, fin: Optional[dict]) -> QualityInfo:
    """单个代码 → 质量评级（持仓与自选池通用）。"""
    is_st = adapter.is_st(name or q.get("name", ""))
    market_cap = q.get("market_cap")
    if market_cap:
        market_cap = round(market_cap / 1e8, 1)
    return quality_engine.classify_stock(
        code=code,
        name=name,
        roe_ttm=fin.get("roe_ttm") if fin else None,
        growth_yoy=fin.get("growth_yoy") if fin else None,
        is_st=is_st,
        market_cap=market_cap,
        pe_percentile=None,
        pb=q.get("pb"),
        audit_opinion=None,
        consecutive_loss_years=2 if is_st else 0,
        rps=q.get("chg_60d"),
        sector_heat=False,
    )


def compute_quality(account: Account, quotes: Optional[dict[str, dict]] = None) -> dict[str, QualityInfo]:
    """基于行情快照 + 财务指标计算持仓质量评级/五池。"""
    quotes = quotes or adapter.get_quotes([p.code for p in account.positions])
    out: dict[str, QualityInfo] = {}
    for p in account.positions:
        q = quotes.get(p.code) or {}
        fin = adapter.get_financial_indicators(p.code) if not adapter.is_st(p.name or q.get("name", "")) else None
        info = _classify_code(p.code, p.name, q, fin)
        out[p.code] = info
        p.rating = info.rating
        p.pool = info.pool
    return out


def compute_quality_for(codes: list[str], names: dict[str, str], quotes: dict) -> dict[str, QualityInfo]:
    """为任意代码列表（如自选池）计算质量评级。"""
    out: dict[str, QualityInfo] = {}
    for code in codes:
        q = quotes.get(code) or {}
        name = names.get(code) or q.get("name", "") or code
        fin = adapter.get_financial_indicators(code) if not adapter.is_st(name) else None
        out[code] = _classify_code(code, name, q, fin)
    return out


def compute_tech_map(codes: list[str]) -> dict[str, dict]:
    """为一批代码计算技术指标（含 RPS 用的 60 日涨幅、量比、刚放量突破年线）。"""
    tech: dict[str, dict] = {}
    for code in codes:
        hist = adapter.get_stock_history(code, days=HISTORY_DAYS)
        if not hist:
            continue
        closes = [b["close"] for b in hist if b.get("close") is not None]
        if len(closes) < 30:
            continue
        t = vote.compute_tech(closes)
        t["chg_60d"] = _chg_pct(closes, 60)
        t["annual_breakout"] = _annual_breakout(hist)
        tech[code] = t
    return tech


def _chg_pct(closes: list[float], n: int) -> Optional[float]:
    if len(closes) <= n or closes[-n - 1] <= 0:
        return None
    return round((closes[-1] / closes[-n - 1] - 1) * 100, 2)


def _annual_breakout(hist: list[dict]) -> bool:
    """「刚放量突破年线」：今日收盘站上 250 日线、昨日还在线下、且量能放大 1.2 倍。"""
    if len(hist) < 252:
        return False
    closes = [b["close"] for b in hist]
    vols = [b.get("volume") or 0 for b in hist]
    ma250_now = sum(closes[-250:]) / 250
    ma250_prev = sum(closes[-251:-1]) / 250
    vol5 = sum(vols[-6:-1]) / 5
    vol_ratio = (vols[-1] / vol5) if vol5 else 0
    return closes[-1] > ma250_now and closes[-2] <= ma250_prev and vol_ratio >= 1.2


def _rps_60d(chg60: Optional[float], universe: list[Optional[float]]) -> Optional[float]:
    """60 日涨幅的市场分位（0~100）。样本不足 100 个时按绝对幅度近似。"""
    vals = [v for v in universe if v is not None]
    if chg60 is None:
        return None
    if len(vals) >= 100:
        return round(sum(1 for v in vals if v < chg60) / len(vals) * 100, 1)
    for threshold, rps in ((40, 95), (25, 85), (10, 70), (0, 50), (-10, 30)):
        if chg60 >= threshold:
            return float(rps)
    return 15.0


def build_facts(account: Account, quotes: dict, tech: dict) -> dict[str, dict]:
    """构建卖出扫描所需的持仓事实（含技术卖点 + 卖飞防御三条件）。"""
    spot = adapter.get_spot_snapshot_cached(3600) or {}
    universe = [r.get("chg_60d") for r in spot.values()]

    facts: dict[str, dict] = {}
    for p in account.positions:
        q = quotes.get(p.code) or {}
        t = tech.get(p.code) or {}
        is_hk = adapter.is_hk_code(p.code)
        # 港股没有 A 股口径的资金流/涨跌停/分位，这些条件直接不参与
        flow = {"inflow_days": 0, "available": False} if is_hk else adapter.get_fund_flow(p.code)
        chg60 = t.get("chg_60d")
        if chg60 is None:
            chg60 = q.get("chg_60d")
        facts[p.code] = {
            "days_held": adapter.compute_days_held(p.buy_date),
            "last_high": p.last_high,
            "volume_ratio": q.get("volume_ratio"),
            "gain_pct": q.get("pct_chg"),
            "rsi": t.get("rsi"),
            "macd_divergence": bool(t.get("macd_divergence")),
            "sector_rps": None if is_hk else _rps_60d(chg60, universe),
            "net_inflow_days": flow["inflow_days"] if flow.get("available") else None,
            "annual_breakout": bool(t.get("annual_breakout")),
        }
    return facts


def compute_market_env() -> "market_env_engine.MarketEnv":
    raw = adapter.get_market_env_raw()
    above = adapter.get_index_above_ma20()
    return market_env_engine.score_market_env(
        turnover_ratio=_turnover_ratio(raw),
        adv_decl_ratio=raw.get("adv_decl_ratio"),
        index_above_ma20=above,
    )


def _turnover_ratio(raw: dict) -> Optional[float]:
    t = raw.get("turnover_yi")
    if t is None:
        return None
    if t >= 12000:
        return 1.2
    if t >= 8000:
        return 0.9
    return 0.6


def run(account: Optional[Account] = None, use_network: bool = True) -> dict:
    """主入口：跑完整决策流水线。"""
    account = account or storage.load_account()
    watchlist = storage.load_watchlist()
    held_codes = [p.code for p in account.positions]
    watch_codes = [w["code"] for w in watchlist if w["code"] not in held_codes]
    all_codes = list(dict.fromkeys(held_codes + watch_codes))

    # 一次拉全量行情（持仓 + 自选池）
    quotes = adapter.get_quotes(all_codes) if use_network else {}

    enrich_account(account, quotes)
    quality = compute_quality(account, quotes)

    # 自选池质量评级（换股/买点需要）
    watch_names = {w["code"]: w.get("name", "") for w in watchlist}
    quality.update(compute_quality_for(watch_codes, watch_names, quotes))

    market_env = compute_market_env() if use_network else market_env_engine.score_market_env()

    # 外围宏观环境（美联储/黄金/CPI/美股）
    macro_data = adapter.get_macro_data() if use_network else {}
    macro_env = macro_env_engine.score_macro(macro_data)

    # 技术指标（持仓 + 自选池）
    tech = compute_tech_map(all_codes) if use_network else {}

    # 基础健康度 → 模式
    base_health = health_engine.compute_health(account, market_env)
    mode = base_health.mode

    # 卖出信号（含技术卖点）
    facts = build_facts(account, quotes, tech)
    from .engine import fundamental as fundamental_engine
    try:
        trends = fundamental_engine.assess_map(held_codes)
    except Exception:
        trends = {}
    sell_signals = sell_engine.scan_positions(account, mode, quality, facts, trends)
    sell_by_code: dict[str, list] = {}
    for s in sell_signals:
        sell_by_code.setdefault(s.code, []).append(s)

    # 最终健康度
    health = health_engine.compute_health(account, market_env, sell_by_code)

    # 仓位门控 + 降杠杆路径
    gate = position_engine.get_gate(account, health.mode)
    deleverage = position_engine.compute_deleverage_plan(account, health.mode)

    # 买点信号（持仓 + 自选池）
    buy_signals = _generate_buy_signals(account, watchlist, quotes, quality, tech, health.mode, gate.can_buy)

    # 换股配对（砍谁 → 买谁）
    swap_pairs = swap_engine.generate_swaps(account, quality, buy_signals, watchlist)

    return {
        "account": account.to_dict(),
        "health": health.to_dict(),
        "market_env": market_env.to_dict(),
        "macro_env": macro_env,
        "gate": gate.to_dict(),
        "deleverage": deleverage.to_dict(),
        "sell": _sell_summary(sell_signals),
        "buy": _buy_summary(buy_signals),
        "swap": [s.to_dict() for s in swap_pairs],
        "watchlist": [{"code": w["code"], "name": w["name"]} for w in watchlist],
        "quality": {c: q.to_dict() for c, q in quality.items()},
        "margin_risk": margin_engine.assess(account),
        "horizon_weights": hw_engine.compute(account, health.to_dict()),
        "mode": health.mode.value,
        "mode_label": health.to_dict()["mode_label"],
    }


def _sell_summary(sell_signals) -> dict:
    red = [s for s in sell_signals if s.level.value == "red"]
    orange = [s for s in sell_signals if s.level.value == "orange"]
    yellow = [s for s in sell_signals if s.level.value == "yellow"]
    others = [s for s in sell_signals if s.level.value in ("green", "hold")]
    return {
        "total": len(sell_signals),
        "red": [s.to_dict() for s in red],
        "orange": [s.to_dict() for s in orange],
        "yellow": [s.to_dict() for s in yellow],
        "others": [s.to_dict() for s in others],
        "signals": [s.to_dict() for s in sell_signals],
    }


def _buy_summary(signals) -> dict:
    strong = [s for s in signals if s.level == "strong"]
    watch = [s for s in signals if s.level == "watch"]
    hold = [s for s in signals if s.level == "hold"]
    return {
        "total": len(signals),
        "strong": [s.to_dict() for s in strong],
        "watch": [s.to_dict() for s in watch],
        "hold": [s.to_dict() for s in hold],
        "signals": [s.to_dict() for s in signals],
    }


def _generate_buy_signals(account, watchlist, quotes, quality, tech, mode: Mode, gate_can_buy: bool) -> list:
    """为持仓 + 自选池生成买点信号。"""
    out = []
    seen = set()

    def _one(code: str, name: str, position: Optional[Position]):
        if code in seen or code not in tech:
            return
        seen.add(code)
        q = quality.get(code)
        roe = q.roe_ttm if q else None
        pe = (quotes.get(code) or {}).get("pe")
        price = (quotes.get(code) or {}).get("price")
        sig = buy_engine.generate_buy_signal(
            code, name, price, tech[code], position, q, mode, gate_can_buy, roe, pe
        )
        out.append(sig)

    for p in account.positions:
        _one(p.code, p.name, p)
    for w in watchlist:
        _one(w["code"], w.get("name", ""), None)

    out.sort(key=lambda s: -s.score)
    return out
