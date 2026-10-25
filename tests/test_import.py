"""批量导入持仓：覆盖 / 合并 / 名称补齐 / 非法代码。"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import api
from app.schemas import Account, Position


@pytest.fixture()
def fake_storage(monkeypatch):
    acc = Account(cash=1000.0, financing_balance=0.0, positions=[
        Position(code="600000", name="旧持仓", shares=100, cost_price=10.0),
    ])
    saved = {}

    monkeypatch.setattr(api.storage, "load_account", lambda: acc)
    monkeypatch.setattr(api.storage, "save_account", lambda a: saved.__setitem__("acc", a))
    monkeypatch.setattr(api, "invalidate_cache", lambda: None)
    monkeypatch.setattr(api.adapter, "get_tencent_quotes",
                        lambda codes: {c: {"name": f"名称{c}"} for c in codes})
    return acc, saved


def test_import_replaces_positions(fake_storage):
    acc, saved = fake_storage
    body = api.ImportInput(
        rows=[api.ImportRow(code="600489", shares=1000, cost_price=18.5),
              api.ImportRow(code="000858", name="五粮液", shares=800, cost_price=70.0)],
        replace=True, cash=50000, financing_balance=650000,
    )
    out = api.import_positions(body)

    assert out["ok"] is True and out["imported"] == 2
    assert [p.code for p in acc.positions] == ["600489", "000858"]
    assert acc.cash == 50000 and acc.financing_balance == 650000
    assert acc.positions[0].name == "名称600489"      # 缺名称自动补
    assert acc.positions[1].name == "五粮液"
    assert saved["acc"] is acc


def test_import_merges_existing_by_code(fake_storage):
    acc, _ = fake_storage
    body = api.ImportInput(rows=[api.ImportRow(code="600000", shares=500, cost_price=12.0)])
    api.import_positions(body)

    assert len(acc.positions) == 1                    # 不新增，只更新
    assert acc.positions[0].shares == 500
    assert acc.positions[0].cost_price == 12.0


def test_import_rejects_bad_code(fake_storage):
    with pytest.raises(HTTPException) as e:
        api.import_positions(api.ImportInput(rows=[api.ImportRow(code="1234", shares=100, cost_price=1.0)]))
    assert e.value.status_code == 400


def test_import_rejects_empty(fake_storage):
    with pytest.raises(HTTPException) as e:
        api.import_positions(api.ImportInput(rows=[]))
    assert e.value.status_code == 400


def test_import_keeps_cash_when_not_given(fake_storage):
    acc, _ = fake_storage
    api.import_positions(api.ImportInput(
        rows=[api.ImportRow(code="600489", shares=100, cost_price=10.0)], replace=True))
    assert acc.cash == 1000.0        # 没传就不动


def test_import_marks_estimated(fake_storage):
    acc, _ = fake_storage
    api.import_positions(api.ImportInput(
        rows=[api.ImportRow(code="600489", shares=100, cost_price=10.0)], estimated=True))
    assert "占位" in acc.note
    assert "不可信" in acc.note


def test_import_clears_note_when_real(fake_storage):
    acc, _ = fake_storage
    acc.note = "占位估算"
    api.import_positions(api.ImportInput(
        rows=[api.ImportRow(code="600489", shares=1000, cost_price=18.5)], estimated=False))
    assert acc.note == ""
