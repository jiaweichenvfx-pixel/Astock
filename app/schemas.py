"""领域数据模型（dataclass）与枚举。

所有比例的分母 = 总资产（净资产 + 融资余额）。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


# ── 枚举 ─────────────────────────────────────────────────────────
class Mode(str, Enum):
    EMERGENCY = "emergency"
    DEFENSE = "defense"
    BALANCE = "balance"
    ATTACK = "attack"


class Rating(str, Enum):
    EXCELLENT = "excellent"   # ⭐卓越
    QUALIFIED = "qualified"   # ✅合格
    WARNING = "warning"       # ⚠️预警
    BLACKLIST = "blacklist"   # 🚫黑名单


class Pool(str, Enum):
    CORE = "core"             # ⭐核心池
    HOT = "hot"               # 🔥热点池
    VALUE = "value"           # 💎价值池
    WATCH = "watch"           # 📦观察池
    BLACKLIST = "blacklist"   # 🚫黑名单池


class PositionColor(str, Enum):
    GREEN = "green"           # 🟢
    YELLOW = "yellow"         # 🟡
    ORANGE = "orange"         # 🟠
    RED = "red"               # 🔴


class SellMechanism(str, Enum):
    HARD_STOP = "hard_stop"
    TIME_STOP = "time_stop"
    FUNDAMENTAL = "fundamental"
    TECHNICAL = "technical"


class SellLevel(str, Enum):
    RED = "red"       # 🔴 开盘市价卖出
    ORANGE = "orange"  # 🟠 今日内挂单
    YELLOW = "yellow"  # 🟡 3日内处理
    GREEN = "green"    # 🟢 预警跟踪
    HOLD = "hold"      # ✅ 坚定持有


class Temperature(str, Enum):
    HOT = "hot"
    MILD = "mild"
    COLD = "cold"


MODE_LABEL = {
    Mode.EMERGENCY: "🔴 急救模式",
    Mode.DEFENSE: "🟠 防守模式",
    Mode.BALANCE: "🟡 平衡模式",
    Mode.ATTACK: "🟢 进攻模式",
}

RATING_LABEL = {
    Rating.EXCELLENT: "⭐卓越",
    Rating.QUALIFIED: "✅合格",
    Rating.WARNING: "⚠️预警",
    Rating.BLACKLIST: "🚫黑名单",
}

POOL_LABEL = {
    Pool.CORE: "⭐核心池",
    Pool.HOT: "🔥热点池",
    Pool.VALUE: "💎价值池",
    Pool.WATCH: "📦观察池",
    Pool.BLACKLIST: "🚫黑名单池",
}

COLOR_LABEL = {
    PositionColor.GREEN: "🟢",
    PositionColor.YELLOW: "🟡",
    PositionColor.ORANGE: "🟠",
    PositionColor.RED: "🔴",
}

LEVEL_LABEL = {
    SellLevel.RED: "🔴 极高",
    SellLevel.ORANGE: "🟠 高",
    SellLevel.YELLOW: "🟡 中",
    SellLevel.GREEN: "🟢 低",
    SellLevel.HOLD: "✅ 极低",
}


# ── 持仓 ─────────────────────────────────────────────────────────
@dataclass
class Position:
    code: str                      # 6 位代码，如 "600519"
    name: str
    shares: int                    # 股数
    cost_price: float              # 成本价
    current_price: Optional[float] = None  # 现价（数据源填充）
    sector: Optional[str] = None
    buy_date: Optional[str] = None          # 建仓日 YYYY-MM-DD
    last_high: Optional[float] = None       # 建仓以来最高价（时间止损用）
    account_type: str = "普通"              # 普通 / 信用（信用账户持仓计入担保比例）
    rating: Optional[Rating] = None         # 质量评级（引擎填充）
    pool: Optional[Pool] = None

    @property
    def market_value(self) -> float:
        p = self.current_price if self.current_price is not None else self.cost_price
        return round(self.shares * p, 2)

    @property
    def pnl_ratio(self) -> float:
        """浮盈/浮亏百分比，+10 表示浮盈 10%"""
        if self.current_price is None or self.cost_price == 0:
            return 0.0
        return round((self.current_price / self.cost_price - 1) * 100, 2)

    @property
    def pnl_amount(self) -> float:
        if self.current_price is None:
            return 0.0
        return round((self.current_price - self.cost_price) * self.shares, 2)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "shares": self.shares,
            "cost_price": self.cost_price,
            "current_price": self.current_price,
            "sector": self.sector,
            "buy_date": self.buy_date,
            "last_high": self.last_high,
            "market_value": self.market_value,
            "pnl_ratio": self.pnl_ratio,
            "pnl_amount": self.pnl_amount,
            "rating": self.rating.value if self.rating else None,
            "rating_label": RATING_LABEL[self.rating] if self.rating else None,
            "pool": self.pool.value if self.pool else None,
            "pool_label": POOL_LABEL[self.pool] if self.pool else None,
        }


# ── 账户 ─────────────────────────────────────────────────────────
@dataclass
class Account:
    positions: list[Position] = field(default_factory=list)
    cash: float = 0.0               # 现金
    financing_balance: float = 0.0  # 融资余额（信用账户负债）
    credit_cash: float = 0.0        # 信用账户可用资金（计入担保比例）
    note: str = ""                  # 数据说明（如「成本为占位估算」）

    @property
    def position_value(self) -> float:
        return round(sum(p.market_value for p in self.positions), 2)

    @property
    def total_assets(self) -> float:
        return round(self.position_value + self.cash, 2)

    @property
    def net_assets(self) -> float:
        return round(self.total_assets - self.financing_balance, 2)

    @property
    def financing_ratio(self) -> float:
        """融资余额 / 总资产 * 100"""
        if self.total_assets <= 0:
            return 0.0
        return round(self.financing_balance / self.total_assets * 100, 2)

    @property
    def credit_position_value(self) -> float:
        """信用账户持仓市值（只算 account_type=信用 的持仓）。"""
        return round(sum(p.market_value for p in self.positions if p.account_type == "信用"), 2)

    @property
    def maintenance_ratio(self) -> float:
        """维持担保比例 = 信用账户总资产 / 融资负债 * 100（券商口径，<130% 强平）。

        注意：只算信用账户的资产，普通账户的钱不会救急，合并计算会虚高。
        """
        if self.financing_balance <= 0:
            return 0.0
        credit_assets = self.credit_position_value + self.credit_cash
        return round(credit_assets / self.financing_balance * 100, 2)

    @property
    def position_ratio(self) -> float:
        """总仓位 = 持仓市值 / 总资产 * 100"""
        if self.total_assets <= 0:
            return 0.0
        return round(self.position_value / self.total_assets * 100, 2)

    @property
    def cash_ratio(self) -> float:
        if self.total_assets <= 0:
            return 0.0
        return round(self.cash / self.total_assets * 100, 2)

    @property
    def overall_pnl_ratio(self) -> float:
        """账户整体浮盈/浮亏（相对持仓成本）"""
        total_cost = sum(p.shares * p.cost_price for p in self.positions)
        if total_cost <= 0:
            return 0.0
        return round((self.position_value / total_cost - 1) * 100, 2)

    @property
    def leverage_ratio(self) -> float:
        """真实杠杆 = 持仓市值 / 净资产 * 100（含融资口径，>100 表示满仓+借钱）。

        区别于 position_ratio（市值/总资产）：后者不含融资，降杠杆时会因分母缩小而失真。
        """
        if self.net_assets <= 0:
            return 0.0
        return round(self.position_value / self.net_assets * 100, 2)

    def to_dict(self) -> dict:
        return {
            "cash": self.cash,
            "financing_balance": self.financing_balance,
            "note": self.note,
            "position_value": self.position_value,
            "total_assets": self.total_assets,
            "net_assets": self.net_assets,
            "financing_ratio": self.financing_ratio,
            "position_ratio": self.position_ratio,
            "leverage_ratio": self.leverage_ratio,
            "cash_ratio": self.cash_ratio,
            "overall_pnl_ratio": self.overall_pnl_ratio,
            "maintenance_ratio": self.maintenance_ratio,
            "credit_position_value": self.credit_position_value,
            "credit_cash": self.credit_cash,
            "position_count": len(self.positions),
            "positions": [p.to_dict() for p in self.positions],
        }


# ── 市场环境 ─────────────────────────────────────────────────────
@dataclass
class MarketEnv:
    score: float = 0.0              # 0-10
    temperature: Temperature = Temperature.COLD
    turnover_ratio: Optional[float] = None   # 成交额 / 30日均量
    adv_decl_ratio: Optional[float] = None   # 涨跌家数比
    index_above_ma20: Optional[bool] = None
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "temperature": self.temperature.value,
            "temperature_label": {"hot": "🔥火热", "mild": "🌤️温和", "cold": "❄️寒冷"}[self.temperature.value],
            "turnover_ratio": self.turnover_ratio,
            "adv_decl_ratio": self.adv_decl_ratio,
            "index_above_ma20": self.index_above_ma20,
            "detail": self.detail,
        }


# ── 健康度评分卡 ─────────────────────────────────────────────────
@dataclass
class HealthScore:
    mode: Mode = Mode.DEFENSE
    total: int = 0
    financing_score: float = 0
    pnl_score: float = 0
    quality_score: float = 0
    concentration_score: float = 0
    market_env_score: float = 0
    financing_ratio: float = 0
    overall_pnl_ratio: float = 0
    position_colors: dict = field(default_factory=dict)  # code -> color
    blacklist_count: int = 0
    warning_count: int = 0

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "mode_label": MODE_LABEL[self.mode],
            "total": self.total,
            "financing_score": self.financing_score,
            "pnl_score": self.pnl_score,
            "quality_score": self.quality_score,
            "concentration_score": self.concentration_score,
            "market_env_score": self.market_env_score,
            "financing_ratio": self.financing_ratio,
            "overall_pnl_ratio": self.overall_pnl_ratio,
            "position_colors": {k: v.value for k, v in self.position_colors.items()},
            "blacklist_count": self.blacklist_count,
            "warning_count": self.warning_count,
        }


# ── 仓位门控 ─────────────────────────────────────────────────────
@dataclass
class PositionGate:
    mode: Mode
    single_max: float       # %
    total_max: float        # %
    leverage_max: float     # %
    cash_min: float         # %
    can_buy: bool
    blockers: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "mode_label": MODE_LABEL[self.mode],
            "single_max": self.single_max,
            "total_max": self.total_max,
            "leverage_max": self.leverage_max,
            "cash_min": self.cash_min,
            "can_buy": self.can_buy,
            "blockers": self.blockers,
        }


# ── 分步降杠杆 ───────────────────────────────────────────────────
@dataclass
class DeleverageStep:
    step: int
    target_total_position: float   # %
    target_financing: float        # %
    target_cash: float             # %
    time_window: str
    trigger: str
    description: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DeleveragePlan:
    mode: Mode
    current: dict
    target: dict
    steps: list[DeleverageStep] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "mode_label": MODE_LABEL[self.mode],
            "current": self.current,
            "target": self.target,
            "steps": [s.to_dict() for s in self.steps],
        }


# ── 卖出信号 ─────────────────────────────────────────────────────
@dataclass
class SellSignal:
    code: str
    name: str
    mechanism: SellMechanism
    severity_score: int             # 0-100
    level: SellLevel
    reason: str
    suggested_price: Optional[float] = None
    execution: str = "market_order"  # market_order / limit_order
    flight_defense: bool = False     # 是否触发卖飞防御降级
    flight_reasons: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "mechanism": self.mechanism.value,
            "mechanism_label": {
                SellMechanism.HARD_STOP: "硬止损",
                SellMechanism.TIME_STOP: "时间止损",
                SellMechanism.FUNDAMENTAL: "基本面止损",
                SellMechanism.TECHNICAL: "技术卖点",
            }[self.mechanism],
            "severity_score": self.severity_score,
            "level": self.level.value,
            "level_label": LEVEL_LABEL[self.level],
            "reason": self.reason,
            "suggested_price": self.suggested_price,
            "execution": self.execution,
            "flight_defense": self.flight_defense,
            "flight_reasons": self.flight_reasons,
        }


# ── 买点信号 ─────────────────────────────────────────────────────
@dataclass
class BuySignal:
    code: str
    name: str
    price: Optional[float] = None
    score: int = 0                   # 加权投票 -100..100
    level: str = "hold"              # strong / watch / hold
    level_label: str = "观望"
    rating: Optional[Rating] = None
    pool: Optional[Pool] = None
    signals: dict = field(default_factory=dict)   # 各策略 +1/-1/0
    reason: str = ""
    position_gate_pass: bool = True

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "price": self.price,
            "score": self.score,
            "level": self.level,
            "level_label": self.level_label,
            "rating": self.rating.value if self.rating else None,
            "rating_label": RATING_LABEL[self.rating] if self.rating else None,
            "pool": self.pool.value if self.pool else None,
            "pool_label": POOL_LABEL[self.pool] if self.pool else None,
            "signals": self.signals,
            "reason": self.reason,
            "position_gate_pass": self.position_gate_pass,
        }


# ── 质量信息 ─────────────────────────────────────────────────────
@dataclass
class QualityInfo:
    code: str
    name: str
    rating: Rating
    pool: Pool
    roe_ttm: Optional[float] = None
    growth_yoy: Optional[float] = None
    is_st: bool = False
    market_cap: Optional[float] = None       # 亿元
    pe_percentile: Optional[float] = None
    pb: Optional[float] = None
    audit_opinion: Optional[str] = None
    consecutive_loss_years: int = 0
    reasons: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "rating": self.rating.value,
            "rating_label": RATING_LABEL[self.rating],
            "pool": self.pool.value,
            "pool_label": POOL_LABEL[self.pool],
            "roe_ttm": self.roe_ttm,
            "growth_yoy": self.growth_yoy,
            "is_st": self.is_st,
            "market_cap": self.market_cap,
            "pe_percentile": self.pe_percentile,
            "pb": self.pb,
            "audit_opinion": self.audit_opinion,
            "consecutive_loss_years": self.consecutive_loss_years,
            "reasons": self.reasons,
        }
