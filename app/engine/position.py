"""模块2：仓位管理引擎（强制风控）+ 分步降杠杆路径。"""
from __future__ import annotations

from .. import config
from ..schemas import Account, DeleveragePlan, DeleverageStep, Mode, PositionGate


def get_gate(account: Account, mode: Mode) -> PositionGate:
    """仓位门控：任何买入必须先通过。返回是否可买 + 阻塞原因。"""
    limits = config.get_position_limits(mode)
    single_max = limits["single_max"]
    total_max = limits["total_max"]
    leverage_max = limits["leverage_max"]
    cash_min = limits["cash_min"]

    blockers: list[str] = []

    if account.total_assets <= 0:
        blockers.append("总资产为 0，无法计算仓位")

    if account.position_ratio > total_max:
        blockers.append(f"总仓位 {account.position_ratio}% 已超上限 {total_max}%，请先减仓")

    if account.financing_ratio > leverage_max:
        blockers.append(f"融资比例 {account.financing_ratio}% 已超上限 {leverage_max}%，请先降杠杆")

    if account.cash_ratio < cash_min:
        blockers.append(f"现金储备 {account.cash_ratio}% 低于最低 {cash_min}%")

    for p in account.positions:
        if account.total_assets > 0:
            single_ratio = round(p.market_value / account.total_assets * 100, 2)
            if single_ratio > single_max:
                blockers.append(f"{p.name} 单只 {single_ratio}% 超上限 {single_max}%，不可再加仓")

    return PositionGate(
        mode=mode,
        single_max=single_max,
        total_max=total_max,
        leverage_max=leverage_max,
        cash_min=cash_min,
        can_buy=len(blockers) == 0,
        blockers=blockers,
    )


def _linear(current: float, target: float, cumulative: float) -> float:
    """当前值 → 目标值 的线性插值（cumulative 0-1）。"""
    return round(current - (current - target) * cumulative, 2)


def compute_deleverage_plan(account: Account, mode: Mode) -> DeleveragePlan:
    """分 3 步降杠杆路径：每步给目标仓位/融资/现金 + 时间窗口 + 触发条件。"""
    limits = config.get_position_limits(mode)
    cfg = config.get_deleverage()
    ratios = cfg["step_ratios"]
    windows = cfg["time_windows"]
    rebound = cfg["rebound_pct"]

    current = {
        "total_position": account.position_ratio,
        "financing": account.financing_ratio,
        "cash": account.cash_ratio,
    }
    target = {
        "total_position": limits["total_max"],
        "financing": limits["leverage_max"],
        "cash": limits["cash_min"],
    }

    gap_total = current["total_position"] - target["total_position"]
    gap_fin = current["financing"] - target["financing"]
    gap_cash = target["cash"] - current["cash"]

    steps: list[DeleverageStep] = []
    # 无需降杠杆时不生成步骤
    if gap_total <= 0 and gap_fin <= 0 and gap_cash <= 0:
        return DeleveragePlan(mode=mode, current=current, target=target, steps=steps)

    cumulative = 0.0
    triggers = [
        "优先执行🔴必卖指令（ST/基本面恶化/硬止损），回收资金偿还融资",
        "执行🟠建议卖（时间止损/技术卖点）；反弹即挂单，不追低割肉",
        "收尾减仓至目标：达到现金储备与融资上限，进入纪律持有",
    ]
    for i in range(cfg["steps"]):
        cumulative += ratios[i]
        step_total = _linear(current["total_position"], target["total_position"], cumulative)
        step_fin = _linear(current["financing"], target["financing"], cumulative)
        step_cash = _linear(current["cash"], target["cash"], cumulative)

        sell_pct = round(current["total_position"] - step_total, 2)
        amount = round(account.total_assets * sell_pct / 100, 0) if account.total_assets else 0
        steps.append(
            DeleverageStep(
                step=i + 1,
                target_total_position=step_total,
                target_financing=step_fin,
                target_cash=step_cash,
                time_window=windows[i],
                trigger=triggers[i],
                description=(
                    f"减仓 {sell_pct}%（约 ¥{amount:,.0f}），"
                    f"总仓位 {current['total_position']}%→{step_total}%，"
                    f"融资 {current['financing']}%→{step_fin}%；"
                    f"反弹 +{rebound}% 即挂单卖出，不追低"
                ),
            )
        )

    return DeleveragePlan(mode=mode, current=current, target=target, steps=steps)
