"""市场概况聚合：当前日期/周几、交易时段、主要指数、涨跌家数比、两市成交额。

供界面顶部「市场信息条」使用：一次返回前端需要的全部基础信息，
数据全部来自 adapter（行情快照 / 指数快照 / 交易日历），失败时逐项降级为 None。
"""
from __future__ import annotations

from datetime import datetime, time as dtime
from typing import Optional

from ..data import adapter

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _session(now: datetime, trading_day: bool) -> tuple[str, str]:
    """返回 (时段代码, 中文标签)。"""
    if not trading_day:
        return "closed", "休市"
    t = now.time()
    if t < dtime(9, 15):
        return "pre", "盘前"
    if t < dtime(9, 30):
        return "auction", "集合竞价"
    if t < dtime(11, 30):
        return "open", "交易中"
    if t < dtime(13, 0):
        return "noon", "午间休市"
    if t < dtime(15, 0):
        return "open", "交易中"
    return "post", "已收盘"


def _breadth_label(ratio: Optional[float]) -> str:
    """涨跌比 → 通俗描述。"""
    if ratio is None:
        return "暂无数据"
    if ratio >= 2:
        return "普涨"
    if ratio >= 1.2:
        return "涨多跌少"
    if ratio >= 0.83:
        return "涨跌均衡"
    if ratio >= 0.5:
        return "跌多涨少"
    return "普跌"


def build_market_info(now: Optional[datetime] = None) -> dict:
    """组装市场概况。now 可注入，便于测试。"""
    now = now or datetime.now()
    trading_day = adapter.is_trading_day(now.date())
    session, session_label = _session(now, trading_day)

    breadth = adapter.get_market_breadth(fetch=False, max_age=1800)
    if not breadth:            # 冷启动（本地无快照）才真正抓一次全市场
        breadth = adapter.get_market_breadth()
    indices = adapter.get_index_snapshot()

    ratio = breadth.get("adv_decl_ratio")
    if ratio is not None and ratio >= 900:  # 无一跌停时用 999 表示，前端显示 --
        ratio = None

    return {
        "date": now.date().isoformat(),
        "weekday": WEEKDAYS[now.weekday()],
        "time": now.strftime("%H:%M:%S"),
        "session": session,
        "session_label": session_label,
        "is_trading_day": trading_day,
        "trade_date": adapter.latest_trade_day(now.date()),
        "as_of": now.strftime("%Y-%m-%d %H:%M"),
        "indices": indices,
        "breadth": {
            "total": breadth.get("total"),
            "up": breadth.get("up"),
            "down": breadth.get("down"),
            "flat": breadth.get("flat"),
            "limit_up": breadth.get("limit_up"),
            "limit_down": breadth.get("limit_down"),
            "up_ratio": breadth.get("up_ratio"),
            "avg_pct": breadth.get("avg_pct"),
            "adv_decl_ratio": ratio,
            "label": _breadth_label(ratio),
        },
        "turnover_yi": breadth.get("turnover_yi"),
    }
