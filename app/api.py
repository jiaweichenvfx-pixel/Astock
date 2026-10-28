"""FastAPI 路由层。"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import pipeline, storage
from .data import adapter
from .schemas import Position

router = APIRouter()

# ── 结果缓存：页面只读缓存，重活在后台线程里做（见 warmup.py）──
_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "data": None}
_screener_cache: dict = {"ts": 0.0, "data": None}
_SCREENER_TTL = 600.0      # 选股扫描 10 分钟


def _summary_ttl() -> float:
    """summary 的「新鲜」阈值：盘中 90 秒，收盘后 30 分钟（与预热节奏对齐）。"""
    try:
        from .warmup import in_trading_session
        return 90.0 if in_trading_session() else 1800.0
    except Exception:
        return 90.0


def _meta(ts: float, ttl: float) -> dict:
    age = round(time.time() - ts, 1) if ts else None
    return {
        "as_of": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else None,
        "age_sec": age,
        "stale": bool(ts) and age is not None and age > ttl,
    }


def get_summary(force: bool = False) -> dict:
    """返回决策快照。

    - force=False：有缓存就直接返回（哪怕过期），并用 meta.stale 标记；
      没有缓存（冷启动）才算一次。
    - force=True：重新计算并覆盖缓存（后台预热线程、手动刷新走这条路）。
    """
    with _lock:
        cached = _cache["data"]
        ts = _cache["ts"]
        if not force and cached is not None:
            return {**cached, "meta": _meta(ts, _summary_ttl())}

    data = pipeline.run(use_network=True)
    try:
        from .engine import equity, journal
        journal.record_from_summary(data)
        equity.record_from_summary(data)
    except Exception:
        pass
    with _lock:
        _cache["ts"] = time.time()
        _cache["data"] = data
        ts = _cache["ts"]
    return {**data, "meta": _meta(ts, _summary_ttl())}


def get_screener(force: bool = False, top_n: int = 20) -> dict:
    """精选选股结果（10 分钟缓存）。"""
    from .engine import screener as screener_engine

    with _lock:
        cached = _screener_cache["data"]
        ts = _screener_cache["ts"]
        if not force and cached is not None:
            return {**cached, "meta": _meta(ts, _SCREENER_TTL)}

    data = screener_engine.scan_market(top_n=top_n)
    with _lock:
        _screener_cache["ts"] = time.time()
        _screener_cache["data"] = data
        ts = _screener_cache["ts"]
    return {**data, "meta": _meta(ts, _SCREENER_TTL)}


def invalidate_cache(keep_data: bool = True) -> None:
    """标记缓存过期并在后台重算（不阻塞请求线程）。

    keep_data=True：保留旧快照，接口先返回带 stale 标记的旧数据，15 秒内自动更新。
    这样改完持仓/资金后不会卡 20 秒，页面立刻有响应。
    """
    with _lock:
        _cache["ts"] = 0.0
        if not keep_data:
            _cache["data"] = None
        _screener_cache["ts"] = 0.0
        if not keep_data:
            _screener_cache["data"] = None
    kick_refresh()


def kick_refresh() -> bool:
    """启动一次后台全量刷新（已在跑则忽略）。"""
    with _lock:
        if _refresh_state["running"]:
            return False
        _refresh_state["running"] = True
        _refresh_state["started"] = time.time()
    threading.Thread(target=_refresh_bg, name="astock-refresh", daemon=True).start()
    return True


def cache_status() -> dict:
    with _lock:
        return {
            "summary": _meta(_cache["ts"], _summary_ttl()),
            "screener": _meta(_screener_cache["ts"], _SCREENER_TTL),
        }


# ── 代码校验（A 股 6 位，港股 5 位）─────────────────────────────
def valid_code(code: str) -> bool:
    c = (code or "").strip()
    return c.isdigit() and len(c) in (5, 6)


# ── 请求模型 ─────────────────────────────────────────────────────
class PositionInput(BaseModel):
    code: str = Field(..., description="6位代码")
    name: str = ""
    shares: int = Field(..., gt=0)
    cost_price: float = Field(..., gt=0)
    sector: Optional[str] = None
    buy_date: Optional[str] = None
    last_high: Optional[float] = None


class AccountInput(BaseModel):
    cash: float = Field(0.0, ge=0)
    financing_balance: float = Field(0.0, ge=0)


# ── 路由 ─────────────────────────────────────────────────────────
@router.get("/api/summary")
def summary():
    return get_summary()


_refresh_state = {"running": False, "started": 0.0}


def _refresh_bg() -> None:
    """后台强制刷新：行情 → 决策 → 选股。失败不影响下一次。"""
    try:
        adapter.get_spot_snapshot(force=True)
        get_summary(force=True)
        get_screener(force=True)
    finally:
        _refresh_state["running"] = False


@router.get("/api/refresh")
def refresh():
    """触发后台刷新并立即返回当前快照（前端稍后自动拉取新数据）。"""
    kick_refresh()
    if _cache["data"] is None:
        return {"refreshing": True, "pending": True, "meta": {"as_of": None, "age_sec": None, "stale": True}}
    data = get_summary()
    return {**data, "refreshing": _refresh_state["running"]}


@router.get("/api/account")
def get_account():
    return storage.load_account().to_dict()


@router.post("/api/account")
def set_account(body: AccountInput):
    acc = storage.load_account()
    acc.cash = body.cash
    acc.financing_balance = body.financing_balance
    storage.save_account(acc)
    invalidate_cache()
    return {"ok": True}


@router.get("/api/positions")
def list_positions():
    return [p.to_dict() for p in storage.load_account().positions]


@router.post("/api/positions")
def upsert_position(body: PositionInput):
    acc = storage.load_account()
    existing = next((p for p in acc.positions if p.code == body.code), None)
    if existing:
        existing.name = body.name or existing.name
        existing.shares = body.shares
        existing.cost_price = body.cost_price
        existing.sector = body.sector or existing.sector
        existing.buy_date = body.buy_date or existing.buy_date
        existing.last_high = body.last_high or existing.last_high
    else:
        acc.positions.append(
            Position(
                code=body.code,
                name=body.name,
                shares=body.shares,
                cost_price=body.cost_price,
                sector=body.sector,
                buy_date=body.buy_date,
                last_high=body.last_high,
            )
        )
    storage.save_account(acc)
    invalidate_cache()
    return {"ok": True}


@router.delete("/api/positions/{code}")
def delete_position(code: str):
    acc = storage.load_account()
    before = len(acc.positions)
    acc.positions = [p for p in acc.positions if p.code != code]
    if len(acc.positions) == before:
        raise HTTPException(status_code=404, detail="持仓不存在")
    storage.save_account(acc)
    invalidate_cache()
    return {"ok": True}


@router.post("/api/demo")
def load_demo():
    storage.seed_demo_account()
    invalidate_cache()
    return {"ok": True}


# ── 批量导入持仓 ────────────────────────────────────────────────
class ImportRow(BaseModel):
    code: str
    name: str = ""
    shares: int = Field(..., gt=0)
    cost_price: float = Field(..., gt=0)
    buy_date: Optional[str] = None
    sector: Optional[str] = None


class ImportInput(BaseModel):
    rows: list[ImportRow]
    replace: bool = False                 # 是否清空现有持仓（演示数据）
    cash: Optional[float] = None
    financing_balance: Optional[float] = None
    estimated: bool = False               # 股数/成本是否为占位估算


@router.post("/api/positions/import")
def import_positions(body: ImportInput):
    """批量导入/覆盖持仓。缺名称时自动补（腾讯行情）。"""
    if not body.rows:
        raise HTTPException(status_code=400, detail="没有可导入的行")

    acc = storage.load_account()
    if body.replace:
        acc.positions = []

    need_names = [r.code.strip() for r in body.rows if not r.name]
    names = {}
    if need_names:
        try:
            names = {c: (row.get("name") or "") for c, row in adapter.get_tencent_quotes(need_names).items()}
        except Exception:
            names = {}

    for row in body.rows:
        code = row.code.strip()
        if not valid_code(code):
            raise HTTPException(status_code=400, detail=f"代码不合法：{row.code}（需要 6 位数字）")
        name = row.name or names.get(code) or code
        existing = next((p for p in acc.positions if p.code == code), None)
        if existing:
            existing.name = name
            existing.shares = row.shares
            existing.cost_price = row.cost_price
            existing.sector = row.sector or existing.sector
            existing.buy_date = row.buy_date or existing.buy_date
        else:
            acc.positions.append(Position(
                code=code, name=name, shares=row.shares, cost_price=row.cost_price,
                sector=row.sector, buy_date=row.buy_date,
            ))

    if body.cash is not None:
        acc.cash = body.cash
    if body.financing_balance is not None:
        acc.financing_balance = body.financing_balance

    acc.note = ("股数与成本为占位估算（100 股 / 当日收盘），止损与仓位判断不可信，请尽快补全"
                if body.estimated else "")

    storage.save_account(acc)
    invalidate_cache()
    return {"ok": True, "imported": len(body.rows), "total_positions": len(acc.positions),
            "missing_names": [c for c in need_names if not names.get(c)]}


@router.get("/api/factor")
def factor_rank(horizon: str = "mid"):
    """选股策略对比：多套策略用历史走势回测排名。"""
    from .engine import factor

    if horizon not in factor.HORIZONS:
        raise HTTPException(status_code=400, detail=f"未知周期：{horizon}")
    return factor.get_rank(horizon)


@router.get("/api/factor/all")
def factor_all():
    from .engine import factor

    return {h: factor.get_rank(h) for h in factor.HORIZONS}


def _default_strategy(code: str, purpose: str = "hold") -> str:
    """没指定战法时，用统一推荐的第一名。"""
    from .engine import recommend

    rec = recommend.recommend(code, purpose)
    return rec.get("strategy") or "bollinger"


@router.get("/api/financing")
def financing_view():
    """融资成本：月息、付息倒计时、打平线、还款方案。"""
    from .engine import financing

    acc = storage.load_account()
    try:
        pipeline.enrich_account(acc, adapter.get_quotes([p.code for p in acc.positions]))
    except Exception:
        pass
    return financing.assess(acc)


@router.get("/api/buypoint")
def buypoint_view():
    """买点体检：位置（便宜）+ 趋势确认 + 基本面一票否决。"""
    from .engine import buypoint, watchpool

    acc = storage.load_account()
    held = {p.code: p.name for p in acc.positions}
    pool = {m["code"]: m["name"] for m in watchpool.list_members(None, "active")}
    codes = list(dict.fromkeys(list(held) + list(pool)))
    names = {**pool, **held}
    return buypoint.scan(codes, names)


@router.get("/api/fundamental")
def fundamental_view():
    """基本面趋势：连续几个季度恶化（比单期 −30% 阈值提前预警）。"""
    from .engine import fundamental

    acc = storage.load_account()
    codes = [p.code for p in acc.positions]
    names = {p.code: p.name for p in acc.positions}
    res = fundamental.scan(codes)
    for r in res["rows"]:
        r["name"] = names.get(r["code"]) or r["code"]
    return res


@router.get("/api/recommend")
def recommend_view(code: str, purpose: str = "hold"):
    """统一战法推荐：同一份回测，按用途（hold/trade/buy）加权。"""
    from .engine import recommend

    if purpose not in ("hold", "trade", "buy"):
        raise HTTPException(status_code=400, detail="purpose 只能是 hold/trade/buy")
    return recommend.recommend(code, purpose)


@router.get("/api/jwatch")
def jwatch_view():
    """J 值监控：日线/周线位置、共振买点、超买高抛参考。"""
    from .engine import jwatch

    acc = storage.load_account()
    codes = [p.code for p in acc.positions]
    names = {p.code: p.name for p in acc.positions}
    return jwatch.snapshot(codes, names)


@router.get("/api/rescue")
def rescue_view():
    """解套降本：每只被套票的回本距离、做 T 方案与利息/成本账。"""
    from .engine import rescue

    acc = storage.load_account()
    codes = [p.code for p in acc.positions]
    try:
        quotes = adapter.get_quotes(codes)
    except Exception:
        quotes = {}
    pipeline.enrich_account(acc, quotes)
    return rescue.analyze(acc, (get_summary().get("sell") or {}))


@router.get("/api/watchpool")
def watchpool_view():
    """自选池（短/中/长）+ 事后验证。"""
    from .engine import watchpool

    members = {p: watchpool.list_members(p, "active") for p in watchpool.POOLS}
    # 给每个在池成员补上当前价与进池以来收益
    quotes = {}
    codes = [m["code"] for p in watchpool.POOLS for m in members[p]]
    if codes:
        try:
            quotes = adapter.get_quotes(codes)
        except Exception:
            quotes = {}
    for p in watchpool.POOLS:
        for m in members[p]:
            q = quotes.get(m["code"]) or {}
            cur = q.get("price")
            m["current_price"] = cur
            m["return_pct"] = round((cur / m["entry_price"] - 1) * 100, 2)                 if (cur and m.get("entry_price")) else None

    from .engine import horizon_weights as hw
    acc = storage.load_account()
    hw_info = hw.compute(acc, (get_summary().get("health") or {}))

    from .engine import rescue as _rescue
    try:
        regime = _rescue.market_regime()
    except Exception:
        regime = {"regime": "neutral", "label": "震荡市"}

    return {
        "labels": watchpool.POOL_LABEL,
        "market": {"regime": regime.get("regime"), "label": regime.get("label"),
                   "paused_pools": ["short"] if regime.get("regime") == "down" else []},
        "pools": members,
        "eval": watchpool.evaluate(),
        "history": watchpool.history(),
        "pool_sizes": hw_info["pool_size"],
        "horizon_weights": hw_info["weights"],
    }


@router.post("/api/watchpool/run")
def watchpool_run():
    """立即跑一次「入选 + 复盘剔除」（约 10~30 秒，看行情缓存）。"""
    from .engine import watchpool

    screener = get_screener()          # 复用缓存
    from .engine import factor
    try:
        strategies = {p: factor.best_strategy(p) for p in watchpool.POOLS}
    except Exception:
        strategies = {}
    acc = storage.load_account()
    health = get_summary().get("health") or {}
    from .engine import horizon_weights as hw
    hw_info = hw.compute(acc, health)
    result = watchpool.daily_update(screener, strategies=strategies,
                                    pool_sizes=hw_info["pool_size"])
    return {**result, "eval": watchpool.evaluate(), "strategies": strategies,
            "pool_sizes": hw_info["pool_size"], "horizon_weights": hw_info["weights"]}


# ── 自选池（买点扫描候选）────────────────────────────────────────
class WatchItem(BaseModel):
    code: str
    name: str = ""


@router.get("/api/watchlist")
def get_watchlist():
    return storage.load_watchlist()


@router.post("/api/watchlist")
def add_watch(body: WatchItem):
    storage.add_to_watchlist(body.code.strip(), body.name.strip())
    invalidate_cache()
    return {"ok": True}


@router.delete("/api/watchlist/{code}")
def del_watch(code: str):
    storage.remove_from_watchlist(code)
    invalidate_cache()
    return {"ok": True}


# ── 市场概况（顶部信息条：日期/涨跌比/指数/成交额）──────────────
@router.get("/api/market")
def market_overview():
    from .engine import market_info

    return market_info.build_market_info()


# ── 下一步买卖点（回测/寻优选出的战法 → 具体触发价）──────────────
class PlanInput(BaseModel):
    code: str
    strategy: str = "ma"
    params: Optional[dict] = None
    period: str = "daily"
    days: int = 320
    exit_params: Optional[dict] = None


@router.post("/api/action_plan")
def action_plan_api(body: PlanInput):
    from .data import adapter as ad
    from .engine import action_plan

    code = body.code.strip()
    if not valid_code(code):
        resolved = ad.resolve_name_to_code(code)
        if resolved:
            code = resolved
        else:
            raise HTTPException(status_code=404, detail=f"无法识别「{body.code}」：请输入 6 位代码或股票名称")

    bars = ad.get_stock_history(code, days=body.days, period=body.period)
    if not bars or len(bars) < 30:
        raise HTTPException(status_code=404, detail=f"{code} 历史K线不足（{len(bars) if bars else 0} 根，需 ≥30）")

    quote = (ad.get_quotes([code]).get(code) or {})
    name = quote.get("name") or code
    return action_plan.build_action_plan(
        code=code,
        name=name,
        bars=bars,
        strategy=body.strategy or _default_strategy(body.code),
        params=body.params,
        exit_params=body.exit_params,
        period_label=body.period,
    )


@router.get("/api/equity")
def equity_curve(days: int = 90):
    from .engine import equity

    summary = get_summary()
    limit = (summary.get("gate") or {}).get("leverage_max")
    return equity.curve(days=max(7, min(days, 365)),
                        account=summary.get("account"), leverage_limit=limit)


@router.get("/api/strategy_detail")
def strategy_detail(code: str, strategy: str, horizon: str = "mid", bars: int = 140):
    """单只票 × 单个战法的详情：K线 + 历史买卖点 + 下一步触发价。"""
    from .data import adapter as ad
    from .engine import action_plan
    from .engine.backtest import run_backtest
    from .engine.horizon import HORIZONS, _exit_for
    from .engine.strategies import STRATEGY_LABEL, strategy_signal

    if horizon not in HORIZONS:
        raise HTTPException(status_code=400, detail=f"未知周期：{horizon}")
    preset = HORIZONS[horizon]
    hist = ad.get_stock_history(code, days=preset["days"], period=preset["period"])
    if not hist or len(hist) < 40:
        raise HTTPException(status_code=404, detail=f"{code} 该周期K线不足")

    name = (ad.get_quotes([code]).get(code) or {}).get("name") or code
    exit_params = _exit_for(horizon)(strategy)
    sig = strategy_signal(strategy, hist)
    bt = run_backtest(hist, sig, exit_params, label=STRATEGY_LABEL.get(strategy, strategy))
    plan = action_plan.build_action_plan(
        code=code, name=name, bars=hist, strategy=strategy,
        exit_params=exit_params, period_label=preset["period_label"],
    )

    keep = max(40, min(bars, 300))
    shown = hist[-keep:]
    start_date = shown[0]["date"]
    trades = [t for t in bt.trades if str(t["buy_date"]) >= str(start_date)]

    return {
        "code": code, "name": name,
        "strategy": strategy, "label": STRATEGY_LABEL.get(strategy, strategy),
        "horizon": horizon, "period_label": preset["period_label"],
        "bars": [{"date": b["date"], "open": b["open"], "high": b["high"],
                  "low": b["low"], "close": b["close"]} for b in shown],
        "trades": trades,
        "summary": {"total_return": bt.total_return, "win_rate": bt.win_rate,
                    "trade_count": bt.trade_count, "max_drawdown": bt.max_drawdown,
                    "profit_loss_ratio": bt.profit_loss_ratio},
        "plan": plan,
    }


@router.get("/api/portfolio_bt")
def portfolio_bt_view(force: bool = False, date: Optional[str] = None):
    """持仓战法体检（短/中/长 × 前5）。

    结果落在 SQLite 的 bt_cache（日期 × 代码 × 周期）：
    - 只读不重算（除非 force=True）；
    - date 可回看历史某天（默认今天）。
    """
    from .engine import portfolio_bt

    acc = storage.load_account()
    pos = [{"code": p.code, "name": p.name, "account_type": p.account_type,
            "market_value": p.market_value} for p in acc.positions]
    pos.sort(key=lambda x: -(x.get("market_value") or 0))
    if force:
        return portfolio_bt.ensure_today(pos, force=True)
    return portfolio_bt.get(pos, date)


# ── 今日待办 / 信号复盘 ────────────────────────────────────────
@router.get("/api/todo")
def todo():
    from .engine import todo as todo_engine

    return todo_engine.build_todo(get_summary())


@router.get("/api/signals")
def signals(days: int = 30, horizon: int = 5):
    from .engine import journal

    return journal.review(days=days, horizon=horizon)


# ── 长/中/短线三档回测（周线 / 日线 / 60分钟 + 市场情绪）──────────
class HorizonInput(BaseModel):
    code: str


@router.post("/api/horizons")
def horizons_api(body: HorizonInput):
    from .data import adapter as ad
    from .engine import horizon

    code = body.code.strip()
    if not valid_code(code):
        resolved = ad.resolve_name_to_code(code)
        if resolved:
            code = resolved
        else:
            raise HTTPException(status_code=404, detail=f"无法识别「{body.code}」：请输入 6 位代码或股票名称")

    name = (ad.get_quotes([code]).get(code) or {}).get("name") or code
    return horizon.compare_horizons(code, name)


# ── 单档买卖点（精选列表里点 📍 用）─────────────────────────────
class HorizonPlanInput(BaseModel):
    code: str
    horizon: str = "short"


@router.post("/api/horizon_plan")
def horizon_plan_api(body: HorizonPlanInput):
    from .data import adapter as ad
    from .engine import horizon

    if body.horizon not in horizon.HORIZONS:
        raise HTTPException(status_code=400, detail=f"未知周期：{body.horizon}")

    code = body.code.strip()
    if not valid_code(code):
        resolved = ad.resolve_name_to_code(code)
        if resolved:
            code = resolved
        else:
            raise HTTPException(status_code=404, detail=f"无法识别「{body.code}」")

    name = (ad.get_quotes([code]).get(code) or {}).get("name") or code
    r = horizon.analyze_horizon(code, name, body.horizon)
    if not r.get("ok"):
        raise HTTPException(status_code=404, detail=r.get("error", "计算失败"))
    return r


# ── 一键决策：回测 + 寻优 + 样本外 + 买卖点 ─────────────────────
class PlaybookInput(BaseModel):
    code: str
    period: str = "daily"
    days: int = 320


@router.post("/api/playbook")
def playbook_api(body: PlaybookInput):
    from .data import adapter as ad
    from .engine import playbook

    code = body.code.strip()
    if not valid_code(code):
        resolved = ad.resolve_name_to_code(code)
        if resolved:
            code = resolved
        else:
            raise HTTPException(status_code=404, detail=f"无法识别「{body.code}」：请输入 6 位代码或股票名称")

    bars = ad.get_stock_history(code, days=body.days, period=body.period)
    if not bars or len(bars) < 120:
        raise HTTPException(status_code=404, detail=f"{code} 历史K线不足（{len(bars) if bars else 0} 根，需 ≥120）")

    name = (ad.get_quotes([code]).get(code) or {}).get("name") or code
    return playbook.run_playbook(code, name, bars, period_label=body.period)


# ── 回测（Phase 9）──────────────────────────────────────────────
class BacktestInput(BaseModel):
    code: str
    days: int = 320
    period: str = "daily"   # daily / weekly / 30 / 60 / 120


@router.post("/api/backtest")
def run_backtest_api(body: BacktestInput):
    import logging
    from .data import adapter as ad
    from .engine import backtest
    logger = logging.getLogger("backtest")

    code = body.code.strip()
    # 名称 → 6位代码解析（支持输入"中金黄金"/"茅台"等名称）
    if not valid_code(code):
        resolved = ad.resolve_name_to_code(code)
        if resolved:
            code = resolved
        else:
            raise HTTPException(status_code=404, detail=f"无法识别「{body.code}」：请输入 6 位 A 股代码（如 600489）、5 位港股代码（如 01810）或股票名称")
    try:
        bars = ad.get_stock_history(code, days=body.days, period=body.period)
    except Exception as e:
        logger.warning("backtest fetch %s/%s raised: %s", code, body.period, e)
        raise HTTPException(status_code=500, detail=f"拉取历史数据异常：{e}")
    got = len(bars) if bars else 0
    logger.warning("backtest fetch %s/%s -> %d bars", code, body.period, got)
    if not bars or got < 40:
        if got == 0:
            detail = f"未找到代码 {code} 的历史数据 —— 请用6位A股代码（如 600519/601899/300750/688981，不支持北交所/退市股）"
        else:
            detail = f"该股历史K线仅 {got} 根（需 ≥40）—— 可能是新股/北交所，请换一只或用日线/周线"
        raise HTTPException(status_code=404, detail=detail)
    name = (ad.get_quotes([code]).get(code) or {}).get("name", code)
    exit_results = backtest.compare_exits(bars)
    strategy_results = backtest.compare_strategies(bars)
    timing_results = backtest.compare_timing(bars)
    capture_results = backtest.compare_capture(bars)
    analysis_results = backtest.analyze_strategies(bars)
    return {
        "code": code,
        "name": name,
        "bars": len(bars),
        "period": body.period,
        "strategy": "双均线 MA5/MA20",
        "results": [r.to_dict() for r in exit_results],
        "strategies": [r.to_dict() for r in strategy_results],
        "timing": [r.to_dict() for r in timing_results],
        "capture": [r.to_dict() for r in capture_results],
        "analysis": [r.to_dict() for r in analysis_results],
    }


# ── 参数寻优（多周期）────────────────────────────────────────────
class OptimizeInput(BaseModel):
    code: str
    days: int = 320
    mode: str = "return"   # return=收益优先 / win_rate=胜率优先


_PERIODS = [("daily", "日线"), ("weekly", "周线"), ("120", "2小时"), ("60", "1小时"), ("30", "30分钟")]


@router.post("/api/optimize")
def optimize_api(body: OptimizeInput):
    from .data import adapter as ad
    from .engine import backtest

    code = body.code.strip()
    if not valid_code(code):
        resolved = ad.resolve_name_to_code(code)
        if resolved:
            code = resolved
    name = (ad.get_quotes([code]).get(code) or {}).get("name", code)
    periods = []
    for period, label in _PERIODS:
        bars = ad.get_stock_history(code, days=body.days, period=period)
        if not bars or len(bars) < 40:
            continue
        best = backtest.optimize_all(bars, sort_by=body.mode)
        trapped = backtest.best_for_trapped(bars)
        periods.append({
            "period": period,
            "period_label": label,
            "bars": len(bars),
            "top": [r.to_dict() for r in best[:5]],
            "trapped": [r.to_dict() for r in trapped],
        })
    return {"code": code, "name": name, "periods": periods}


# ── 精选选股（全市场扫描）───────────────────────────────────────
@router.get("/api/screener")
def screener(top_n: int = 20):
    return get_screener(top_n=top_n)


@router.get("/api/ping")
def ping():
    return {"ok": True, "akshare": adapter.HAS_AKSHARE}
