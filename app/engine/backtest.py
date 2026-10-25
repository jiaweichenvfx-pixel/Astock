"""模块9：模拟仓回测引擎（完整 A 股规则建模）。

规则：T+1、涨跌停无法成交、佣金万2.5、印花税 0.05%（卖出）、过户费、滑点 0.1%。
撮合：信号在收盘判定，次日开盘价成交；止损/止盈盘中按价格触发。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

from .. import config
from .strategies import STRATEGIES, STRATEGY_LABEL, PARAM_GRIDS, ma_cross_signals, strategy_signal


@dataclass
class Trade:
    buy_date: str
    buy_price: float
    sell_date: str
    sell_price: float
    shares: int
    pnl_pct: float       # 含费后收益率 %
    pnl_amount: float
    reason: str          # 止损/止盈/死叉/时间止损/期末平仓

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BacktestResult:
    code: str = ""
    name: str = ""
    label: str = ""
    initial_capital: float = 100000.0
    final_capital: float = 100000.0
    total_return: float = 0.0      # %
    annual_return: float = 0.0     # %
    max_drawdown: float = 0.0      # %
    win_rate: float = 0.0          # %
    profit_loss_ratio: float = 0.0
    trade_count: int = 0
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)
    # 期末状态（force_close=False 时保留未平仓持仓，供「下一步买卖点」使用）
    in_position: bool = False
    entry_price: Optional[float] = None
    entry_date: str = ""
    entry_pnl_pct: Optional[float] = None
    entry_days_held: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _limit_check(bar: dict, prev_close: Optional[float], limit_pct: float, direction: str) -> bool:
    """判断是否涨跌停无法成交。direction: 'up' 无法买入, 'down' 无法卖出。"""
    if prev_close is None or prev_close <= 0:
        return False
    pct = (bar["open"] - prev_close) / prev_close
    if direction == "up":
        return pct >= limit_pct - 0.005
    return pct <= -(limit_pct - 0.005)


def _fees(value: float) -> float:
    """单向费用：佣金万2.5（不足5元按5元）+ 过户费。"""
    cfg = config.get_backtest()
    commission = max(5.0, value * cfg["commission_rate"])
    transfer = value * cfg["transfer_fee_rate"]
    return commission + transfer


def _sell_fees(value: float) -> float:
    """卖出费用：佣金 + 印花税 + 过户费。"""
    cfg = config.get_backtest()
    commission = max(5.0, value * cfg["commission_rate"])
    stamp = value * cfg["stamp_tax_rate"]
    transfer = value * cfg["transfer_fee_rate"]
    return commission + stamp + transfer


def run_backtest(
    bars: list[dict],
    signals: list[int],
    exit_params: dict,
    code: str = "",
    name: str = "",
    label: str = "",
    initial_capital: float = 1000000.0,
    limit_pct: float = 10.0,
    force_close: bool = True,
) -> BacktestResult:
    """通用回测。

    bars: 升序 [{date, open, high, low, close, volume}]
    signals: 与 bars 等长的 +1/-1/0（1=买入信号，-1=卖出信号）
    exit_params: {stop_loss(负数%), take_profit(正数%), time_stop_days}
    force_close: 期末是否强平。False 时把未平仓持仓写进结果（下一步买卖点用）。
    """
    cfg = config.get_backtest()
    slippage = cfg["slippage"]
    stop_loss = exit_params.get("stop_loss")
    take_profit = exit_params.get("take_profit")
    time_stop_days = exit_params.get("time_stop_days")

    cash = initial_capital
    shares = 0
    buy_price = 0.0
    buy_date = ""
    days_held = 0
    pending: Optional[str] = None
    trades: list[Trade] = []
    equity_curve: list[dict] = []
    peak = initial_capital
    max_drawdown = 0.0

    n = len(bars)
    for i in range(n):
        bar = bars[i]
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
        prev_close = bars[i - 1]["close"] if i > 0 else o

        # 1. 执行昨日挂单（次日开盘价成交）
        if pending == "buy" and shares == 0:
            if not _limit_check(bar, prev_close, limit_pct, "up"):
                fill = o * (1 + slippage)
                lots = int((cash / (fill * 100)) * (1 - cfg["commission_rate"]))
                if lots > 0:
                    shares = lots * 100
                    cost = shares * fill + _fees(shares * fill)
                    cash -= cost
                    buy_price = fill
                    buy_date = bar["date"]
                    days_held = 0
        elif pending == "sell" and shares > 0:
            if not _limit_check(bar, prev_close, limit_pct, "down"):
                fill = o * (1 - slippage)
                _close_trade(fill, bar["date"], shares, buy_price, buy_date, "死叉卖出", trades)
                cash += shares * fill - _sell_fees(shares * fill)
                shares = 0
        pending = None

        # 2. 盘中止损/止盈（T+1：买入当日不可卖）
        if shares > 0 and days_held >= 1:
            if stop_loss is not None:
                stop_price = buy_price * (1 + stop_loss / 100)
                if l <= stop_price:
                    _close_trade(stop_price, bar["date"], shares, buy_price, buy_date, f"止损 {stop_loss}%", trades)
                    cash += shares * stop_price - _sell_fees(shares * stop_price)
                    shares = 0
            if shares > 0 and take_profit is not None:
                tp_price = buy_price * (1 + take_profit / 100)
                if h >= tp_price:
                    _close_trade(tp_price, bar["date"], shares, buy_price, buy_date, f"止盈 {take_profit}%", trades)
                    cash += shares * tp_price - _sell_fees(shares * tp_price)
                    shares = 0

        # 3. 时间止损
        if shares > 0 and time_stop_days and days_held >= time_stop_days:
            fill = c * (1 - slippage)
            _close_trade(fill, bar["date"], shares, buy_price, buy_date, f"时间止损 {time_stop_days}天", trades)
            cash += shares * fill - _sell_fees(shares * fill)
            shares = 0

        # 4. 收盘判定信号，挂次日单
        sig = signals[i] if i < len(signals) else 0
        if shares == 0 and sig > 0:
            pending = "buy"
        elif shares > 0 and sig < 0:
            pending = "sell"

        # 5. 持仓天数 + 权益
        if shares > 0:
            days_held += 1
        equity = cash + shares * c
        equity_curve.append({"date": bar["date"], "equity": round(equity, 2), "close": c})
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100 if peak > 0 else 0.0
        max_drawdown = max(max_drawdown, dd)

    # 期末：默认强平；force_close=False 则保留持仓状态（供买卖点推算）
    open_info = {}
    if shares > 0:
        if force_close:
            last = bars[-1]
            fill = last["close"] * (1 - slippage)
            _close_trade(fill, last["date"], shares, buy_price, buy_date, "期末平仓", trades)
            cash += shares * fill - _sell_fees(shares * fill)
            shares = 0
        else:
            open_info = {
                "in_position": True,
                "entry_price": round(buy_price, 2),
                "entry_date": buy_date,
                "entry_pnl_pct": round((bars[-1]["close"] / buy_price - 1) * 100, 2),
                "entry_days_held": days_held,
            }

    result = _build_result(code, name, label, initial_capital, cash, max_drawdown, trades, equity_curve, bars)
    for k, v in open_info.items():
        setattr(result, k, v)
    return result


def _close_trade(fill, date, shares, buy_price, buy_date, reason, trades):
    gross_pnl = (fill - buy_price) / buy_price
    # 简化：含费后收益率近似 = 毛收益率 - 双边费
    cfg = config.get_backtest()
    fee_pct = (cfg["commission_rate"] * 2 + cfg["stamp_tax_rate"] + cfg["transfer_fee_rate"] * 2) * 100
    pnl_pct = round(gross_pnl * 100 - fee_pct, 2)
    trades.append(
        Trade(
            buy_date=buy_date,
            buy_price=round(buy_price, 2),
            sell_date=date,
            sell_price=round(fill, 2),
            shares=shares,
            pnl_pct=pnl_pct,
            pnl_amount=round(shares * (fill - buy_price) - _sell_fees(shares * fill) - _fees(shares * buy_price), 2),
            reason=reason,
        )
    )


def _build_result(code, name, label, initial, final_cash, max_dd, trades, equity_curve, bars):
    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]
    win_rate = round(len(wins) / len(trades) * 100, 2) if trades else 0.0
    avg_win = sum(t.pnl_pct for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t.pnl_pct for t in losses) / len(losses) if losses else 0.0
    pl_ratio = round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else (99.0 if wins else 0.0)
    total_return = round((final_cash / initial - 1) * 100, 2)
    n_days = max(1, len(bars))
    annual = round(((1 + total_return / 100) ** (250 / n_days) - 1) * 100, 2) if total_return > -100 else -100.0
    return BacktestResult(
        code=code,
        name=name,
        label=label,
        initial_capital=round(initial, 2),
        final_capital=round(final_cash, 2),
        total_return=total_return,
        annual_return=annual,
        max_drawdown=round(max_dd, 2),
        win_rate=win_rate,
        profit_loss_ratio=pl_ratio,
        trade_count=len(trades),
        trades=[t.to_dict() for t in trades],
        equity_curve=equity_curve,
    )


# ── 交叉对比：同一策略不同卖点参数 ───────────────────────────────
def compare_exits(bars: list[dict], signal_sets: Optional[list] = None, label: str = "") -> list[BacktestResult]:
    """同一策略、不同止损/止盈参数交叉对比。

    默认对比三组：激进(-3/+5) / 均衡(-5/+8) / 稳健(-7/+12)。
    """
    closes = [b["close"] for b in bars]
    signals = ma_cross_signals(bars)
    if signal_sets is None:
        exit_sets = [
            {"label": "激进 -3/+5", "stop_loss": -3, "take_profit": 5, "time_stop_days": 7},
            {"label": "均衡 -5/+8", "stop_loss": -5, "take_profit": 8, "time_stop_days": 10},
            {"label": "稳健 -7/+12", "stop_loss": -7, "take_profit": 12, "time_stop_days": 15},
        ]
    else:
        exit_sets = signal_sets
    results = []
    for es in exit_sets:
        r = run_backtest(bars, signals, es, label=es.get("label", ""))
        results.append(r)
    return results


def _exit_params_for(name: str) -> dict:
    """各战法的回测卖出参数。长线持有 = 无止损（纯拿住）；其余用共同止损 -12%。"""
    if name == "value":
        return {"stop_loss": None, "take_profit": None, "time_stop_days": None}
    return {"stop_loss": -12, "take_profit": None, "time_stop_days": None}


def compare_strategies(bars: list[dict], exit_params: Optional[dict] = None) -> list[BacktestResult]:
    """九大策略并排回溯，按总收益降序返回。

    用「纯信号」卖出，与 compare_capture 口径一致，两者才可相互印证。
    """
    results = []
    for name in STRATEGIES:
        sig = strategy_signal(name, bars)
        ep = dict(exit_params) if exit_params else _exit_params_for(name)
        r = run_backtest(bars, sig, ep, label=STRATEGY_LABEL[name])
        results.append(r)
    results.sort(key=lambda r: -r.total_return)
    return results


# ── 阶段性高低点 + 择时贴近度 ────────────────────────────────────
@dataclass
class TimingResult:
    label: str
    buy_gap: Optional[float]      # 平均买点偏离低点 %（越小越准，绝对值）
    sell_gap: Optional[float]     # 平均卖点偏离高点 %（越小越准，绝对值）
    total_gap: float              # buy_gap + sell_gap
    buy_count: int
    sell_count: int
    buy_knife: int = 0            # 买在低点下方（接飞刀）的笔数
    swing_highs: int = 0
    swing_lows: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def find_swings(closes: list[float], min_pct: float = 12.0) -> tuple[list, list]:
    """Zigzag 波段高低点：价格必须反向 >= min_pct% 才确认一个波段拐点。

    这样过滤掉 < min_pct% 的小波动噪音，只保留真正的「波段」。
    返回 (highs, lows)，每个元素为 (index, price)。
    """
    highs: list[tuple] = []
    lows: list[tuple] = []
    n = len(closes)
    if n < 2:
        return highs, lows

    last_idx, last_price = 0, closes[0]
    direction = "up" if closes[1] > closes[0] else "down"
    for i in range(1, n):
        p = closes[i]
        if direction == "up":
            if p >= last_price:
                last_idx, last_price = i, p
            elif (last_price - p) / last_price * 100 >= min_pct:
                highs.append((last_idx, last_price))
                direction = "down"
                last_idx, last_price = i, p
        else:
            if p <= last_price:
                last_idx, last_price = i, p
            elif (p - last_price) / last_price * 100 >= min_pct:
                lows.append((last_idx, last_price))
                direction = "up"
                last_idx, last_price = i, p
    return highs, lows


def _nearest_price_before(points: list[tuple], idx: int) -> Optional[float]:
    """返回 index <= idx 的最近一个摆动点价格。"""
    best = None
    for pi, p in points:
        if pi <= idx:
            best = p
        else:
            break
    return best


def _signal_events(signals: list[int]) -> tuple[list[int], list[int]]:
    """把连续信号归并为买卖事件（取每段的首个索引），对齐回测实际成交。"""
    buys, sells = [], []
    for i, s in enumerate(signals):
        if s > 0 and (i == 0 or signals[i - 1] <= 0):
            buys.append(i)
        if s < 0 and (i == 0 or signals[i - 1] >= 0):
            sells.append(i)
    return buys, sells


def compare_timing(bars: list[dict], min_pct: float = 12.0) -> list[TimingResult]:
    """择时贴近度（方向锚定 + Zigzag 波段）。

    买点参考「买点之前最近一个已确认波段低点」，卖点参考「卖点之前最近一个已确认波段高点」。
    分值 = 平均买入偏离 + 平均卖出偏离（越小 = 越贴近波段高低点）。
    """
    closes = [b["close"] for b in bars]
    highs, lows = find_swings(closes, min_pct)
    results: list[TimingResult] = []

    for name in STRATEGIES:
        sig = strategy_signal(name, bars)
        buys, sells = _signal_events(sig)

        buy_signed = []
        for i in buys:
            lp = _nearest_price_before(lows, i)
            if lp:
                buy_signed.append((closes[i] - lp) / lp * 100)
        buy_gaps = [abs(g) for g in buy_signed]
        buy_knife = sum(1 for g in buy_signed if g < 0)

        sell_gaps = []
        for i in sells:
            hp = _nearest_price_before(highs, i)
            if hp:
                sell_gaps.append(abs(closes[i] - hp) / hp * 100)

        avg_buy = round(sum(buy_gaps) / len(buy_gaps), 2) if buy_gaps else None
        avg_sell = round(sum(sell_gaps) / len(sell_gaps), 2) if sell_gaps else None
        total = round((avg_buy or 0) + (avg_sell or 0), 2)
        results.append(
            TimingResult(
                label=STRATEGY_LABEL[name],
                buy_gap=avg_buy,
                sell_gap=avg_sell,
                total_gap=total,
                buy_count=len(buy_gaps),
                sell_count=len(sell_gaps),
                buy_knife=buy_knife,
                swing_highs=len(highs),
                swing_lows=len(lows),
            )
        )

    results = [r for r in results if r.buy_count > 0 or r.sell_count > 0]
    results.sort(key=lambda r: r.total_gap)
    return results


@dataclass
class CaptureResult:
    label: str
    capture_ratio: float     # 平均捕获率 %（实际收益/波段潜在收益）
    trade_count: int

    def to_dict(self) -> dict:
        return asdict(self)


def compare_capture(bars: list[dict], exit_params: Optional[dict] = None) -> list[CaptureResult]:
    """捕获率：每笔交易「实际收益 ÷ 该波段最大可能收益」，衡量买卖点找得有多好。

    统计所有交易（含亏损的小波段），只剔除 <1% 的无意义窗口，避免幸存者偏差。
    用「纯信号」卖出，与 compare_strategies 口径一致。
    """
    closes = [b["close"] for b in bars]
    date_to_idx = {b["date"]: i for i, b in enumerate(bars)}
    results: list[CaptureResult] = []

    for name in STRATEGIES:
        sig = strategy_signal(name, bars)
        ep = dict(exit_params) if exit_params else _exit_params_for(name)
        bt = run_backtest(bars, sig, ep, label=STRATEGY_LABEL[name])
        captures = []
        for t in bt.trades:
            bi = date_to_idx.get(t["buy_date"])
            si = date_to_idx.get(t["sell_date"])
            if bi is None or si is None or si <= bi:
                continue
            window = closes[bi:si + 1]
            max_high = max(window)
            min_low = min(window)
            if min_low <= 0:
                continue
            amplitude = (max_high - min_low) / min_low * 100   # 该交易持仓期波段振幅 %
            if amplitude < 1.0:
                continue   # 只剔除极窄窗口，避免除零
            actual = (t["sell_price"] - t["buy_price"]) / t["buy_price"] * 100
            captures.append(actual / amplitude * 100)
        avg = round(sum(captures) / len(captures), 1) if captures else None
        results.append(CaptureResult(label=STRATEGY_LABEL[name], capture_ratio=avg, trade_count=len(captures)))

    results = [r for r in results if r.capture_ratio is not None]
    results.sort(key=lambda r: -(r.capture_ratio or 0))
    return results


# ── 综合评分：收益 + 捕获率 + 买/卖命中率 + 回撤 ──────────────────
@dataclass
class StrategyAnalysis:
    label: str
    total_return: float
    capture_ratio: float
    buy_hit_rate: float       # 买点命中率 %（买在波段下半区 = 命中）
    sell_hit_rate: float      # 卖点命中率 %（卖在波段上半区 = 命中）
    max_drawdown: float       # 最大回撤 %（越小 = 止损越快）
    trade_count: int
    score: float              # 综合分 0-100
    name: str = ""            # 战法 key（前端据此请求下一步买卖点）

    def to_dict(self) -> dict:
        return asdict(self)


def _hit_rates(bars: list[dict], bt: BacktestResult) -> tuple[float, float]:
    """买点/卖点命中率：买在持仓波段下半区算买点命中，卖在上半区算卖点命中。"""
    closes = [b["close"] for b in bars]
    date_to_idx = {b["date"]: i for i, b in enumerate(bars)}
    buy_hits, sell_hits = [], []
    for t in bt.trades:
        bi = date_to_idx.get(t["buy_date"])
        si = date_to_idx.get(t["sell_date"])
        if bi is None or si is None or si <= bi:
            continue
        window = closes[bi:si + 1]
        lo, hi = min(window), max(window)
        if hi <= lo:
            continue
        buy_pos = (t["buy_price"] - lo) / (hi - lo)   # 0=最低 1=最高
        sell_pos = (hi - t["sell_price"]) / (hi - lo) # 0=最高 1=最低
        buy_hits.append(1 if buy_pos <= 0.5 else 0)
        sell_hits.append(1 if sell_pos <= 0.5 else 0)
    buy_rate = round(sum(buy_hits) / len(buy_hits) * 100, 1) if buy_hits else 0.0
    sell_rate = round(sum(sell_hits) / len(sell_hits) * 100, 1) if sell_hits else 0.0
    return buy_rate, sell_rate


def analyze_strategies(bars: list[dict], exit_for=None) -> list[StrategyAnalysis]:
    """综合分析：收益 + 捕获率 + 买点命中率 + 卖点命中率 + 最大回撤 → 综合评分。

    综合分 = 收益35% + 捕获率30% + 买点命中17.5% + 卖点命中17.5%（各归一化到 0-100）。
    exit_for: 可选回调 (strategy_name) -> exit_params，用于按持有周期改用不同止损止盈。
    """
    closes = [b["close"] for b in bars]
    date_to_idx = {b["date"]: i for i, b in enumerate(bars)}
    rows = []

    for name in STRATEGIES:
        sig = strategy_signal(name, bars)
        ep = exit_for(name) if exit_for else _exit_params_for(name)
        bt = run_backtest(bars, sig, ep, label=STRATEGY_LABEL[name])

        # 捕获率
        captures = []
        for t in bt.trades:
            bi = date_to_idx.get(t["buy_date"])
            si = date_to_idx.get(t["sell_date"])
            if bi is None or si is None or si <= bi:
                continue
            window = closes[bi:si + 1]
            mx, mn = max(window), min(window)
            if mn <= 0:
                continue
            amplitude = (mx - mn) / mn * 100
            if amplitude < 1.0:
                continue
            actual = (t["sell_price"] - t["buy_price"]) / t["buy_price"] * 100
            captures.append(actual / amplitude * 100)
        cap = round(sum(captures) / len(captures), 1) if captures else 0.0

        buy_rate, sell_rate = _hit_rates(bars, bt)
        rows.append(dict(
            name=name,
            label=STRATEGY_LABEL[name],
            total_return=bt.total_return,
            capture_ratio=cap,
            buy_hit_rate=buy_rate,
            sell_hit_rate=sell_rate,
            max_drawdown=bt.max_drawdown,
            trade_count=bt.trade_count,
        ))

    # 归一化收益/捕获率到 0-100（min-max）
    rets = [r["total_return"] for r in rows]
    caps = [r["capture_ratio"] for r in rows]
    r_lo, r_hi = min(rets), max(rets)
    c_lo, c_hi = min(caps), max(caps)

    def norm(v, lo, hi):
        return 0.0 if hi == lo else (v - lo) / (hi - lo) * 100

    out = []
    for r in rows:
        r_n = norm(r["total_return"], r_lo, r_hi)
        c_n = norm(r["capture_ratio"], c_lo, c_hi)
        score = round(0.35 * r_n + 0.30 * c_n + 0.175 * r["buy_hit_rate"] + 0.175 * r["sell_hit_rate"], 1)
        out.append(StrategyAnalysis(
            name=r["name"], label=r["label"], total_return=r["total_return"], capture_ratio=r["capture_ratio"],
            buy_hit_rate=r["buy_hit_rate"], sell_hit_rate=r["sell_hit_rate"],
            max_drawdown=r["max_drawdown"], trade_count=r["trade_count"], score=score,
        ))

    out.sort(key=lambda x: -x.score)
    return out


# ── 参数寻优 ─────────────────────────────────────────────────────
@dataclass
class OptimizeResult:
    label: str
    params: dict
    total_return: float
    win_rate: float
    buy_hit_rate: float
    sell_hit_rate: float
    max_drawdown: float
    trade_count: int
    name: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def optimize_strategy(bars: list[dict], name: str, top: int = 3) -> list[OptimizeResult]:
    """单战法参数寻优：遍历参数网格，按总收益降序返回 top 组。"""
    grid = PARAM_GRIDS.get(name, [{}])
    results = []
    for prm in grid:
        sig = strategy_signal(name, bars, prm)
        bt = run_backtest(bars, sig, _exit_params_for(name), label=STRATEGY_LABEL[name])
        br, sr = _hit_rates(bars, bt)
        results.append(OptimizeResult(
            name=name, label=STRATEGY_LABEL[name], params=prm, total_return=bt.total_return,
            win_rate=bt.win_rate, buy_hit_rate=br, sell_hit_rate=sr,
            max_drawdown=bt.max_drawdown, trade_count=bt.trade_count,
        ))
    results.sort(key=lambda r: -r.total_return)
    return results[:top]


def optimize_all(bars: list[dict], sort_by: str = "return", min_trades: int = 3) -> list[OptimizeResult]:
    """全战法寻优：每个战法取最优参数。

    sort_by="return" 按总收益排名；sort_by="win_rate" 按胜率排名（剔除交易次数过少的假高胜率）。
    """
    best = []
    for name in STRATEGIES:
        opts = optimize_strategy(bars, name, top=1)
        if opts:
            best.append(opts[0])
    if sort_by == "win_rate":
        eligible = [r for r in best if r.trade_count >= min_trades]
        eligible.sort(key=lambda r: (-r.win_rate, -r.total_return))
        return eligible
    best.sort(key=lambda r: -r.total_return)
    return best


def best_for_trapped(bars: list[dict], min_trades: int = 3, top: int = 5) -> list[OptimizeResult]:
    """被套降本推荐：优先高胜率 + 主动做T（排除长线持有/趋势这类「不动」的战法）。

    面向「被套想降成本」的场景：胜率优先，且要求有足够交易次数（说明在主动做T）。
    """
    active = [name for name in STRATEGIES if name not in ("value", "ma_align", "momentum", "donchian", "keltner", "obv")]
    rows = []
    for name in active:
        opts = optimize_strategy(bars, name, top=3)
        for o in opts:
            if o.trade_count >= min_trades:
                rows.append(o)
    rows.sort(key=lambda r: (-r.win_rate, -r.total_return))
    return rows[:top]
