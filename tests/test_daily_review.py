"""每日复盘：风格判断 + 板块 + 持仓对比。"""
from __future__ import annotations

from app.engine import daily_review


def test_defensive_style():
    sectors = [{"name": "酿酒行业", "pct": 2.4}, {"name": "金融行业", "pct": 1.1},
               {"name": "电子器件", "pct": -3.6}, {"name": "机械行业", "pct": -3.5}]
    indices = [{"name": "沪深300", "pct": -0.2}]
    pos = [{"code": "000858", "name": "五粮液", "pct": 1.75, "mv": 22665, "pnl_ratio": -36.4},
           {"code": "603501", "name": "豪威集团", "pct": 1.09, "mv": 50436, "pnl_ratio": -29.5}]
    r = daily_review.build(sectors, indices, {"limit_up": 63, "limit_down": 25}, pos)
    assert r["style"]["label"] == "防守"
    assert r["style"]["spread"] > 1.5
    assert r["portfolio"]["vs_index"] > 0
    assert "防守型市场" in r["summary"]


def test_offensive_style():
    sectors = [{"name": "酿酒行业", "pct": -2.0}, {"name": "电子器件", "pct": 3.5},
               {"name": "机械行业", "pct": 3.0}]
    r = daily_review.build(sectors, [{"name": "沪深300", "pct": 1.0}],
                           {"limit_up": 90, "limit_down": 5}, [])
    assert r["style"]["label"] == "进攻"


def test_heat_level():
    assert daily_review._heat_level(90, 5, 5) == "过热"
    assert daily_review._heat_level(63, 25, -5.9) == "偏冷"
    assert daily_review._heat_level(30, 20, 1) in ("中性", "偏热")


# 历史复盘落库
def test_daily_review_persist(tmp_path, monkeypatch):
    from app.data import db

    data = {"style": {"label": "防守", "spread": 2.65},
            "portfolio": {"pct": 1.04, "vs_index": 1.24},
            "heat": {"level": "偏冷"}, "summary": "今天是防守型市场"}
    db.save_daily_review("2099-01-01", data)
    rows = db.list_daily_reviews(5)
    hit = [r for r in rows if r["date"] == "2099-01-01"]
    assert hit and hit[0]["style"] == "防守"
    assert hit[0]["vs_index"] == 1.24
    # 同日覆盖
    db.save_daily_review("2099-01-01", {**data, "summary": "覆盖后"})
    rows = [r for r in db.list_daily_reviews(5) if r["date"] == "2099-01-01"]
    assert len(rows) == 1 and rows[0]["summary"] == "覆盖后"
