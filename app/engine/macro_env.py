"""外围宏观环境评分：美联储利率 + 黄金避险 + 美国CPI + 美股趋势 → 风险偏好。

输出 0-10 分 + 风险评级（risk_on/neutral/risk_off）+ 各因素信号 + 策略调整建议。
"""
from __future__ import annotations

from typing import Optional


def score_macro(data: Optional[dict] = None) -> dict:
    """根据外围宏观数据评分。data 为空时返回中性。"""
    data = data or {}
    score = 5.0
    signals: list[tuple] = []   # (因素, 描述, 方向)

    # 1. 美股趋势（风险偏好）
    us = data.get("us_stock")
    if us and us.get("close") is not None:
        if us.get("above_ma20") and us.get("chg_60d", 0) > 0:
            score += 1.5
            signals.append(("美股", f"标普 {us['close']} 站上20日线，60日 {us['chg_60d']:+.1f}%", "risk_on"))
        elif not us.get("above_ma20"):
            score -= 1.5
            signals.append(("美股", f"标普 {us['close']} 跌破20日线", "risk_off"))
        else:
            signals.append(("美股", f"标普 {us['close']}（60日 {us['chg_60d']:+.1f}%）", "neutral"))

    # 2. 黄金（避险情绪）
    gold = data.get("gold")
    if gold and gold.get("chg_60d") is not None:
        if gold["chg_60d"] > 10:
            score -= 1.5
            signals.append(("黄金", f"60日涨 {gold['chg_60d']:+.1f}%，避险升温", "risk_off"))
        elif gold["chg_60d"] < -5:
            score += 1.0
            signals.append(("黄金", f"60日跌 {gold['chg_60d']:+.1f}%，避险降温", "risk_on"))
        else:
            signals.append(("黄金", f"60日 {gold['chg_60d']:+.1f}%", "neutral"))

    # 3. 美联储利率（货币政策）
    fed = data.get("fed_rate")
    if fed and fed.get("latest") is not None:
        if fed.get("prev") is not None and fed["latest"] < fed["prev"]:
            score += 1.5
            signals.append(("美联储", f"降息至 {fed['latest']}%", "risk_on"))
        elif fed.get("prev") is not None and fed["latest"] > fed["prev"]:
            score -= 1.5
            signals.append(("美联储", f"加息至 {fed['latest']}%", "risk_off"))
        else:
            signals.append(("美联储", f"利率维持 {fed['latest']}%", "neutral"))

    # 4. 美国CPI（通胀）
    cpi = data.get("us_cpi")
    if cpi and cpi.get("latest") is not None:
        if cpi["latest"] > 4:
            score -= 1.0
            signals.append(("美国CPI", f"{cpi['latest']}% 高通胀", "risk_off"))
        elif cpi["latest"] < 2:
            score += 1.0
            signals.append(("美国CPI", f"{cpi['latest']}% 温和", "risk_on"))
        else:
            signals.append(("美国CPI", f"{cpi['latest']}%", "neutral"))

    score = round(max(0.0, min(10.0, score)), 1)

    if score >= 6.5:
        rating, rating_label = "risk_on", "🟢 风险偏好"
        advice = "外围偏暖：可正常持仓/进攻，趋势与突破战法占优"
    elif score <= 3.5:
        rating, rating_label = "risk_off", "🔴 风险厌恶"
        advice = "外围风险高：建议降仓位、收紧止损、防守模式，回避高波动"
    else:
        rating, rating_label = "neutral", "🟡 中性"
        advice = "外围中性：维持当前仓位，轻仓参与，以低吸/做T为主"

    return {
        "score": score,
        "rating": rating,
        "rating_label": rating_label,
        "signals": [{"factor": f, "desc": d, "direction": dr} for f, d, dr in signals],
        "advice": advice,
    }
