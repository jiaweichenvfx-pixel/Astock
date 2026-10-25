"""一键决策（回测 + 寻优 + 样本外验收 + 买卖点）的规则与结构测试。"""
from __future__ import annotations

from app.engine import playbook


def _bars(closes: list[float]) -> list[dict]:
    return [
        {"date": f"2025-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", "open": c, "high": c * 1.01,
         "low": c * 0.99, "close": c, "volume": 1_000_000}
        for i, c in enumerate(closes)
    ]


def _wave(n: int = 260) -> list[float]:
    """下跌 → 横盘 → 上涨，保证各战法都能产生交易。"""
    closes = [120 - i * 0.35 for i in range(90)]
    closes += [88 + ((i % 7) - 3) * 0.9 for i in range(70)]
    closes += [88 + i * 0.55 for i in range(100)]
    return closes[:n]


# ── 采用规则（核心：样本外没赢就不能用调参）────────────────────
def test_decide_keeps_default_when_not_tuned():
    use, overfit, note = playbook._decide(tuned=False, is_gain=0, oos_edge=0)
    assert use is False and overfit is False
    assert "不用调" in note


def test_decide_adopts_when_oos_wins():
    use, overfit, _ = playbook._decide(tuned=True, is_gain=1.0, oos_edge=2.0)
    assert use is True and overfit is False


def test_decide_flags_overfit_when_is_gain_lost_oos():
    use, overfit, note = playbook._decide(tuned=True, is_gain=8.3, oos_edge=0.0)
    assert use is False
    assert overfit is True
    assert "过拟合" in note


def test_decide_rejects_weak_edge_without_overfit_flag():
    use, overfit, note = playbook._decide(tuned=True, is_gain=0.5, oos_edge=0.3)
    assert use is False and overfit is False
    assert "噪声" in note


# ── 整条流水线 ───────────────────────────────────────────────────
def test_playbook_structure():
    bars = _bars(_wave())
    r = playbook.run_playbook("600000", "测试股", bars)
    assert r["ok"] is True
    assert 2 <= len(r["candidates"]) <= 3
    assert r["split"]["is_bars"] + r["split"]["oos_bars"] == len(bars)
    ch = r["chosen"]
    for key in ("name", "label", "params", "oos_return", "why"):
        assert key in ch
    assert r["plan"]["ok"] is True
    assert r["plan"]["strategy"] == ch["name"]
    # 买卖点里必须用的就是最终采用的参数
    for k, v in ch["params"].items():
        assert r["plan"]["params"][k] == v


def test_playbook_rejects_short_history():
    r = playbook.run_playbook("600000", "测试股", _bars([10.0] * 50))
    assert r["ok"] is False
    assert "K线不足" in r["error"]


def test_playbook_candidates_ranked_by_oos():
    r = playbook.run_playbook("600000", "测试股", _bars(_wave()))
    oos = [c["oos_return"] for c in r["candidates"]]
    assert oos == sorted(oos, reverse=True)
    # 第一名就是被选中的战法
    assert r["candidates"][0]["name"] == r["chosen"]["name"]
