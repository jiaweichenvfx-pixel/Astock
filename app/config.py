"""配置加载：读取 config.yaml，提供类型化的访问。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from .schemas import Mode

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
DATA_DIR = ROOT / "data"
STATIC_DIR = ROOT / "static"


@lru_cache(maxsize=1)
def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_mode(mode: Mode) -> dict:
    return load_config()["account_modes"][mode.value]


def get_position_limits(mode: Mode) -> dict:
    return get_mode(mode)["position_limits"]


def get_strategy_weights(mode: Mode) -> dict:
    return load_config()["strategy_weights"][mode.value]


def get_exit_rules() -> dict:
    return load_config()["exit_rules"]


def get_quality() -> dict:
    return load_config()["quality"]


def get_scorecard() -> dict:
    return load_config()["health_scorecard"]


def get_market_env() -> dict:
    return load_config()["market_env"]


def get_deleverage() -> dict:
    return load_config()["deleverage"]


def get_fx() -> dict:
    """汇率配置（默认港币 0.8508）。"""
    return (load_config().get("fx") or {"hkd_cny": 0.8508})


def get_backtest() -> dict:
    return load_config()["backtest"]


def mode_for_score(score: float) -> Mode:
    """健康度总分 → 模式。左闭右开。"""
    modes = load_config()["account_modes"]
    for key in ("emergency", "defense", "balance", "attack"):
        trig = modes[key]["trigger"]
        lo, hi = trig["health_min"], trig["health_max"]
        if lo <= score < hi:
            return Mode(key)
    return Mode.ATTACK if score >= 70 else Mode.EMERGENCY
