"""精选选股 + 大盘动态（全市场扫描）。

基于快照：大盘动态（涨跌家数/涨停跌停/ST）+ 热点榜 + 活跃榜 + 价值池 + 核心池。
另出两份带方向的推荐：
- 短线（持股 2~10 天）：量比/换手/当日涨幅/60日趋势打分，涨停与ST直接剔除；
- 长线（持股 6 个月+）：在核心池（ROE>15 增速>10）与价值池（PE<15 或 PB<1）里，
  按估值/市值/趋势再排一次。
两份都只给「候选 + 理由」，具体买卖价由「📍」按钮按对应周期现算（周线/60分钟）。
"""
from __future__ import annotations

from ..data import adapter

HIST_BARS = 90        # 补算量比/均线用的日线根数
ENRICH_POOL = 40      # 只对成交额前 40 只补算，兼顾速度


def _history_metrics(bars: list[dict]) -> dict | None:
    """从日线补算出快照缺失的字段：量比（近似）、5/20/60 日涨幅、均线位置。"""
    if not bars or len(bars) < 25:
        return None
    closes = [b["close"] for b in bars]
    vols = [b.get("volume") or 0 for b in bars]
    last = closes[-1]

    prev5 = sum(vols[-6:-1]) / 5 if len(vols) >= 6 else 0
    vol_ratio = round(vols[-1] / prev5, 2) if prev5 else None
    ma5 = sum(closes[-5:]) / 5
    ma20 = sum(closes[-20:]) / 20

    def chg(n: int):
        if len(closes) <= n or closes[-n - 1] <= 0:
            return None
        return round((last / closes[-n - 1] - 1) * 100, 2)

    return {
        "vol_ratio": vol_ratio,
        "ma5": round(ma5, 2),
        "ma20": round(ma20, 2),
        "above_ma20": last >= ma20,
        "chg5": chg(5),
        "chg20": chg(20),
        "chg60": chg(60),
    }


def _enrich(codes: list[str]) -> dict[str, dict]:
    """顺序补齐日线指标。

    ⚠️ 不能用线程池：akshare 内部 py_mini_racer(V8) 不是线程安全的，
    并发拉取会直接 SIGTRAP 崩进程。首次约 10 秒，之后命中 SQLite 缓存很快。
    """
    out: dict[str, dict] = {}
    for code in codes:
        try:
            m = _history_metrics(adapter.get_stock_history(code, days=HIST_BARS))
        except Exception:
            m = None
        if m:
            out[code] = m
    return out


def _short_pick(r: dict, m: dict | None = None) -> dict | None:
    """短线评分（2~10 天）：量比 + 换手 + 当日强度 + 5/60 日趋势。"""
    name = r.get("name") or ""
    pct = r.get("pct_chg")
    price = r.get("price")
    turn = r.get("turnover") or 0
    if "ST" in name.upper() or "退" in name:
        return None
    if pct is None or price is None or price < 3:
        return None
    code = str(r.get("code", ""))
    if code.startswith(("4", "8", "920")):
        return None                     # 北交所：流动性差、波动大，短线先不碰
    limit = adapter.limit_pct_for(code, name)
    if pct >= limit - 0.1 or pct <= -(limit - 0.1):   # 涨停买不进、跌停卖不出
        return None
    if turn < 1e8:                     # 成交额 < 1 亿，流动性不够
        return None

    score = 0.0
    why: list[str] = []

    vr = r.get("volume_ratio")
    if vr is None:
        vr = (m or {}).get("vol_ratio")
    if vr is not None:
        if vr >= 2:
            score += 30; why.append(f"量比 {vr:.1f} 明显放量")
        elif vr >= 1.5:
            score += 20; why.append(f"量比 {vr:.1f} 温和放量")
        elif vr >= 1.2:
            score += 10; why.append(f"量比 {vr:.1f}")

    tr = r.get("turnover_rate")
    if tr is not None:
        if 0 < tr < 1:
            score -= 5; why.append(f"换手 {tr:.2f}% 偏低（盘子大，短线弹性弱）")
        elif 3 <= tr <= 12:
            score += 20; why.append(f"换手 {tr:.1f}% 活跃")
        elif 12 < tr <= 20:
            score += 5; why.append(f"换手 {tr:.1f}% 偏热")
        elif tr > 20:
            score -= 10; why.append(f"换手 {tr:.1f}% 过热")

    if 2 <= pct <= 7:
        score += 20; why.append(f"当日 +{pct:.2f}% 强势但不极端")
    elif 0 < pct < 2:
        score += 10; why.append(f"当日 +{pct:.2f}%")
    elif pct < 0:
        score -= 10; why.append(f"当日 {pct:.2f}% 逆势")

    chg60 = (m or {}).get("chg60")
    if chg60 is None:
        chg60 = (m or {}).get("chg60") if m else None
    if chg60 is None:
        chg60 = r.get("chg_60d")
    if chg60 is not None:
        if 0 <= chg60 <= 40:
            score += 15; why.append(f"60日 {chg60:+.1f}% 趋势向上")
        elif chg60 > 40:
            score += 5; why.append(f"60日 {chg60:+.1f}% 已过热")
        elif chg60 < -10:
            score -= 10; why.append(f"60日 {chg60:+.1f}% 弱势")

    chg5 = (m or {}).get("chg5")
    if chg5 is not None:
        if 0 < chg5 <= 15:
            score += 15; why.append(f"5日 {chg5:+.1f}% 短线走强")
        elif chg5 > 15:
            score -= 5; why.append(f"5日 {chg5:+.1f}% 短期过热")
    if (m or {}).get("above_ma20"):
        score += 10; why.append("站上20日线")

    if turn >= 5e8:
        score += 15; why.append("成交额 ≥5 亿")
    elif turn >= 2e8:
        score += 8; why.append("成交额 ≥2 亿")

    return {
        "code": r.get("code"), "name": name, "price": price, "pct_chg": pct,
        "turnover_yi": round(turn / 1e8, 1),
        "volume_ratio": None if vr is None else round(vr, 2),
        "turnover_rate": None if tr is None else round(tr, 2),
        "chg_60d": None if chg60 is None else round(chg60, 2),
        "score": round(score, 1), "reasons": why,
    }


def _mid_pick(r: dict, m: dict | None = None) -> dict | None:
    """中线评分（数周~数月）：趋势向上但不过热 + 量能正常。"""
    name = r.get("name") or ""
    pct = r.get("pct_chg")
    price = r.get("price")
    turn = r.get("turnover") or 0
    code = str(r.get("code", ""))
    if "ST" in name.upper() or "退" in name:
        return None
    if pct is None or price is None or price < 3:
        return None
    if code.startswith(("4", "8", "920")):
        return None
    limit = adapter.limit_pct_for(code, name)
    if pct >= limit - 0.1 or pct <= -(limit - 0.1):
        return None
    if turn < 1e8:
        return None

    score, why = 0.0, []
    chg20 = (m or {}).get("chg20")
    chg60 = (m or {}).get("chg60")
    above = (m or {}).get("above_ma20")
    vr = r.get("volume_ratio") or (m or {}).get("vol_ratio")

    if above:
        score += 15; why.append("站上20日线")
    if chg20 is not None:
        if 0 < chg20 <= 20:
            score += 20; why.append(f"20日 {chg20:+.1f}% 走强")
        elif chg20 > 20:
            score -= 10; why.append(f"20日 {chg20:+.1f}% 过热")
        elif chg20 < -5:
            score -= 15; why.append(f"20日 {chg20:+.1f}% 走弱")
    if chg60 is not None:
        if 0 <= chg60 <= 30:
            score += 15; why.append(f"60日 {chg60:+.1f}% 向上")
        elif chg60 < -8:
            score -= 10; why.append(f"60日 {chg60:+.1f}% 弱势")
    if vr is not None and 1.0 <= vr <= 2.5:
        score += 10; why.append(f"量比 {vr:.1f} 正常")
    if turn >= 3e8:
        score += 10; why.append("成交额 ≥3 亿")

    return {
        "code": code, "name": name, "price": price, "pct_chg": pct,
        "turnover_yi": round(turn / 1e8, 1),
        "score": round(score, 1), "reasons": why,
    }


def _long_pick(r: dict, fin: dict | None, kind: str, m: dict | None = None) -> dict | None:
    """长线评分（6 个月+）：好公司（ROE/增速）+ 好价格（PE/PB）+ 大市值 + 趋势不逆。"""
    name = r.get("name") or ""
    if "ST" in name.upper() or "退" in name:
        return None
    roe = fin.get("roe_ttm") if fin else None
    growth = fin.get("growth_yoy") if fin else None
    pe, pb = r.get("pe"), r.get("pb")
    cap = r.get("market_cap")
    chg60 = (m or {}).get("chg60") if m else None
    if chg60 is None:
        chg60 = r.get("chg_60d")

    score = 0.0
    why: list[str] = []

    if roe is not None:
        if roe > 60:
            score += 15; why.append(f"ROE {roe:.1f}% 极高，注意是否一次性收益")
        elif roe > 15:
            score += 25; why.append(f"ROE {roe:.1f}% 优秀")
        elif roe > 10:
            score += 15; why.append(f"ROE {roe:.1f}% 良好")
    if growth is not None and growth > 0:
        score += 15 if growth > 10 else 8
        why.append(f"增速 {growth:+.1f}%")
        if growth > 150:
            score -= 10
            why.append("增速异常高，可能是低基数/一次性收益，需看财报确认")

    if pe is not None and 0 < pe < 15:
        score += 15; why.append(f"PE {pe:.1f} 低估")
    elif pe is not None and 0 < pe < 25:
        score += 8; why.append(f"PE {pe:.1f} 合理")
    if pb is not None and 0 < pb < 2:
        score += 8; why.append(f"PB {pb:.2f}")

    if cap:
        cap_yi = cap / 1e8
        if cap_yi > 500:
            score += 15; why.append(f"市值 {cap_yi:.0f} 亿 大盘股")
        elif cap_yi > 200:
            score += 10; why.append(f"市值 {cap_yi:.0f} 亿")
        elif cap_yi > 100:
            score += 5; why.append(f"市值 {cap_yi:.0f} 亿")

    if chg60 is not None and chg60 > -10:
        score += 8; why.append(f"60日 {chg60:+.1f}% 未走坏")

    return {
        "code": r.get("code"), "name": name, "price": r.get("price"),
        "pct_chg": r.get("pct_chg"),
        "turnover_yi": round((r.get("turnover") or 0) / 1e8, 1),
        "kind": kind,
        "roe": roe, "growth": growth, "pe": pe, "pb": pb,
        "market_cap_yi": None if not cap else round(cap / 1e8, 0),
        "chg_60d": None if chg60 is None else round(chg60, 2),
        "score": round(score, 1), "reasons": why,
    }


def scan_market(top_n: int = 20, core_n: int = 20) -> dict:
    """全市场扫描：大盘动态 + 热点池 + 核心池（卓越）+ 活跃榜 + 黑名单统计。"""
    spot = adapter.get_spot_snapshot()
    if not spot:
        return {"stocks_total": 0, "market_breadth": {}, "hot": [], "core": [], "active": []}

    up = down = flat = limit_up = limit_down = st_count = 0
    hot_rows: list[dict] = []
    active_rows: list[dict] = []
    value_rows: list[dict] = []

    for code, r in spot.items():
        name = r.get("name", "")
        pct = r.get("pct_chg")
        turn = r.get("turnover") or 0
        is_st = "ST" in name.upper() or "退" in name

        if is_st:
            st_count += 1
        if pct is None:
            flat += 1
            continue
        if pct > 0:
            up += 1
        elif pct < 0:
            down += 1
        else:
            flat += 1
        if pct >= 9.8:
            limit_up += 1
        elif pct <= -9.8:
            limit_down += 1

        item = {"code": code, "name": name, "price": r.get("price"), "pct_chg": pct, "turnover_yi": round(turn / 1e8, 1)}
        if not is_st and turn > 1e8 and pct is not None:
            hot_rows.append(item)
        if not is_st and turn > 0:
            active_rows.append(item)
        # 价值池：低 PE（<15）或破净（PB<1），需东财字段
        pe, pb = r.get("pe"), r.get("pb")
        if not is_st and turn > 1e8 and ((pe is not None and 0 < pe < 15) or (pb is not None and 0 < pb < 1)):
            value_rows.append({**item, "pe": pe, "pb": pb})

    hot_rows.sort(key=lambda x: -(x["pct_chg"] or 0))
    active_rows.sort(key=lambda x: -(x["turnover_yi"] or 0))
    # 价值池按 PE 升序（None 排最后）
    value_rows.sort(key=lambda x: (x["pe"] is None, x["pe"] if x["pe"] is not None else 9999))

    # 核心池：从活跃榜前 core_n 只里筛「卓越」（真实 ROE/增速）
    core_rows: list[dict] = []
    for item in active_rows[:core_n]:
        fin = adapter.get_financial_indicators(item["code"])
        roe = fin.get("roe_ttm") if fin else None
        growth = fin.get("growth_yoy") if fin else None
        if roe is not None and roe > 15 and growth is not None and growth > 10:
            core_rows.append({**item, "roe": roe, "growth": growth})
    core_rows.sort(key=lambda x: -(x.get("roe") or 0))

    # ── 短线推荐：成交额前 60 只里补日线，再按量价打分 ────────────
    pool_codes = [x["code"] for x in active_rows[:ENRICH_POOL]]
    metrics = _enrich(pool_codes)

    # 快照缺字段时用腾讯行情补 PE/PB/市值/换手/量比（只补候选池，一次请求 50 只）
    long_codes = [x["code"] for x in core_rows] + [x["code"] for x in value_rows]
    need_tx = list(dict.fromkeys(pool_codes + long_codes))
    tx = adapter.get_tencent_quotes(need_tx)

    def merged(code: str) -> dict:
        base = dict(spot.get(code) or {})
        for k, v in (tx.get(code) or {}).items():
            if v is not None and base.get(k) is None:
                base[k] = v
        base["code"] = code
        return base

    short_rows = []
    for code in pool_codes:
        pick = _short_pick(merged(code), metrics.get(code))
        if pick:
            short_rows.append(pick)
    short_rows.sort(key=lambda x: (-x["score"], -(x.get("turnover_yi") or 0)))

    mid_rows = []
    for code in pool_codes:
        pick = _mid_pick(merged(code), metrics.get(code))
        if pick:
            mid_rows.append(pick)
    mid_rows.sort(key=lambda x: (-x["score"], -(x.get("turnover_yi") or 0)))

    # ── 长线推荐：核心池（卓越）+ 价值池（低估）合并后重排 ────────
    fin_map: dict[str, dict] = {}
    for item in core_rows:
        fin_map[item["code"]] = {"roe_ttm": item.get("roe"), "growth_yoy": item.get("growth")}
    long_rows, seen = [], set()
    for item, kind in [(x, "卓越") for x in core_rows] + [(x, "低估") for x in value_rows]:
        code = item["code"]
        if code in seen:
            continue
        seen.add(code)
        r = merged(code)
        r["name"] = item.get("name") or r.get("name", "")
        pick = _long_pick(r, fin_map.get(code), kind, metrics.get(code))
        if pick:
            long_rows.append(pick)
    long_rows.sort(key=lambda x: (-x["score"], -(x.get("turnover_yi") or 0)))

    # 行情源降级检测：新浪兜底源没有量比/换手/PE/市值
    sample = list(spot.values())[:300]
    with_vr = sum(1 for r in sample if r.get("volume_ratio") is not None)
    snapshot_degraded = bool(sample) and with_vr < len(sample) * 0.5

    # 腾讯补充命中率：候选池里拿到 PE 的比例
    tx_hit = sum(1 for c in pool_codes if (tx.get(c) or {}).get("pe") is not None)
    supplemented = bool(pool_codes) and tx_hit >= max(1, len(pool_codes) // 2)
    degraded = snapshot_degraded and not supplemented

    return {
        "stocks_total": len(spot),
        "data_quality": {
            "degraded": degraded,
            "snapshot_degraded": snapshot_degraded,
            "supplemented": supplemented,
            "sources": {"snapshot": "新浪(兜底)" if snapshot_degraded else "东财",
                        "supplement": f"腾讯行情 {tx_hit}/{len(pool_codes)}" if supplemented else "无"},
            "note": ("当前行情源缺少量比/换手/PE/市值（新浪兜底），短线打分与长线估值已降级，"
                     "只能看涨幅与财务") if degraded else "",
        },
        "market_breadth": {
            "up": up, "down": down, "flat": flat,
            "limit_up": limit_up, "limit_down": limit_down,
            "adv_decl_ratio": round(up / down, 2) if down else (999.0 if up else 1.0),
            "st_count": st_count,
        },
        "short": short_rows[:15],
        "mid": mid_rows[:15],
        "short_pool": len(pool_codes),
        "long": long_rows[:15],
        "hot": hot_rows[:top_n],
        "core": core_rows,
        "value": value_rows[:top_n],
        "active": active_rows[:top_n],
    }
