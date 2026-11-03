"""每日复盘：一句话讲清今天是什么市场、哪些板块在涨、你的持仓跟不跟得上。

3 个问题：
  1. 今天是哪种市场？      老登（价值/防御）还是科技（成长/进攻）
  2. 哪些板块在涨、热度如何  领涨领跌 + 涨停家数 + 次新股（风险偏好）
  3. 我的持仓表现如何       今日涨跌、跑赢/跑输、哪只最好最差
"""
from __future__ import annotations

from typing import Optional

OLD_MONEY = {  # 价值 / 防御 / 高股息
    "酿酒行业", "船舶制造", "酒店旅游", "农林牧渔", "农药化肥", "发电设备", "服装鞋类",
    "金融行业", "物资外贸", "建筑建材", "摩托车", "钢铁行业", "食品行业", "商业百货",
    "水泥行业", "电器行业", "生物制药", "电力行业", "纺织行业", "供水供气", "综合行业",
    "公路桥梁", "交通运输", "家电行业", "造纸行业", "化纤行业", "房地产", "煤炭行业",
    "石油行业", "医疗器械", "环保行业",
}
TECH = {  # 成长 / 进攻 / 高波动
    "电子信息", "电子器件", "机械行业", "仪器仪表", "汽车制造", "玻璃行业",
    "陶瓷行业", "家具行业", "次新股", "化工行业", "其它行业", "塑料制品", "印刷包装",
}

# 持仓代码 → 所属板块（东财个股行业接口不稳，这里用小映射 + 关键词兜底）
CODE_SECTOR = {
    "600489": "有色金属", "600754": "酒店旅游", "601127": "汽车制造",
    "603049": "化工行业", "603099": "酒店旅游", "603501": "电子器件",
    "000858": "酿酒行业", "002895": "农药化肥", "300641": "化工行业",
    "300059": "金融行业", "01810": "港股-消费电子",
}
NAME_SECTOR = {
    "黄金": "有色金属", "酒店": "酒店旅游", "汽车": "汽车制造", "橡胶": "化工行业",
    "芯": "电子器件", "半导体": "电子器件", "电子": "电子器件", "酿": "酿酒行业",
    "酒": "酿酒行业", "磷": "农药化肥", "化工": "化工行业", "证券": "金融行业",
    "银行": "金融行业", "医药": "生物制药",
}


def _sector_of(code: str, name: str) -> str:
    if code in CODE_SECTOR:
        return CODE_SECTOR[code]
    for k, v in NAME_SECTOR.items():
        if k in (name or ""):
            return v
    return "未分类"


def _heat_level(limit_up: int, limit_down: int, new_pct: Optional[float]) -> str:
    """热度：涨停数 + 跌停数 + 次新股（风险偏好）综合。"""
    score = 0
    score += 2 if limit_up >= 80 else 1 if limit_up >= 40 else -1 if limit_up < 20 else 0
    score += -1 if limit_down >= 40 else 0
    if new_pct is not None:
        score += 2 if new_pct >= 3 else 1 if new_pct > 0 else -2 if new_pct <= -4 else -1
    return "过热" if score >= 3 else "偏热" if score >= 1 else "偏冷" if score <= -1 else "中性"


def _avg(vals: list[float]) -> Optional[float]:
    return sum(vals) / len(vals) if vals else None


def build(sectors: list[dict], indices: list[dict], breadth: dict,
          positions: list[dict], activity: Optional[dict] = None,
          fund_flow: Optional[list[dict]] = None) -> dict:
    """sectors: [{name, pct}]；indices: [{name, pct}]；positions: [{name, code, pct, mv, pnl_ratio}]"""
    old = [s["pct"] for s in sectors if s.get("name") in OLD_MONEY and s.get("pct") is not None]
    tech = [s["pct"] for s in sectors if s.get("name") in TECH and s.get("pct") is not None]
    old_avg, tech_avg = _avg(old), _avg(tech)
    spread = round((old_avg or 0) - (tech_avg or 0), 2) if old_avg is not None and tech_avg is not None else None

    if spread is None:
        style_label, style_plain = "未知", "板块数据不足"
    elif spread >= 1.5:
        style_label, style_plain = "防守", f"老登股（价值/防御）跑赢科技 {spread:.1f}pp"
    elif spread <= -1.5:
        style_label, style_plain = "进攻", f"科技股跑赢老登股 {abs(spread):.1f}pp"
    else:
        style_label, style_plain = "均衡", f"老登与科技接近（差 {spread:+.1f}pp）"

    ranked = sorted([s for s in sectors if s.get("pct") is not None], key=lambda x: -x["pct"])
    top = ranked[:5]
    bottom = ranked[-5:][::-1]

    # 行业资金净流入 / 流出
    flows = [f for f in (fund_flow or []) if f.get("net") is not None]
    inflow = sorted(flows, key=lambda x: -x["net"])[:5]
    outflow = sorted(flows, key=lambda x: x["net"])[:5]

    # 涨跌温度：真实涨停/跌停优先（剔除 ST）
    act = activity or {}
    def _num(k):
        v = act.get(k)
        try:
            return int(float(str(v).rstrip("%")))
        except Exception:
            return None
    real_up, real_down = _num("真实涨停"), _num("真实跌停")
    up_n, down_n = _num("上涨"), _num("下跌")
    flat_n, halt_n = _num("平盘"), _num("停牌")
    active_pct = act.get("活跃度")
    try:
        active_pct = float(str(active_pct).rstrip("%"))
    except Exception:
        active_pct = None

    up_sectors = sum(1 for s in ranked if s["pct"] > 0)
    limit_up, limit_down = breadth.get("limit_up") or 0, breadth.get("limit_down") or 0
    hot = next((s for s in ranked if s["name"] == "次新股"), None)
    hs300 = next((i["pct"] for i in indices if "沪深300" in (i.get("name") or "")), None)

    # 持仓
    rows = []
    for p in positions:
        if p.get("pct") is None:
            continue
        rows.append({
            "name": p["name"], "code": p["code"],
            "pct": round(p["pct"], 2),
            "sector": _sector_of(p["code"], p["name"]),
            "mv": p.get("mv"),
            "pnl_ratio": p.get("pnl_ratio"),
        })
    rows.sort(key=lambda r: -r["pct"])
    total_mv = sum(x["mv"] or 0 for x in rows) or 1
    port_pct = sum((x["pct"] * (x["mv"] or 0)) for x in rows) / total_mv if rows else None
    vs_index = round(port_pct - hs300, 2) if port_pct is not None and hs300 is not None else None

    # 一句话
    parts = [f"今天是{style_label}型市场，{style_plain}。"]
    if top:
        parts.append(f"领涨 {top[0]['name']}{top[0]['pct']:+.1f}%、{top[1]['name']}{top[1]['pct']:+.1f}%；")
    if bottom:
        parts.append(f"领跌 {bottom[0]['name']}{bottom[0]['pct']:+.1f}%、{bottom[1]['name']}{bottom[1]['pct']:+.1f}%。")
    if hot and hot["name"] not in [x["name"] for x in (top[:2] + bottom[:2])]:
        parts.append(f"次新股 {hot['pct']:+.1f}%（风险偏好{'走弱' if hot['pct'] < 0 else '回暖'}）。")
    if port_pct is not None:
        verb = "跑赢" if (vs_index or 0) > 0 else "跑输"
        parts.append(f"你的持仓 {port_pct:+.2f}%，{verb}沪深300 {abs(vs_index or 0):.2f}pp。")

    return {
        "breadth2": {
            "up": up_n, "down": down_n, "flat": flat_n, "halt": halt_n,
            "limit_up": _num("涨停"), "limit_down": _num("跌停"),
            "real_limit_up": real_up, "real_limit_down": real_down,
            "active_pct": active_pct,
            "up_ratio": round(up_n / (up_n + down_n) * 100, 1)
            if up_n is not None and down_n and (up_n + down_n) > 0 else None,
        },
        "fund_flow": {"inflow": inflow, "outflow": outflow,
                      "total_net": round(sum(f["net"] for f in flows), 1) if flows else None},
        "style": {"label": style_label, "plain": style_plain,
                  "old_money_pct": round(old_avg, 2) if old_avg is not None else None,
                  "tech_pct": round(tech_avg, 2) if tech_avg is not None else None,
                  "spread": spread},
        "sectors": {"top": top, "bottom": bottom,
                    "up_count": up_sectors, "total": len(ranked)},
        "heat": {"limit_up": limit_up, "limit_down": limit_down,
                 "new_listing_pct": round(hot["pct"], 1) if hot else None,
                 "level": _heat_level(limit_up, limit_down, hot["pct"] if hot else None)},
        "indices": indices,
        "portfolio": {"rows": rows, "pct": round(port_pct, 2) if port_pct is not None else None,
                      "vs_index": vs_index, "hs300": hs300,
                      "up": sum(1 for r in rows if r["pct"] > 0), "total": len(rows)},
        "summary": "".join(parts),
    }
