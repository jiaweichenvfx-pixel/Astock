"""后台预热：让页面打开的一瞬间就有数据，而不是干等 20 秒。

背景：全市场快照只能走新浪兜底源（东财被限频），冷抓一次 10~25 秒。
如果等用户请求时才抓，首屏就要等这么久。这里起一个守护线程，按节奏在后台把
「行情快照 → 决策快照（summary）→ 精选选股（screener）」依次算好放进内存缓存，
接口只读缓存（几十毫秒）。

节奏：
- 交易时段（工作日 9:00~15:30）每 2 分钟刷一次行情
- 非交易时段每 30 分钟刷一次（盘后数据基本不变）
- 选股扫描偏重（约 10 秒），单独按 10 分钟节奏
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

log = logging.getLogger("astock.warmup")

SPOT_INTERVAL_OPEN = 120
SPOT_INTERVAL_CLOSED = 1800
SCREENER_INTERVAL = 600


def in_trading_session(now: datetime | None = None) -> bool:
    """粗略判断是否在 A 股交易时段（工作日 9:00~15:30，不含节假日）。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 <= minutes <= 15 * 60 + 30


class Warmer:
    """守护线程：周期刷新缓存。失败只记日志，绝不影响主服务。"""

    def __init__(self, interval_open: int = SPOT_INTERVAL_OPEN,
                 interval_closed: int = SPOT_INTERVAL_CLOSED):
        self.interval_open = interval_open
        self.interval_closed = interval_closed
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_summary_ts = 0.0
        self.last_screener_ts = 0.0
        self.last_error: str | None = None
        self.runs = 0

    # ── 生命周期 ────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="astock-warmer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        time.sleep(2)  # 等应用把其他初始化跑完
        while not self._stop.is_set():
            try:
                self.warm_once()
            except Exception as e:                      # pragma: no cover - 兜底
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("预热失败：%s", self.last_error)
            self._stop.wait(self.next_delay())

    def next_delay(self) -> int:
        return self.interval_open if in_trading_session() else self.interval_closed

    # ── 一次预热 ────────────────────────────────────────────────
    def warm_once(self, include_screener: bool | None = None) -> dict:
        from . import api
        from .data import adapter

        started = time.time()
        adapter.get_index_snapshot()                    # 指数条（/api/market 用）
        api.get_summary(force=True)                     # 内部会刷新行情快照
        self.last_summary_ts = time.time()

        if include_screener is None:
            include_screener = (time.time() - self.last_screener_ts) >= SCREENER_INTERVAL
        if include_screener:
            api.get_screener(force=True)
            self.last_screener_ts = time.time()

        # 自选池迭代：每天一次（入选 + 复盘剔除 + 事后验证）
        try:
            from .engine import watchpool
            from .data import adapter as _ad
            key = f"wp_run_{_ad.data_day()}"
            if not _ad._cache_get(key, 86400):
                _ad._cache_set(key, {"ran": True})
                try:
                    from . import storage as _st
                    from .engine import horizon_weights as _hw
                    _acc = _st.load_account()
                    _sum = api.get_summary()
                    _info = _hw.compute(_acc, _sum.get("health") or {})
                    # 与手动「更新池子」用同一套引擎：按历史排名选战法，而不是退回 screener
                    from .engine import factor as _f
                    try:
                        _strat = {k: _f.best_strategy(k) for k in ("short", "mid", "long")}
                    except Exception:
                        _strat = {}
                    watchpool.daily_update(api.get_screener(), strategies=_strat,
                                           pool_sizes=_info["pool_size"])
                except Exception:
                    pass
        except Exception as e:
            self.last_error = f"watchpool: {type(e).__name__}: {e}"

        # 持仓战法体检：每天算一次（结果按日期缓存，算过就秒回）
        try:
            from . import storage
            from .engine import portfolio_bt
            acc = storage.load_account()
            portfolio_bt.ensure_today([{"code": p.code, "name": p.name} for p in acc.positions])
        except Exception as e:
            self.last_error = f"portfolio_bt: {type(e).__name__}: {e}"

        self.runs += 1
        self.last_error = None
        elapsed = round(time.time() - started, 1)
        log.info("预热完成 #%d，用时 %.1fs", self.runs, elapsed)
        return {"runs": self.runs, "elapsed": elapsed}


warmer = Warmer()
