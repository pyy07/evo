from agent_runner.loop import compact_candidates, _orders_from_payload, next_action


def test_orders_from_payload_accepts_list_and_legacy_fields():
    assert _orders_from_payload({}) == []
    orders = _orders_from_payload(
        {
            "place_order": True,
            "symbol": "600519",
            "side": "buy",
            "quantity": 100,
            "orders": [{"symbol": "000858", "side": "sell", "quantity": 200}],
        }
    )
    assert {o["symbol"] for o in orders} == {"600519", "000858"}


def test_next_action_matrix():
    assert next_action({"session": "open"}) == "intraday"
    assert (
        next_action({"session": "postclose", "postclose_due": True, "postclose_completed_today": False})
        == "postclose"
    )
    assert (
        next_action({"session": "postclose", "postclose_due": True, "postclose_completed_today": True})
        == "idle"
    )
    assert next_action({"session": "lunch", "postclose_due": False}) == "idle"
    assert next_action({"session": "closed"}) == "idle"


def test_compact_candidates_marks_holdings():
    rows = compact_candidates(
        [
            {"code": "510300", "name": "沪深300ETF", "price": 4.3, "change_pct": -1.0},
            {"code": "512480", "name": "半导体ETF", "price": 1.2, "change_pct": 2.1},
            {"code": "159915", "name": "创业板ETF", "price": 2.0, "change_pct": 0.5},
        ],
        held=["510300"],
    )
    by_code = {r["code"]: r for r in rows}
    assert by_code["510300"]["held"] is True
    assert by_code["512480"]["held"] is False
    assert [r["code"] for r in rows if not r["held"]] == ["512480", "159915"]
