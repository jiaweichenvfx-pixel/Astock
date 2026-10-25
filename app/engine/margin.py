"""融资仓的平仓风险评估。

券商口径：**维持担保比例 = 信用账户总资产 / 融资负债**。
- 低于警戒线（默认 150%）会被要求追加担保
- 低于平仓线（默认 130%）会被强制平仓

这里把「还能跌多少」换算成百分比，比看比例更直观：
可承受跌幅 = 1 − 线 / 当前担保比例。
"""
from __future__ import annotations

from .. import config
from ..schemas import Account


def rules() -> dict:
    cfg = config.load_config().get("margin") or {}
    return {
        "warning": float(cfg.get("warning_ratio", 150)),
        "liquidation": float(cfg.get("liquidation_ratio", 130)),
    }


def assess(account: Account) -> dict:
    """返回担保比例、警戒/平仓线、以及还能跌多少。"""
    r = rules()
    ratio = account.maintenance_ratio
    if account.financing_balance <= 0:
        return {"has_margin": False, "ratio": 0.0, "status": "none",
                "warning": r["warning"], "liquidation": r["liquidation"],
                "drop_to_warning": None, "drop_to_liquidation": None,
                "note": "无融资负债，不存在平仓风险"}

    drop_warn = round(max(0.0, (1 - r["warning"] / ratio) * 100), 1) if ratio > 0 else None
    drop_liq = round(max(0.0, (1 - r["liquidation"] / ratio) * 100), 1) if ratio > 0 else None

    if ratio <= r["liquidation"]:
        status, note = "danger", f"担保比例 {ratio}% 已触及平仓线 {r['liquidation']}%，随时可能被强平"
    elif ratio <= r["warning"]:
        status, note = "warning", f"担保比例 {ratio}% 低于警戒线 {r['warning']}%，需要补充担保或减仓"
    elif ratio < r["warning"] + 50:
        status, note = "warm", f"担保比例 {ratio}%，再跌 {drop_warn}% 触警戒线"
    else:
        status, note = "safe", f"担保比例 {ratio}%，距警戒线还有 {drop_warn}% 的下跌空间"

    return {
        "has_margin": True,
        "ratio": ratio,
        "status": status,
        "warning": r["warning"],
        "liquidation": r["liquidation"],
        "drop_to_warning": drop_warn,
        "drop_to_liquidation": drop_liq,
        "credit_assets": round(account.credit_position_value + account.credit_cash, 2),
        "liabilities": account.financing_balance,
        "note": note,
    }
