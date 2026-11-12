"""CR#8：收盘价估值 + 缺日清算补齐 + 日初基准诚实标注；卖出日归因不重复计入。"""

from datetime import date, datetime

from conftest import CN_TZ, OPEN_NOW, POSTCLOSE_NOW
from evo_api.services.portfolio import _build_attribution


def _invoke(client, cap, payload=None):
    return client.post(f"/capabilities/{cap}/invoke", json={"input": payload or {}})


def _order(symbol="510300", **extra):
    body = {
        "symbol": symbol,
        "side": "buy",
        "quantity": 100,
        "rationale": "清算口径测试下单：验证收盘价与缺日补齐",
        "confidence": 60,
        "verify": {
            "horizon_days": 3,
            "metric": "return_vs_cost",
            "op": "gte",
            "target": -1.0,
            "summary": "测试验证条件",
        },
    }
    body.update(extra)
    return body


def test_settle_uses_close_mark_and_basis_fields(client, monkeypatch):
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: OPEN_NOW)
    assert _invoke(client, "submit_order", _order()).status_code == 200

    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: POSTCLOSE_NOW)
    settled = _invoke(client, "settle_day")
    assert settled.status_code == 200, settled.text
    body = settled.json()["result"]
    assert body["settled"] is True
    assert "open_basis_date" in body
    assert "open_basis_stale" in body
    assert body.get("attribution") is not None
    assert body["attribution"].get("mark_mode") == "close"
    assert "attribution_window" in body["attribution"]
    assert "fees" in body["attribution"]
    pos = body["positions"][0]
    assert pos.get("mark_price_source") in ("close", "last_fallback", "avg_cost_fallback")
    settle = body["settlement"]
    assert settle.get("open_basis_date")
    assert "attribution" in settle


def test_backfill_gap_settlement(client, monkeypatch):
    # Day1 open: buy + settle
    day1_open = datetime(2026, 10, 13, 10, 0, tzinfo=CN_TZ)  # Tue
    day1_close = datetime(2026, 10, 13, 15, 30, tzinfo=CN_TZ)
    day3_open = datetime(2026, 10, 15, 10, 0, tzinfo=CN_TZ)  # Thu（跳过 14 日）
    day3_close = datetime(2026, 10, 15, 15, 30, tzinfo=CN_TZ)

    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: day1_open)
    monkeypatch.setattr(
        "evo_api.services.market_session.is_trading_day",
        lambda d, calendar_days=None: d.weekday() < 5,
    )
    assert _invoke(client, "submit_order", _order()).status_code == 200
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: day1_close)
    assert _invoke(client, "settle_day").status_code == 200

    # Day3: another buy + settle → should backfill Wed 10/14
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: day3_open)
    assert _invoke(client, "submit_order", _order(quantity=200)).status_code == 200
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: day3_close)
    settled = _invoke(client, "settle_day")
    assert settled.status_code == 200, settled.text
    body = settled.json()["result"]
    assert "2026-10-14" in (body.get("backfilled_dates") or [])

    listed = _invoke(client, "list_settlements", {"limit": 10})
    assert listed.status_code == 200, listed.text
    dates = [i["trade_date"] for i in listed.json()["result"]["items"]]
    assert "2026-10-14" in dates
    assert "2026-10-15" in dates
    assert "2026-10-13" in dates


def test_attribution_sell_day_no_double_count():
    """卖出日：留存仓计价 + 卖出腿，不得用昨仓全量再叠一层 trade_mark。"""
    prev_close, close, sell_px = 4.0, 4.2, 4.1
    prev_qty, sell_qty = 1000.0, 300.0
    retained = prev_qty - sell_qty
    # 构造与公式一致的 day_pnl，便于对账
    # start MV = 1000*4.0；end MV = 700*4.2；现金 +300*4.1；fee=1
    day_pnl = retained * (close - prev_close) + sell_qty * (sell_px - prev_close) - 1.0
    view = {
        "day_pnl": day_pnl,
        "cash": 100_000.0 + sell_qty * sell_px - 1.0,
        "open_cash": 100_000.0,
        "open_basis_date": "2026-10-20",
        "open_basis_stale": False,
        "prev_close_source": "settlement",
        "mark_mode": "close",
        "_prev_positions": {
            "510300": {"quantity": prev_qty, "mark_price": prev_close, "avg_cost": 3.9}
        },
        "positions": [
            {
                "symbol": "510300",
                "quantity": retained,
                "mark_price": close,
                "avg_cost": 3.9,
            }
        ],
    }
    trades = [
        {
            "symbol": "510300",
            "side": "sell",
            "quantity": sell_qty,
            "price": sell_px,
            "amount": sell_qty * sell_px,
            "commission": 1.0,
        }
    ]
    attr = _build_attribution(
        trade_date=date(2026, 10, 21),
        view=view,
        trades=trades,
        buy_amount=0.0,
        sell_amount=sell_qty * sell_px,
    )
    assert attr["holding_price_pnl"] == round(retained * (close - prev_close), 2)
    assert attr["trade_mark_pnl"] == round(sell_qty * (sell_px - prev_close), 2)
    assert attr["fees"] == 1.0
    assert abs(attr["reconcile_gap"]) < 1.0
    assert not any("归因不对账" in w for w in attr["warnings"])
    leg = attr["positions"][0]
    assert leg["retained_qty"] == retained
    assert leg["sell_qty"] == sell_qty
    assert leg["prev_qty"] == prev_qty
    assert leg["curr_qty"] == retained


def test_attribution_buy_and_sell_same_day():
    prev_close, close = 10.0, 11.0
    buy_px, sell_px = 10.5, 10.8
    prev_qty, buy_qty, sell_qty = 100.0, 50.0, 30.0
    retained = prev_qty - sell_qty  # 70
    curr_qty = prev_qty + buy_qty - sell_qty  # 120
    day_pnl = (
        retained * (close - prev_close)
        + sell_qty * (sell_px - prev_close)
        + buy_qty * (close - buy_px)
        - 2.0
    )
    view = {
        "day_pnl": day_pnl,
        "cash": 50_000.0 - buy_qty * buy_px + sell_qty * sell_px - 2.0,
        "open_cash": 50_000.0,
        "open_basis_date": "2026-10-20",
        "open_basis_stale": False,
        "prev_close_source": "settlement",
        "mark_mode": "close",
        "_prev_positions": {
            "510300": {"quantity": prev_qty, "mark_price": prev_close, "avg_cost": 9.0}
        },
        "positions": [
            {
                "symbol": "510300",
                "quantity": curr_qty,
                "mark_price": close,
                "avg_cost": 9.5,
            }
        ],
    }
    trades = [
        {
            "symbol": "510300",
            "side": "buy",
            "quantity": buy_qty,
            "price": buy_px,
            "amount": buy_qty * buy_px,
            "commission": 1.0,
        },
        {
            "symbol": "510300",
            "side": "sell",
            "quantity": sell_qty,
            "price": sell_px,
            "amount": sell_qty * sell_px,
            "commission": 1.0,
        },
    ]
    attr = _build_attribution(
        trade_date=date(2026, 10, 21),
        view=view,
        trades=trades,
        buy_amount=buy_qty * buy_px,
        sell_amount=sell_qty * sell_px,
    )
    assert attr["holding_price_pnl"] == round(retained * (close - prev_close), 2)
    assert abs(attr["reconcile_gap"]) < 1.0
    assert not any("归因不对账" in w for w in attr["warnings"])


def test_repair_fills_historical_gap_not_just_latest(client, monkeypatch):
    """ensure_prior 须补齐历史空洞（10-19→10-21 缺 10-20），不能只看最近一日。"""
    day1_open = datetime(2026, 10, 19, 10, 0, tzinfo=CN_TZ)  # Mon
    day1_close = datetime(2026, 10, 19, 15, 30, tzinfo=CN_TZ)
    day3_open = datetime(2026, 10, 21, 10, 0, tzinfo=CN_TZ)  # Wed
    day3_close = datetime(2026, 10, 21, 15, 30, tzinfo=CN_TZ)
    day4_close = datetime(2026, 10, 22, 15, 30, tzinfo=CN_TZ)

    monkeypatch.setattr(
        "evo_api.services.market_session.is_trading_day",
        lambda d, calendar_days=None: d.weekday() < 5,
    )
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: day1_open)
    assert _invoke(client, "submit_order", _order(quantity=100)).status_code == 200
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: day1_close)
    assert _invoke(client, "settle_day").status_code == 200

    # 故意跳过 10-20：直接在 10-21 下单并清算
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: day3_open)
    assert _invoke(client, "submit_order", _order(quantity=100)).status_code == 200
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: day3_close)
    assert _invoke(client, "settle_day").status_code == 200

    # 10-21 清算时就应回填 10-20 空洞；再清算 10-22 刷新 open_basis
    listed_mid = _invoke(client, "list_settlements", {"limit": 10})
    mid_dates = [i["trade_date"] for i in listed_mid.json()["result"]["items"]]
    assert "2026-10-20" in mid_dates, mid_dates

    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: day4_close)
    settled = _invoke(client, "settle_day")
    assert settled.status_code == 200, settled.text

    listed = _invoke(client, "list_settlements", {"limit": 10})
    items = listed.json()["result"]["items"]
    by_date = {i["trade_date"]: i for i in items}
    assert "2026-10-20" in by_date
    row21 = by_date["2026-10-21"]
    assert row21.get("open_basis_date") == "2026-10-20"
    assert row21.get("attribution") is not None


def test_portfolio_positions_include_day_pnl(client, monkeypatch):
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: OPEN_NOW)
    assert _invoke(client, "submit_order", _order(quantity=100)).status_code == 200
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: POSTCLOSE_NOW)
    assert _invoke(client, "settle_day").status_code == 200

    day2 = datetime(2026, 10, 12, 10, 0, tzinfo=CN_TZ)
    monkeypatch.setattr(
        "evo_api.services.market_session.is_trading_day",
        lambda d, calendar_days=None: d.weekday() < 5,
    )
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: day2)
    port = _invoke(client, "get_portfolio")
    assert port.status_code == 200, port.text
    pos = port.json()["result"]["positions"][0]
    assert "day_pnl" in pos
    assert "prev_close" in pos or pos.get("day_pnl") is not None


def test_settle_after_sell_attribution_reconciles(client, monkeypatch):
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: OPEN_NOW)
    assert _invoke(client, "submit_order", _order(quantity=500)).status_code == 200
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: POSTCLOSE_NOW)
    assert _invoke(client, "settle_day").status_code == 200

    day2_open = datetime(2026, 10, 10, 10, 0, tzinfo=CN_TZ)  # 周末会被拦截；改用周一
    day2_open = datetime(2026, 10, 12, 10, 0, tzinfo=CN_TZ)
    day2_close = datetime(2026, 10, 12, 15, 30, tzinfo=CN_TZ)
    monkeypatch.setattr(
        "evo_api.services.market_session.is_trading_day",
        lambda d, calendar_days=None: d.weekday() < 5,
    )
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: day2_open)
    sold = _invoke(client, "submit_order", _order(side="sell", quantity=200))
    assert sold.status_code == 200, sold.text
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: day2_close)
    settled = _invoke(client, "settle_day")
    assert settled.status_code == 200, settled.text
    attr = settled.json()["result"]["attribution"]
    assert attr is not None
    assert "reconcile_gap" in attr
    assert abs(float(attr["reconcile_gap"])) < 1.0 or attr.get("open_basis_stale")
    leg = next(p for p in attr["positions"] if p["symbol"] == "510300")
    assert leg["sell_qty"] == 200
    assert leg["retained_qty"] == 300
