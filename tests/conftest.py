"""测试隔离：把 SQLite 指向临时文件，绝不动生产库 data/astock.db。

踩过的坑：有测试直接写 equity_snapshots / signals，把真实数据污染了
（净值曲线被算出 -99.8% 回撤、信号复盘被清空）。
"""
from __future__ import annotations

import pytest

from app.data import db


@pytest.fixture(autouse=True, scope="session")
def _isolated_db(tmp_path_factory):
    original = db.DB_PATH
    db.DB_PATH = tmp_path_factory.mktemp("astock-db") / "test.db"
    yield
    db.DB_PATH = original
