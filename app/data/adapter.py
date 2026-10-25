"""数据适配层：封装 akshare，统一接口 + 本地缓存 + 失败降级。

原则：任何一次网络失败都不抛异常给上层，返回 None/空，由上层降级处理。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from ..config import DATA_DIR

# 缓存目录（保留用于兼容旧逻辑的可选文件缓存；主缓存已迁到 SQLite）
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

try:
    import akshare as ak
    HAS_AKSHARE = True
except Exception:  # pragma: no cover
    ak = None
    HAS_AKSHARE = False

# 缓存 TTL（秒）—— 按数据类型合理设置，减少重复抓取
TTL_SPOT = 120          # 行情快照：2 分钟（盘中够新鲜）
TTL_HISTORY = 21600     # 日线/分钟线：6 小时（收盘前更新，避免频繁重拉）
TTL_CALENDAR = 86400 * 7
TTL_MACRO = 600         # 宏观：10 分钟
TTL_FINANCIAL = 86400 * 7  # 财务指标：7 天（季报级别，变化很小）

# 全市场快照抓取锁：避免多线程冷启动时重复抓取 5000+ 只行情
_SPOT_LOCK = threading.Lock()

# akshare 全局锁：内部 py_mini_racer(V8) 不是线程安全的，后台预热线程与请求线程并发会 SIGTRAP 崩进程。
# 单用户场景下串行化网络抓取代价很小，换来进程稳定。
_AK_LOCK = threading.RLock()


def _ak_call(fn, *args, **kwargs):
    """串行执行 akshare 调用：内部 py_mini_racer 非线程安全，并发会崩进程。"""
    with _AK_LOCK:
        return fn(*args, **kwargs)


def _cache_get(key: str, ttl: float) -> Optional[dict]:
    """从 SQLite 读缓存，超 TTL 返回 None。"""
    from .db import cache_get
    return cache_get(key, ttl)


def _cache_set(key: str, data: dict) -> None:
    """写入 SQLite 缓存。"""
    from .db import cache_set
    cache_set(key, data)


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


# ── 实时快照 ─────────────────────────────────────────────────────
def _fetch_em_spot() -> Optional[dict[str, dict]]:
    """东方财富全市场快照（直连 requests + 默认 UA，绕过系统代理）。

    akshare 内部用浏览器 UA 会被东财反爬拒（RemoteDisconnected），
    这里直接用 requests 默认 UA + 禁用代理抓，稳定返回含 PE/PB/市值/60日涨幅 的全字段。
    """
    try:
        import requests
    except Exception:
        return None
    url = "https://82.push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "100", "po": "1", "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2", "invt": "2",
        "fid": "f12", "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048",
        "fields": "f2,f3,f6,f8,f9,f10,f12,f14,f20,f23,f24",
    }
    rows: dict[str, dict] = {}
    pn = 1
    fetched_total = 0
    expected_total = None
    complete = False
    try:
        while pn <= 60:  # 最多 60 页（6000 只）
            p = dict(params)
            p["pn"] = str(pn)
            r = requests.get(url, params=p, timeout=15, proxies={"http": None, "https": None})
            if r.status_code != 200:
                break
            data = (r.json() or {}).get("data") or {}
            diff = data.get("diff") or []
            if not diff:
                break
            total = data.get("total", 0)
            if expected_total is None:
                expected_total = total
            for it in diff:
                code = str(it.get("f12", ""))
                if not code:
                    continue
                rows[code] = {
                    "name": it.get("f14", ""),
                    "price": _to_float(it.get("f2")),
                    "pct_chg": _to_float(it.get("f3")),
                    "turnover": _to_float(it.get("f6")),          # 元
                    "turnover_rate": _to_float(it.get("f8")),
                    "volume_ratio": _to_float(it.get("f10")),
                    "pe": _to_float(it.get("f9")),
                    "pb": _to_float(it.get("f23")),
                    "market_cap": _to_float(it.get("f20")),        # 元
                    "chg_60d": _to_float(it.get("f24")),
                }
            fetched_total += len(diff)
            if expected_total and fetched_total >= expected_total:
                complete = True
                break
            pn += 1
            time.sleep(0.3)  # 分页间隔，降低触发东财限频的概率
    except Exception:
        pass
    # 只有完整抓取才返回，否则交给新浪兜底（避免残缺数据导致持仓现价缺失）
    if complete and len(rows) > 4000:
        return rows
    return None


def _fetch_sina_spot() -> Optional[dict[str, dict]]:
    """新浪全市场快照（慢、字段少，作为兜底）。失败返回 None。"""
    df = _ak_call(ak.stock_zh_a_spot)
    if df is None or df.empty:
        return None
    rows: dict[str, dict] = {}
    for _, r in df.iterrows():
        code = str(r.get("代码", ""))
        if not code:
            continue
        code = code[-6:] if not code.isdigit() else code  # 新浪代码带 sh/sz/bj 前缀，取后 6 位
        rows[code] = {
            "name": r.get("名称", ""),
            "price": _to_float(r.get("最新价")),
            "pct_chg": _to_float(r.get("涨跌幅")),
            "turnover": _to_float(r.get("成交额")),          # 元
            "turnover_rate": None,
            "volume_ratio": None,
            "pe": None,
            "pb": None,
            "market_cap": None,
            "chg_60d": None,
        }
    return rows


def get_spot_snapshot(force: bool = False) -> Optional[dict]:
    """获取全市场实时快照：优先东方财富，失败降级新浪。缓存 120s。

    返回 {code: {name, price, pct_chg, turnover(元), turnover_rate, volume_ratio,
                 pe, pb, market_cap(元), chg_60d}}

    并发保护：整表抓取要 20~30 秒，多线程（summary + market）同时冷启动时
    只让第一个线程真正抓取，其余线程等锁后直接读缓存。
    """
    if not HAS_AKSHARE:
        return None
    cached = _cache_get("spot", TTL_SPOT)
    if cached and not force:
        return cached.get("rows")

    with _SPOT_LOCK:
        cached = _cache_get("spot", TTL_SPOT)
        if cached and not force:
            return cached.get("rows")

        rows = None
        source = None
        try:
            rows = _fetch_em_spot()
            source = "em"
        except Exception:
            rows = None
        if not rows:
            try:
                rows = _fetch_sina_spot()
                source = "sina"
            except Exception:
                rows = None

        if rows:
            _cache_set("spot", {"rows": rows, "source": source})
            return rows
        return cached.get("rows") if cached else None


def get_spot_snapshot_cached(max_age: float = 1800) -> Optional[dict]:
    """只读缓存的行情快照（允许 max_age 秒内的旧数据），绝不触发网络抓取。"""
    if not HAS_AKSHARE:
        return None
    cached = _cache_get("spot", max_age)
    return cached.get("rows") if cached else None


def _to_float(v) -> Optional[float]:
    try:
        if v is None or v == "" or v == "-":
            return None
        return float(v)
    except (ValueError, TypeError):
        return None


def get_quotes(codes: list[str]) -> dict[str, dict]:
    """按代码取行情；快照里没有的（含港股）用腾讯行情补。"""
    spot = get_spot_snapshot() or {}
    out: dict[str, dict] = {}
    missing: list[str] = []
    for c in codes:
        if c in spot:
            out[c] = spot[c]
        else:
            missing.append(c)
    if missing:
        try:
            out.update(get_tencent_quotes(missing))
        except Exception:
            pass
    return out


def resolve_name_to_code(name: str) -> Optional[str]:
    """按股票名称解析 6 位代码（精确匹配优先，其次包含匹配）。"""
    if not name or not HAS_AKSHARE:
        return None
    spot = get_spot_snapshot()
    if not spot:
        return None
    for c, r in spot.items():
        if r.get("name") == name:
            return c
    clean = name.replace(" ", "")
    for c, r in spot.items():
        if clean and clean in (r.get("name") or "").replace(" ", ""):
            return c
    return None


def is_st(name: str) -> bool:
    return "ST" in name.upper() or "退" in name


# ── 交易日历 ─────────────────────────────────────────────────────
def get_trade_days() -> Optional[list[str]]:
    """全部交易日（YYYY-MM-DD），缓存 7 天。"""
    if not HAS_AKSHARE:
        return None
    cached = _cache_get("calendar", TTL_CALENDAR)
    if cached and cached.get("days"):
        return cached["days"]
    try:
        df = _ak_call(ak.tool_trade_date_hist_sina)
        days = [str(d) for d in df["trade_date"].tolist()]
        _cache_set("calendar", {"days": days})
        return days
    except Exception:
        return cached.get("days") if cached else None


def latest_trade_day(today: Optional[date] = None) -> Optional[str]:
    today = today or date.today()
    days = get_trade_days()
    if not days:
        return None
    t = today.isoformat()
    past = [d for d in days if d <= t]
    return past[-1] if past else None


def data_day(now: Optional[datetime] = None) -> str:
    """当前应采用的「数据日期」= 最近一个已收盘的交易日。

    周末/节假日回退到上一个交易日；交易日 09:30 前回退到前一交易日（当天还没有数据）。
    """
    now = now or datetime.now()
    d = now.date()
    days = get_trade_days()
    if not days:
        while d.weekday() >= 5:          # 拿不到日历时至少跳过周末
            d -= timedelta(days=1)
        return d.isoformat()
    t = d.isoformat()
    if t in days and now.hour * 60 + now.minute >= 9 * 60 + 30:
        return t
    past = [x for x in days if x < t]
    return past[-1] if past else t


def next_trade_day(after: Optional[date] = None) -> Optional[str]:
    """after 之后的第一个交易日（用于休市时安排下单）。"""
    days = get_trade_days()
    if not days:
        d = (after or date.today()) + timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d.isoformat()
    t = (after or date.today()).isoformat()
    future = [x for x in days if x > t]
    return future[0] if future else None


def compute_days_held(buy_date: Optional[str], today: Optional[date] = None) -> Optional[int]:
    """自然日持仓天数。"""
    if not buy_date:
        return None
    today = today or date.today()
    try:
        d0 = datetime.strptime(buy_date, "%Y-%m-%d").date()
        return (today - d0).days
    except ValueError:
        return None


def is_trading_day(d: Optional[date] = None) -> bool:
    d = d or date.today()
    days = get_trade_days()
    if not days:
        return True  # 无法判断时默认按交易日处理
    return d.isoformat() in days


# ── 历史日线 ─────────────────────────────────────────────────────
def _sina_symbol(code: str) -> str:
    """新浪代码前缀：6→sh，0/3→sz，8/4/9→bj。"""
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    return "bj" + code


def _bad_price(v) -> bool:
    """是否为坏价格（None 或 NaN）。"""
    return v is None or (isinstance(v, float) and v != v)  # v != v 检测 NaN


def _clean_bars(rows: list[dict]) -> list[dict]:
    """过滤掉价格含 None/NaN 的坏 K 线。"""
    return [
        b for b in rows
        if not (_bad_price(b["open"]) or _bad_price(b["high"]) or _bad_price(b["low"]) or _bad_price(b["close"]))
    ]


def _fetch_em_history(code: str, start_date: str) -> Optional[list[dict]]:
    df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start_date, adjust="qfq")
    if df is None or df.empty:
        return None
    return [
        {
            "date": str(r.get("日期", "")),
            "open": _to_float(r.get("开盘")),
            "high": _to_float(r.get("最高")),
            "low": _to_float(r.get("最低")),
            "close": _to_float(r.get("收盘")),
            "volume": _to_float(r.get("成交量")),
        }
        for _, r in df.iterrows()
    ]


def _fetch_sina_history(code: str) -> Optional[list[dict]]:
    """新浪日线。新浪会限频，重试 3 次并递增间隔。"""
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_daily(symbol=_sina_symbol(code), adjust="qfq")
            if df is None or df.empty:
                continue
            rows = [
                {
                    "date": str(r.get("date", "")),
                    "open": _to_float(r.get("open")),
                    "high": _to_float(r.get("high")),
                    "low": _to_float(r.get("low")),
                    "close": _to_float(r.get("close")),
                    "volume": _to_float(r.get("volume")),
                }
                for _, r in df.iterrows()
            ]
            rows = _clean_bars(rows)   # 剔除 NaN/None 坏 bar
            if rows:
                return rows
        except Exception:
            pass
        time.sleep(1 + attempt)  # 递增重试间隔 1s/2s/3s
    return None


def _resample_weekly(bars: list[dict]) -> list[dict]:
    """日线 → 周线重采样（按 ISO 年-周分组）。"""
    from collections import defaultdict
    from datetime import datetime

    weeks: dict = defaultdict(list)
    for b in bars:
        try:
            dt = datetime.strptime(str(b["date"])[:10], "%Y-%m-%d")
            weeks[dt.isocalendar()[:2]].append(b)
        except ValueError:
            continue
    out = []
    for key in sorted(weeks):
        g = weeks[key]
        highs = [b["high"] for b in g if b["high"] is not None]
        lows = [b["low"] for b in g if b["low"] is not None]
        if not highs or not lows:
            continue
        out.append({
            "date": g[-1]["date"],
            "open": g[0]["open"],
            "high": max(highs),
            "low": min(lows),
            "close": g[-1]["close"],
            "volume": sum(b["volume"] or 0 for b in g),
        })
    return out


def _fetch_sina_minute(code: str, period: str) -> Optional[list[dict]]:
    """新浪分钟线（30/60/120）。新浪会限频，重试 3 次并递增间隔。"""
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_minute(symbol=_sina_symbol(code), period=period, adjust="qfq")
            if df is not None and not df.empty:
                rows = [
                    {
                        "date": str(r.get("day", "")),
                        "open": _to_float(r.get("open")),
                        "high": _to_float(r.get("high")),
                        "low": _to_float(r.get("low")),
                        "close": _to_float(r.get("close")),
                        "volume": _to_float(r.get("volume")),
                    }
                    for _, r in df.iterrows()
                ]
                rows = _clean_bars(rows)   # 剔除 NaN/None 坏 bar
                if rows:
                    return rows
        except Exception:
            pass
        time.sleep(1 + attempt)  # 递增重试间隔 1s/2s/3s
    return None


def _fetch_hk_history(code: str, days: int) -> Optional[list[dict]]:
    """港股日线（腾讯前复权）。格式：[date, open, close, high, low, volume, ...]"""
    import requests

    try:
        r = requests.get(
            "https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get",
            params={"param": f"hk{code},day,,,{max(days, 160)},qfq"},
            timeout=15, proxies={"http": None, "https": None}, headers=FLOW_HEADERS,
        )
        node = ((r.json() or {}).get("data") or {}).get(f"hk{code}") or {}
        klines = node.get("qfqday") or node.get("day") or []
        rows = []
        for k in klines:
            if len(k) < 6:
                continue
            rows.append({
                "date": str(k[0]),
                "open": _to_float(k[1]),
                "close": _to_float(k[2]),
                "high": _to_float(k[3]),
                "low": _to_float(k[4]),
                "volume": _to_float(k[5]),
            })
        return _clean_bars(rows) or None
    except Exception:
        return None


def get_stock_history(code: str, days: int = 250, period: str = "daily") -> Optional[list[dict]]:
    """K线历史。period: daily / weekly / 30 / 60 / 120（分钟）。

    days 表示返回的 K 线根数。缓存。返回按时间升序的 [{date, open, high, low, close, volume}]。
    """
    if not HAS_AKSHARE:
        return None
    with _AK_LOCK:
        return _get_stock_history_locked(code, days, period)


def _get_stock_history_locked(code: str, days: int, period: str) -> Optional[list[dict]]:
    key = f"hist_{code}_{period}_{days}"
    cached = _cache_get(key, TTL_HISTORY)
    if cached and cached.get("rows"):
        return cached["rows"]

    if is_hk_code(code):
        if period == "weekly":
            daily = get_stock_history(code, days=max(300, days * 6), period="daily")
            rows = _resample_weekly(daily)[-days:] if daily else None
        elif period in ("30", "60", "120"):
            rows = None          # 港股分钟线暂无稳定来源
        else:
            rows = _fetch_hk_history(code, days)
        if rows:
            rows = rows[-days:]
            _cache_set(key, {"rows": rows})
        return rows or (cached.get("rows") if cached else None)

    rows = None
    if period == "weekly":
        # 周线：先取足够日线再重采样
        daily = get_stock_history(code, days=max(300, days * 6), period="daily")
        if daily:
            rows = _resample_weekly(daily)[-days:]
    elif period in ("30", "60", "120"):
        try:
            rows = _fetch_sina_minute(code, period)
        except Exception:
            rows = None
    else:
        # 日线：优先新浪（可靠、不受东财限频影响），东财做兜底
        rows = _fetch_sina_history(code) if HAS_AKSHARE else None
        if not rows:
            try:
                start_date = (date.today() - timedelta(days=int(days * 1.7))).strftime("%Y%m%d")
                rows = _fetch_em_history(code, start_date)
            except Exception:
                rows = None

    if rows:
        rows = rows[-days:]
        _cache_set(key, {"rows": rows})
        return rows
    return cached.get("rows") if cached else None


# ── 财务指标（ROE / 增速）───────────────────────────────────────
def get_financial_indicators(code: str) -> Optional[dict]:
    """最新一期财务指标：{roe_ttm, growth_yoy, report_date}。

    直接复用 get_financial_trend 的数据（同一次 akshare 调用），避免重复拉取。
    """
    if not HAS_AKSHARE:
        return None
    key = f"fin_{code}"
    cached = _cache_get(key, TTL_FINANCIAL)
    if cached and "roe_ttm" in cached:
        return cached

    trend = get_financial_trend(code)
    if not trend or not trend.get("series"):
        return cached
    latest = trend["series"][-1]
    out = {"roe_ttm": latest.get("roe"), "growth_yoy": latest.get("growth"),
           "report_date": latest.get("date")}
    _cache_set(key, out)
    return out


def get_financial_trend(code: str, periods: int = 8) -> Optional[dict]:
    """财务趋势：最近若干报告期的扣非净利同比增速 + 净资产收益率。

    返回 {series:[{date, roe, growth}], latest, consecutive_up, consecutive_down, report_date}
    growth = 扣非净利同比（与去年同期同一报告期比较）。
    """
    if not HAS_AKSHARE:
        return None
    key = f"fintrend_{code}"
    cached = _cache_get(key, TTL_FINANCIAL)
    if cached and "series" in cached:
        return cached

    try:
        df = _ak_call(ak.stock_financial_analysis_indicator, symbol=code,
                      start_year=str(date.today().year - 3))
        if df is None or df.empty:
            return cached
        df = df.sort_values("日期")
        profit_col = "扣除非经常性损益后的净利润(元)"
        roe_col = "净资产收益率(%)"
        if profit_col not in df.columns:
            return cached

        rows: list[dict] = []
        for _, r in df.iterrows():
            rows.append({
                "date": str(r.get("日期", ""))[:10],
                "profit": _to_float(r.get(profit_col)),
                "roe": _to_float(r.get(roe_col)),
            })
        by_date = {x["date"]: x for x in rows}

        series: list[dict] = []
        for x in rows:
            d = x["date"]
            if len(d) < 10:
                continue
            prev = by_date.get(str(int(d[:4]) - 1) + d[4:])
            g = None
            if prev and prev["profit"] and prev["profit"] > 0 and x["profit"] is not None:
                g = round((x["profit"] / prev["profit"] - 1) * 100, 2)
            series.append({"date": d, "roe": x["roe"], "growth": g})

        series = [s for s in series if s["growth"] is not None][-periods:]
        # 连续同向变化（阈值 3 个百分点，过滤噪声）
        up = down = 0
        for i in range(len(series) - 1, 0, -1):
            delta = series[i]["growth"] - series[i - 1]["growth"]
            if delta <= -3:
                down += 1
            elif delta >= 3:
                up += 1
            else:
                break

        out = {
            "series": series,
            "latest": series[-1] if series else None,
            "consecutive_up": up,
            "consecutive_down": down,
            "report_date": series[-1]["date"] if series else None,
        }
        _cache_set(key, out)
        return out
    except Exception:
        return cached


# ── 港股判定 / 腾讯行情补充源 ────────────────────────────────────
FLOW_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}

TX_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}


def is_hk_code(code: str) -> bool:
    """港股：5 位数字（如 01810）。A 股是 6 位。"""
    c = str(code or "").strip()
    return len(c) == 5 and c.isdigit()


def _tx_symbol(code: str) -> str:
    """腾讯代码前缀：sh/sz/hk。"""
    c = str(code or "").strip()
    if is_hk_code(c):
        return "hk" + c
    if c.startswith(("6", "9")):
        return "sh" + c
    return "sz" + c


def _num(parts: list[str], idx: int) -> Optional[float]:
    try:
        v = float(parts[idx])
        return v
    except (ValueError, IndexError):
        return None


def parse_tencent_quote(line: str) -> Optional[tuple[str, dict]]:
    """解析腾讯一行行情：v_sh600519="1~名称~代码~现价~...";

    返回 (code, row) 或 None。字段下标（A 股 88 段）：
      1 名称 / 3 现价 / 32 涨跌幅 / 37 成交额(万) / 38 换手率 /
      39 PE / 45 总市值(亿) / 46 PB / 49 量比
    """
    if not line or "=" not in line:
        return None
    try:
        head, body = line.split("=", 1)
        if not head.startswith("v_"):
            return None
        body = body.strip().rstrip(";").strip('"')
        parts = body.split("~")
        if len(parts) < 50:
            return None
        code = parts[2].strip()
        name = parts[1].strip()
        price = _num(parts, 3)
        if not name or not code or price is None or price <= 0:
            return None

        hk = is_hk_code(code)
        pe = _num(parts, 39)
        pb = None if hk else _num(parts, 46)          # 港股 46 是英文名，不是 PB
        mcap = _num(parts, 45)                        # 两种口径都是「总市值(亿)」
        amount = _num(parts, 37)
        if amount is None:
            amount_yi = None
        else:
            amount_yi = round(amount / 1e4, 2) if not hk else round(amount / 1e8, 2)

        return code, {
            "name": name,
            "price": price,
            "pct_chg": _num(parts, 32),
            "turnover_rate": _num(parts, 38),
            "pe": pe if (pe is not None and pe > 0) else None,
            "pb": pb if (pb is not None and pb > 0) else None,
            "market_cap": mcap * 1e8 if (mcap is not None and mcap > 0) else None,
            "volume_ratio": _num(parts, 49),
            "amount_yi": amount_yi,
            "is_hk": hk,
        }
    except Exception:
        return None


def get_tencent_quotes(codes: list[str]) -> dict[str, dict]:
    """腾讯行情批量补充（PE/PB/市值/换手/量比）。分批 + 逐只缓存。"""
    import requests

    out: dict[str, dict] = {}
    need: list[str] = []
    for c in codes:
        c = str(c).strip()
        if not c:
            continue
        hit = _cache_get(f"tx_{c}", TTL_SPOT)
        if hit and hit.get("price"):
            out[c] = hit
        else:
            need.append(c)
    if not need:
        return out

    for i in range(0, len(need), 50):
        batch = need[i:i + 50]
        url = "http://qt.gtimg.cn/q=" + ",".join(_tx_symbol(c) for c in batch)
        try:
            resp = requests.get(url, headers=TX_HEADERS, timeout=8)
            text = resp.content.decode("gbk", errors="ignore")
        except Exception:
            continue
        for line in text.split("\n"):
            parsed = parse_tencent_quote(line)
            if not parsed:
                continue
            code, row = parsed
            _cache_set(f"tx_{code}", row)
            out[code] = row
    return out


# ── 指数 ─────────────────────────────────────────────────────────
INDEX_CODES = [
    ("sh000001", "上证指数"), ("sz399001", "深证成指"), ("sz399006", "创业板指"),
    ("sh000688", "科创50"), ("sh000300", "沪深300"),
]


def get_index_snapshot() -> list[dict]:
    """主要指数行情（腾讯）。缓存 60s。"""
    import requests

    cached = _cache_get("index_snap", 60)
    if cached and cached.get("rows"):
        return cached["rows"]
    url = "http://qt.gtimg.cn/q=" + ",".join(s for s, _ in INDEX_CODES)
    rows: list[dict] = []
    try:
        resp = requests.get(url, headers=TX_HEADERS, timeout=8)
        text = resp.content.decode("gbk", errors="ignore")
        for line in text.split("\n"):
            if "=" not in line:
                continue
            try:
                body = line.split("=", 1)[1].strip().rstrip(";").strip('"')
                parts = body.split("~")
                if len(parts) < 33:
                    continue
                rows.append({"name": parts[1].strip(), "price": _num(parts, 3),
                             "pct": _num(parts, 32)})
            except Exception:
                continue
    except Exception:
        return cached["rows"] if cached else []
    if rows:
        _cache_set("index_snap", {"rows": rows})
    return rows


def get_index_above_ma20() -> bool:
    """上证指数是否站上 20 日线（市场环境打分用）。缓存 1 小时。"""
    cached = _cache_get("idx_ma20", 3600)
    if cached and "above" in cached:
        return bool(cached["above"])
    above = True
    try:
        df = _ak_call(ak.stock_zh_index_daily, symbol="sh000001")
        closes = [float(x) for x in df["close"].tolist()][-20:]
        if len(closes) >= 20:
            above = closes[-1] > sum(closes) / len(closes)
    except Exception:
        pass
    _cache_set("idx_ma20", {"above": above})
    return above


# ── 涨跌家数（市场宽度）─────────────────────────────────────────
def get_market_breadth(fetch: bool = True, max_age: float = 1800) -> dict:
    """全市场涨跌家数 / 涨跌停 / 成交额。fetch=False 时只读缓存。"""
    spot = get_spot_snapshot() if fetch else get_spot_snapshot_cached(86400 * 30)
    if not spot:
        return {}
    # 兼容两种返回：原始 spot dict 或 已算好的 breadth
    if "total" in spot and "up" in spot:
        return spot

    total = up = down = flat = limit_up = limit_down = 0
    turnover = 0.0
    sum_pct = 0.0
    for code, r in spot.items():
        pct = r.get("pct_chg")
        if pct is None:
            continue
        total += 1
        turnover += r.get("turnover") or 0
        sum_pct += pct
        if pct > 0:
            up += 1
        elif pct < 0:
            down += 1
        else:
            flat += 1
        lim = limit_pct_for(code, r.get("name") or "")
        if pct >= lim - 0.1:
            limit_up += 1
        elif pct <= -(lim - 0.1):
            limit_down += 1

    out = {
        "total": total, "up": up, "down": down, "flat": flat,
        "limit_up": limit_up, "limit_down": limit_down,
        "up_ratio": round(up / total * 100, 2) if total else None,
        "avg_pct": round(sum_pct / total, 2) if total else None,
        "adv_decl_ratio": round(up / down, 2) if down else 999.0,
        "turnover_yi": round(turnover / 1e8, 0),
    }
    return out


def get_market_env_raw() -> dict:
    """市场环境原始数据（打分用）。"""
    b = get_market_breadth() or {}
    return {
        "adv_decl_ratio": b.get("adv_decl_ratio"),
        "turnover_yi": b.get("turnover_yi"),
        "up_count": b.get("up"),
        "down_count": b.get("down"),
        "up_ratio": b.get("up_ratio"),
        "avg_pct": b.get("avg_pct"),
        "limit_up": b.get("limit_up"),
        "limit_down": b.get("limit_down"),
    }


def get_sector_spot() -> list[dict]:
    """行业板块涨跌（新浪）。缓存 120s。"""
    if not HAS_AKSHARE:
        return []
    cached = _cache_get("sector_spot", 120)
    if cached and cached.get("rows"):
        return cached["rows"]
    try:
        df = _ak_call(ak.stock_sector_spot, indicator="新浪行业")
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "name": str(r.get("板块", "")),
                "pct": _to_float(r.get("涨跌幅")),
                "count": _to_float(r.get("公司家数")),
            })
        rows.sort(key=lambda x: -(x["pct"] or -999))
        _cache_set("sector_spot", {"rows": rows})
        return rows
    except Exception:
        return cached["rows"] if cached else []


# ── 资金流（东财）────────────────────────────────────────────────
def get_fund_flow(code: str) -> dict:
    """近 5 日主力资金净流入天数。失败降级为不可用。"""
    import requests

    if is_hk_code(code):
        return {"inflow_days": 0, "available": False}
    cached = _cache_get(f"flow_{code}", 1800)
    if cached and "inflow_days" in cached:
        return cached

    out = {"inflow_days": 0, "available": False}
    try:
        secid = f"{1 if str(code).startswith(('6', '9')) else 0}.{code}"
        url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
        params = {"lmt": "0", "klt": "101", "secid": secid,
                  "fields1": "f1,f2,f3,f7",
                  "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"}
        resp = requests.get(url, params=params, headers=FLOW_HEADERS, timeout=8)
        klines = ((resp.json() or {}).get("data") or {}).get("klines") or []
        days = 0
        for k in klines[-5:]:
            parts = str(k).split(",")
            if len(parts) > 1:
                try:
                    if float(parts[1]) > 0:
                        days += 1
                except ValueError:
                    pass
        out = {"inflow_days": days, "available": bool(klines)}
    except Exception:
        pass
    _cache_set(f"flow_{code}", out)
    return out


# ── 涨跌停幅度 ───────────────────────────────────────────────────
def limit_pct_for(code: str, name: str = "") -> float:
    """涨跌停幅度（%）：主板 10、创业板/科创 20、北交所 30、ST 5。"""
    c = str(code or "")
    if is_st(name or ""):
        return 5.0
    if c.startswith(("300", "301", "688", "689")):
        return 20.0
    if c.startswith(("4", "8", "920")):
        return 30.0
    return 10.0


# ── 外围宏观数据 ─────────────────────────────────────────────────
def get_macro_data() -> dict:
    """美股/黄金/美联储/美国CPI。任一失败则该项缺失，不抛异常。缓存 10 分钟。"""
    cached = _cache_get("macro_data", TTL_MACRO)
    if cached and cached.get("_ts_ok"):
        return cached
    out: dict = {"_ts_ok": True}
    if not HAS_AKSHARE:
        return {}

    def _us_stock() -> Optional[dict]:
        try:
            df = _ak_call(ak.index_us_stock_sina, symbol=".INX")
            closes = [float(x) for x in df["close"].tolist() if x == x]
            if len(closes) < 60:
                return None
            ma20 = sum(closes[-20:]) / 20
            return {"close": round(closes[-1], 2), "above_ma20": closes[-1] > ma20,
                    "chg_60d": round((closes[-1] / closes[-60] - 1) * 100, 2)}
        except Exception:
            return None

    def _gold() -> Optional[dict]:
        try:
            df = _ak_call(ak.futures_foreign_hist, symbol="GC")
            closes = [float(x) for x in df["close"].tolist()]
            if len(closes) < 60:
                return None
            return {"chg_60d": round((closes[-1] / closes[-60] - 1) * 100, 2)}
        except Exception:
            return None

    def _fed() -> Optional[dict]:
        try:
            df = _ak_call(ak.macro_bank_usa_interest_rate)
            vals = [v for v in df["今值"].tolist() if v == v]
            if not vals:
                return None
            latest = float(vals[-1])
            prev = float(vals[-2]) if len(vals) > 1 else None
            return {"latest": latest, "prev": prev}
        except Exception:
            return None

    def _cpi() -> Optional[dict]:
        try:
            df = _ak_call(ak.macro_usa_cpi_yoy)
            vals = [float(v) for v in df.iloc[:, -1].tolist() if v == v]
            return {"latest": round(vals[-1], 2)} if vals else None
        except Exception:
            return None

    for key, fn in (("us_stock", _us_stock), ("gold", _gold),
                    ("fed_rate", _fed), ("us_cpi", _cpi)):
        val = fn()
        if val:
            out[key] = val
    _cache_set("macro_data", out)
    return out
