from datetime import date, timedelta

from evo_api.services.trade_verify import (
    add_trading_days,
    parse_order_evidence,
    parse_verify_spec,
)


def test_parse_verify_spec_ok():
    spec = parse_verify_spec(
        {
            "horizon_days": 5,
            "metric": "return_vs_benchmark",
            "op": "gte",
            "target": 0.01,
            "benchmark": "000300",
            "summary": "5日相对沪深300超额≥1%",
        }
    )
    assert spec["benchmark"] == "000300"
    assert spec["horizon_days"] == 5


def test_parse_order_evidence_rejects_weak_rationale():
    try:
        parse_order_evidence(rationale="短", confidence=50, verify={
            "horizon_days": 3,
            "metric": "still_held",
            "op": "gte",
            "target": 1,
            "summary": "仍持有",
        })
        assert False, "should raise"
    except ValueError as exc:
        assert "rationale" in str(exc)


def test_add_trading_days_skips_weekend(monkeypatch):
    # 周一 + 1 个交易日 → 周二（不依赖真实日历时用 weekday 回退）
    monkeypatch.setattr(
        "evo_api.services.market_session.is_trading_day",
        lambda d, calendar_days=None: d.weekday() < 5,
    )
    start = date(2026, 10, 19)  # Monday
    assert add_trading_days(start, 1) == date(2026, 10, 20)
    assert add_trading_days(start, 5) == date(2026, 10, 26)  # skip weekend


def test_trade_verify_roundtrip(client, monkeypatch):
    from datetime import datetime

    from conftest import CN_TZ

    def _invoke(cap, payload=None):
        return client.post(f"/capabilities/{cap}/invoke", json={"input": payload or {}})

    order = _invoke(
        "submit_order",
        {
            "symbol": "510300",
            "side": "buy",
            "quantity": 100,
            "rationale": "估值锚与风格配置：测试验证链路",
            "confidence": 66,
            "verify": {
                "horizon_days": 1,
                "metric": "return_vs_cost",
                "op": "gte",
                "target": -1.0,
                "summary": "1个交易日内相对成本跌幅不超过100%（必过）",
            },
        },
    )
    assert order.status_code == 200, order.text
    body = order.json()["result"]
    assert body["verify_status"] == "pending"
    assert body["confidence"] == 66
    trade_id = body["trade_id"]
    assert body["verify_due_date"]

    # 拨到到期日之后的盘后窗口
    later = datetime(2026, 10, 20, 15, 30, tzinfo=CN_TZ)
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: later)

    due = _invoke("list_trade_verifications", {"status": "due"})
    assert due.status_code == 200, due.text
    items = due.json()["result"]["items"]
    assert any(i["id"] == trade_id for i in items)
    hit = next(i for i in items if i["id"] == trade_id)
    assert hit.get("live_eval") is not None

    done = _invoke(
        "submit_trade_verification",
        {
            "trade_id": trade_id,
            "outcome": "条件达成：相对成本未大幅回撤，验证通过。",
            "lesson": "[交易验证·510300] 有明确验证条件的成交便于复盘。",
        },
    )
    assert done.status_code == 200, done.text
    assert done.json()["result"]["verify_status"] == "verified"
    assert done.json()["result"]["verify_eval"] is not None
