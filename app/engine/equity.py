"""账户净值曲线与杠杆监控。

两条线：
1. **真实快照**：每次计算决策时把账户状态存一行（每天覆盖），从今天开始积累；
2. **回溯估算**：用「当前持仓股数 × 历史收盘价 + 当前现金」倒推过去 N 天，
   用来立刻看清这组仓位过去跌了多深——注意它假设持仓与资金从未变过，只是参考。

杠杆监控：融资余额 / 总资产。这个比例越高，越接近强平线；系统给出口径变化与预警。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from ..data import adapter, db

DEFAULT_DAYS = 90
DEFAULT_LEVERAGE_LIMIT = 20.0   # 与 config 里防守模式的 leverage_max 对齐，可由调用方覆盖


def record_from_summary(summary: dict, today: Optional[str] = None) -> None:
    """把当前账户状态写进每日快照（同日覆盖）。"""
    acc = summary.get("account") or {}
    if not acc:
        return
    db.save_equity_snapshot(today or adapter.data_day(), {
        "total_assets": acc.get("total_assets"),
        "net_assets": acc.get("net_assets"),
        "position_value": acc.get("position_value"),
        "cash": acc.get("cash"),
        "financing": acc.get("financing_balance"),
        "position_ratio": acc.get("position_ratio"),
        "financing_ratio": acc.get("financing_ratio"),
        "pnl_ratio": acc.get("overall_pnl_ratio"),
    })


def _estimate_series(positions: list[dict], cash: float, financing: float,
                     days: int) -> list[dict]:
    """用当前持仓 × 历史收盘价回溯净值（假设持仓、现金、负债都没变）。"""
    if not positions:
        return []

    from .. import config
    fx = config.get_fx().get("hkd_cny", 0.8508)

    closes: dict[str, dict[str, float]] = {}
    for p in positions:
        bars = adapter.get_stock_history(p["code"], days=max(days + 20, 120))
        if not bars:
            continue
        closes[p["code"]] = {str(b["date"])[:10]: b["close"] for b in bars if b.get("close")}

    if not closes:
        return []

    # 用最长的一只票的交易日当时间轴
    axis = sorted(max(closes.values(), key=len).keys())[-days:]
    last_seen: dict[str, float] = {}
    out: list[dict] = []
    for d in axis:
        value = 0.0
        for p in positions:
            series = closes.get(p["code"])
            if not series:
                continue
            if d in series:
                last_seen[p["code"]] = series[d]
            price = last_seen.get(p["code"])
            if price:
                if adapter.is_hk_code(p["code"]):
                    price *= fx          # 港股收盘价是港币
                value += p["shares"] * price
        total = value + cash
        out.append({
            "date": d,
            "total_assets": round(total, 2),
            "net_assets": round(total - financing, 2),
            "position_value": round(value, 2),
            "financing": financing,
            "financing_ratio": round(financing / total * 100, 2) if total > 0 else 0.0,
        })
    return out


def _max_drawdown(series: list[dict]) -> tuple[float, Optional[str], Optional[str]]:
    """最大回撤（%）+ 峰值日 + 谷值日。"""
    peak = None
    peak_date = None
    worst = 0.0
    worst_peak = worst_trough = None
    for row in series:
        v = row.get("net_assets") or 0
        if peak is None or v > peak:
            peak, peak_date = v, row["date"]
        if peak and peak > 0:
            dd = (v - peak) / peak * 100
            if dd < worst:
                worst, worst_peak, worst_trough = dd, peak_date, row["date"]
    return round(worst, 2), worst_peak, worst_trough


def curve(days: int = DEFAULT_DAYS, account: Optional[dict] = None,
          leverage_limit: Optional[float] = None) -> dict:
    """净值曲线 + 回撤 + 杠杆变化。account 传入已 enrich 的账户（含现价），保证末点与账户一致。"""
    from .. import storage

    since = (date.today() - timedelta(days=days * 2)).isoformat()
    real = db.list_equity_snapshots(since)
    real = real[-days:]

    acc = storage.load_account()
    if account:
        positions = [{"code": p["code"], "shares": p["shares"]} for p in account.get("positions", [])]
        cash, financing = account.get("cash", acc.cash), account.get("financing_balance", acc.financing_balance)
    else:
        positions = [{"code": p.code, "shares": p.shares} for p in acc.positions]
        cash, financing = acc.cash, acc.financing_balance
    est = _estimate_series(positions, cash, financing, days)

    # 合并：真实快照优先（同一天用真实值覆盖估算值）
    by_date = {row["date"]: row for row in est}
    for row in real:
        by_date[row["date"]] = row
    merged = [by_date[d] for d in sorted(by_date)]

    # 末点用真实账户值覆盖，避免估算误差影响「当前回撤」
    if merged and account:
        merged[-1] = {**merged[-1],
                      "total_assets": account.get("total_assets"),
                      "net_assets": account.get("net_assets"),
                      "position_value": account.get("position_value"),
                      "financing": account.get("financing_balance"),
                      "financing_ratio": account.get("financing_ratio")}

    current_net = (account or {}).get("net_assets") or (merged[-1]["net_assets"] if merged else acc.net_assets)
    peak_net = max([(r["net_assets"] or 0) for r in merged] + [current_net]) if merged else current_net
    drawdown = round((current_net - peak_net) / peak_net * 100, 2) if peak_net else 0.0
    max_dd, dd_peak, dd_trough = _max_drawdown(merged)

    def leverage_at(offset_days: int) -> Optional[float]:
        if not merged:
            return None
        target = (date.today() - timedelta(days=offset_days)).isoformat()
        past = [r for r in merged if r["date"] <= target]
        return (past[-1] if past else merged[0]).get("financing_ratio")

    lev_now = (account or {}).get("financing_ratio", acc.financing_ratio)
    lev_30 = leverage_at(30)
    lev_90 = leverage_at(90)

    limit = leverage_limit or DEFAULT_LEVERAGE_LIMIT
    level = ("danger" if lev_now >= limit * 1.5
             else "warn" if lev_now > limit else "ok")
    note = {
        "danger": f"融资比 {lev_now}% 已达上限 {limit}% 的 1.5 倍，先降杠杆再谈买点",
        "warn": f"融资比 {lev_now}% 超过上限 {limit}%，优先减仓而不是补仓",
        "ok": f"融资比 {lev_now}% 在 {limit}% 上限内",
    }[level]

    return {
        "days": days,
        "points": merged,
        "real_count": len(real),
        "estimated": bool(est) and len(real) < len(merged),
        "metrics": {
            "net_now": round(current_net, 2),
            "net_peak": round(peak_net, 2),
            "drawdown_pct": drawdown,
            "max_drawdown_pct": max_dd,
            "max_dd_peak": dd_peak,
            "max_dd_trough": dd_trough,
            "leverage_now": lev_now,
            "leverage_30d": lev_30,
            "leverage_90d": lev_90,
            "leverage_delta_30d": round(lev_now - lev_30, 2) if lev_30 is not None else None,
            "level": level,
            "leverage_limit": limit,
            "note": note,
        },
        "note": ("曲线含回溯估算（当前持仓 × 历史收盘价，假设持仓与资金不变）；"
                 "真实快照每天收盘后累积" if est else "均为真实快照"),
    }
