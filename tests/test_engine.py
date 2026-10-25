"""核心引擎单元测试。"""
from __future__ import annotations

import pytest

from app.engine import health, market_env, position, quality, sell
from app.schemas import (
    Account,
    Mode,
    Pool,
    Position,
    PositionColor,
    QualityInfo,
    Rating,
    SellLevel,
    SellMechanism,
)


# ── 质量筛选器 ───────────────────────────────────────────────────
def test_rating_excellent():
    r, _ = quality.classify_rating(roe_ttm=20, growth_yoy=15)
    assert r == Rating.EXCELLENT


def test_rating_qualified():
    r, _ = quality.classify_rating(roe_ttm=8, growth_yoy=5)
    assert r == Rating.QUALIFIED


def test_rating_warning_loss():
    r, _ = quality.classify_rating(roe_ttm=-3, growth_yoy=None)
    assert r == Rating.WARNING


def test_rating_warning_growth_crash():
    r, _ = quality.classify_rating(roe_ttm=5, growth_yoy=-25)
    assert r == Rating.WARNING


def test_rating_blacklist_st():
    r, _ = quality.classify_rating(roe_ttm=5, growth_yoy=5, is_st=True)
    assert r == Rating.BLACKLIST


def test_pool_core_hot_value():
    assert quality.classify_pool(Rating.EXCELLENT) == Pool.CORE
    assert quality.classify_pool(Rating.QUALIFIED, rps=95) == Pool.HOT
    assert quality.classify_pool(Rating.QUALIFIED, pb=0.8) == Pool.VALUE
    assert quality.classify_pool(Rating.QUALIFIED) == Pool.WATCH
    assert quality.classify_pool(Rating.BLACKLIST) == Pool.BLACKLIST


# ── 健康度评分卡 ─────────────────────────────────────────────────
def test_score_financing():
    assert health.score_financing_ratio(20) == 25
    assert health.score_financing_ratio(40) == 15
    assert health.score_financing_ratio(60) == 5
    assert health.score_financing_ratio(80) == 0


def test_score_pnl():
    assert health.score_pnl(15) == 25
    assert health.score_pnl(5) == 15
    assert health.score_pnl(-5) == 15
    assert health.score_pnl(-15) == 8
    assert health.score_pnl(-25) == 0


def test_base_color():
    assert health.base_position_color(Rating.QUALIFIED, 0) == PositionColor.GREEN
    assert health.base_position_color(Rating.WARNING, 0) == PositionColor.YELLOW
    assert health.base_position_color(Rating.QUALIFIED, -20) == PositionColor.ORANGE
    assert health.base_position_color(Rating.BLACKLIST, 0) == PositionColor.RED
    assert health.base_position_color(None, -30) == PositionColor.RED


def test_refine_color():
    assert health.refine_color(PositionColor.GREEN, [SellMechanism.HARD_STOP]) == PositionColor.RED
    assert health.refine_color(PositionColor.GREEN, [SellMechanism.TECHNICAL]) == PositionColor.ORANGE
    assert health.refine_color(PositionColor.ORANGE, [SellMechanism.FUNDAMENTAL]) == PositionColor.RED


def test_compute_health_emergency():
    acc = Account(
        positions=[
            Position(code="600000", name="A", shares=1000, cost_price=10, current_price=7, rating=Rating.WARNING),
            Position(code="600001", name="B", shares=1000, cost_price=10, current_price=8, rating=Rating.QUALIFIED),
        ],
        cash=0,
        financing_balance=50000,
    )
    env = market_env.score_market_env(turnover_ratio=0.5, adv_decl_ratio=0.5, index_above_ma20=False)
    hs = health.compute_health(acc, env)
    assert hs.total < 30
    assert hs.mode == Mode.EMERGENCY


# ── 仓位管理 ─────────────────────────────────────────────────────
def test_gate_blocks_when_over():
    acc = Account(
        positions=[Position(code="600000", name="A", shares=100000, cost_price=10, current_price=10)],
        cash=0,
        financing_balance=0,
    )
    gate = position.get_gate(acc, Mode.EMERGENCY)
    assert gate.can_buy is False
    assert gate.blockers


def test_deleverage_three_steps():
    acc = Account(
        positions=[Position(code="600000", name="A", shares=100000, cost_price=10, current_price=10)],
        cash=0,
        financing_balance=50000,
    )
    plan = position.compute_deleverage_plan(acc, Mode.EMERGENCY)
    assert len(plan.steps) == 3
    assert plan.steps[-1].target_total_position <= 60


# ── 卖出处理器 ───────────────────────────────────────────────────
def test_hard_stop_trigger():
    p = Position(code="600000", name="A", shares=1000, cost_price=10, current_price=9.6)
    c = sell.check_hard_stop(p, Mode.EMERGENCY)  # -3% 止损线 = 9.7
    assert c is not None
    assert c.mechanism == SellMechanism.HARD_STOP


def test_hard_stop_skip_deep_underwater():
    p = Position(code="600000", name="A", shares=1000, cost_price=10, current_price=7)
    assert sell.check_hard_stop(p, Mode.EMERGENCY) is None  # 深套不机械割


def test_time_stop_trigger():
    p = Position(code="600000", name="A", shares=1000, cost_price=10, current_price=10.05)
    c = sell.check_time_stop(p, Mode.EMERGENCY, days_held=8, last_high=10.5)
    assert c is not None
    assert c.mechanism == SellMechanism.TIME_STOP


def test_fundamental_stop_blacklist():
    p = Position(code="600000", name="A", shares=1000, cost_price=10, current_price=10)
    q = QualityInfo(code="600000", name="A", rating=Rating.BLACKLIST, pool=Pool.BLACKLIST, is_st=True, reasons=["ST"])
    c = sell.check_fundamental_stop(p, q)
    assert c is not None
    assert c.mechanism == SellMechanism.FUNDAMENTAL


def test_level_and_flight_downgrade():
    assert sell.level_from_score(85) == SellLevel.RED
    assert sell.level_from_score(70) == SellLevel.ORANGE
    assert sell.level_from_score(50) == SellLevel.YELLOW
    assert sell.level_from_score(25) == SellLevel.GREEN
    assert sell.level_from_score(10) == SellLevel.HOLD
    assert sell._downgrade(SellLevel.RED) == SellLevel.ORANGE


def test_flight_defense_and():
    ok, _ = sell.check_flight_defense(95, 3, True)
    assert ok
    ok2, _ = sell.check_flight_defense(95, 1, True)
    assert ok2 is False


def test_scan_positions_sorted():
    acc = Account(
        positions=[
            Position(code="600000", name="A", shares=1000, cost_price=10, current_price=9.5, rating=Rating.QUALIFIED),
            Position(code="600001", name="B", shares=1000, cost_price=10, current_price=10.0, rating=Rating.BLACKLIST),
        ],
        cash=0,
        financing_balance=0,
    )
    q = {
        "600000": QualityInfo(code="600000", name="A", rating=Rating.QUALIFIED, pool=Pool.WATCH),
        "600001": QualityInfo(code="600001", name="B", rating=Rating.BLACKLIST, pool=Pool.BLACKLIST, is_st=True, reasons=["ST"]),
    }
    sigs = sell.scan_positions(acc, Mode.EMERGENCY, q)
    assert len(sigs) == 2
    assert sigs[0].severity_score >= sigs[1].severity_score


# ── 市场环境 ─────────────────────────────────────────────────────
def test_market_env_cold():
    env = market_env.score_market_env(turnover_ratio=0.5, adv_decl_ratio=0.5, index_above_ma20=False)
    assert env.score < 4
    assert env.temperature.value == "cold"
