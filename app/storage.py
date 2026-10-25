"""本地 JSON 持久化：账户持仓/现金/融资。"""
from __future__ import annotations

from pathlib import Path

from .config import DATA_DIR
from .schemas import Account, Position

ACCOUNT_PATH = DATA_DIR / "account.json"
WATCHLIST_PATH = DATA_DIR / "watchlist.json"


def account_exists() -> bool:
    return ACCOUNT_PATH.exists()


def load_account() -> Account:
    if not ACCOUNT_PATH.exists():
        return Account()
    import json

    try:
        raw = json.loads(ACCOUNT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return Account()

    positions = []
    for p in raw.get("positions", []):
        positions.append(
            Position(
                code=str(p.get("code", "")),
                name=p.get("name", ""),
                shares=int(p.get("shares", 0)),
                cost_price=float(p.get("cost_price", 0)),
                sector=p.get("sector"),
                buy_date=p.get("buy_date"),
                last_high=p.get("last_high"),
                account_type=p.get("account_type") or "普通",
            )
        )
    return Account(
        positions=positions,
        cash=float(raw.get("cash", 0)),
        financing_balance=float(raw.get("financing_balance", 0)),
        credit_cash=float(raw.get("credit_cash", 0)),
        note=str(raw.get("note", "")),
    )


def save_account(account: Account) -> None:
    import json

    payload = {
        "cash": account.cash,
        "financing_balance": account.financing_balance,
        "credit_cash": account.credit_cash,
        "note": account.note,
        "positions": [
            {
                "code": p.code,
                "name": p.name,
                "shares": p.shares,
                "cost_price": p.cost_price,
                "sector": p.sector,
                "buy_date": p.buy_date,
                "last_high": p.last_high,
                "account_type": p.account_type,
            }
            for p in account.positions
        ],
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ACCOUNT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def seed_demo_account() -> Account:
    """首次运行：写入一个「满仓融资被套」的演示账户，方便立刻看到效果。"""
    positions = [
        Position(code="600519", name="贵州茅台", shares=100, cost_price=1650.0, sector="白酒", buy_date="2026-05-20", last_high=1700.0),
        Position(code="300750", name="宁德时代", shares=600, cost_price=210.0, sector="锂电池", buy_date="2026-06-02", last_high=228.0),
        Position(code="601012", name="隆基绿能", shares=5000, cost_price=28.5, sector="光伏", buy_date="2026-04-15", last_high=30.0),
        Position(code="000858", name="五粮液", shares=800, cost_price=155.0, sector="白酒", buy_date="2026-05-11", last_high=160.0),
        Position(code="600036", name="招商银行", shares=2000, cost_price=42.0, sector="银行", buy_date="2026-03-01", last_high=43.5),
        Position(code="002594", name="比亚迪", shares=400, cost_price=260.0, sector="汽车", buy_date="2026-05-28", last_high=275.0),
        Position(code="600030", name="中信证券", shares=3000, cost_price=24.0, sector="券商", buy_date="2026-06-10", last_high=25.0),
        Position(code="000001", name="平安银行", shares=5000, cost_price=13.0, sector="银行", buy_date="2026-02-20", last_high=13.8),
        Position(code="688981", name="中芯国际", shares=1000, cost_price=55.0, sector="半导体", buy_date="2026-06-15", last_high=58.0),
        Position(code="601899", name="紫金矿业", shares=4000, cost_price=18.0, sector="有色", buy_date="2026-04-01", last_high=19.0),
        Position(code="600696", name="ST某某", shares=1000, cost_price=8.0, sector="综合", buy_date="2026-03-10", last_high=9.0),
        Position(code="002415", name="海康威视", shares=1500, cost_price=35.0, sector="安防", buy_date="2026-05-05", last_high=36.5),
        Position(code="600900", name="长江电力", shares=2000, cost_price=22.0, sector="电力", buy_date="2026-01-15", last_high=23.0),
    ]
    account = Account(positions=positions, cash=50000.0, financing_balance=650000.0)
    save_account(account)
    return account


# ── 自选池（买点扫描候选）────────────────────────────────────────
def load_watchlist() -> list[dict]:
    if not WATCHLIST_PATH.exists():
        return []
    import json

    try:
        return json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_watchlist(items: list[dict]) -> None:
    import json

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WATCHLIST_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def add_to_watchlist(code: str, name: str = "") -> None:
    items = load_watchlist()
    if not any(i["code"] == code for i in items):
        items.append({"code": code, "name": name})
    save_watchlist(items)


def remove_from_watchlist(code: str) -> None:
    items = [i for i in load_watchlist() if i["code"] != code]
    save_watchlist(items)
