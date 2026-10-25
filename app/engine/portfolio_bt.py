"""持仓战法体检：每只持仓 × 短/中/长三档，各自给出前 5 名。

结果落库在 `bt_cache`（日期 × 代码 × 周期），所以：
- 同一只票同一天只算一次，重复打开页面不重复搜索；
- 每天一份，历史可回溯；
- 新增一只票只补算它自己那 3 行，不动其它票。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from ..data import adapter
from . import bt_store

TOP_N = 5


def _reshape(row: dict) -> dict:
    """把库里存的全量战法裁成前 5，并保持接口字段兼容。"""
    out = dict(row)
    out["top"] = (row.get("all") or [])[:TOP_N]
    out.pop("all", None)
    return out


def _items(positions: list[dict], day: str) -> dict:
    from . import recommend

    from .strategies import STRATEGIES

    data = bt_store.load_day(day, positions)
    data["strategy_count"] = len(STRATEGIES)
    for item in data["items"]:
        item["horizons"] = {h: _reshape(v) for h, v in item["horizons"].items()}
        for purpose in ("hold", "trade"):
            rec = recommend.recommend(item["code"], purpose, day)
            item.setdefault("recommend", {})[purpose] = (
                {"label": rec["label"], "strategy": rec["strategy"],
                 "horizon": rec["horizon"], "score": rec["rank_score"]}
                if rec.get("ok") else None)
    return data


def get(positions: Optional[list[dict]] = None, today: Optional[str] = None) -> dict:
    """只读：从库里读当天结果，不做任何计算。"""
    day = today or adapter.data_day()
    data = _items(positions or [], day)
    if data["missing"]:
        data["status"] = "computing" if data["missing"] == data["count"] * 3 else "partial"
    return data


def ensure_today(positions: list[dict], today: Optional[str] = None,
                 force: bool = False) -> dict:
    """补齐当天缺失的 (代码, 周期)；force=True 先清掉当天再重算。"""
    from ..data import db

    day = today or adapter.data_day()
    if force:
        db.clear_bt(day)
    stats = bt_store.ensure(positions, day)
    data = _items(positions, day)
    data["status"] = "ready"
    data["computed"] = stats["computed"]
    data["reused"] = stats["reused"]
    return data
