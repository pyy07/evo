from conftest import POSTCLOSE_NOW, WEEKEND_NOW


def _invoke(client, cap, payload=None):
    return client.post(f"/capabilities/{cap}/invoke", json={"input": payload or {}})


def test_list_and_invoke_list_capabilities(client):
    r = client.get("/capabilities")
    assert r.status_code == 200
    ids = {c["id"] for c in r.json()["capabilities"]}
    assert "list_capabilities" in ids
    assert "submit_order" in ids

    inv = client.post("/capabilities/list_capabilities/invoke", json={"input": {}})
    assert inv.status_code == 200
    assert any(c["id"] == "get_portfolio" for c in inv.json()["result"]["capabilities"])


def test_unknown_capability(client):
    r = client.post("/capabilities/does_not_exist/invoke", json={"input": {}})
    assert r.status_code == 404


def test_missing_required_input(client):
    r = client.post("/capabilities/get_etf_history/invoke", json={"input": {}})
    assert r.status_code == 400


def test_screen_market_and_stock_order(client):
    screen = _invoke(
        client,
        "screen_market",
        {"stock_type": "stock", "index_code": "000300", "top_n": 8},
    )
    assert screen.status_code == 200
    body = screen.json()["result"]
    assert body["stock_type"] == "stock"
    assert body["universe_size"] >= 8
    assert len(body["candidates"]) == 8
    pick = next(c["code"] for c in body["candidates"] if c["code"].startswith("6"))
    order = _invoke(
        client,
        "submit_order",
        {"symbol": pick, "side": "buy", "quantity": 100},
    )
    assert order.status_code == 200
    assert order.json()["result"]["status"] == "filled"
    blocked = _invoke(
        client,
        "submit_order",
        {"symbol": "000300", "side": "buy", "quantity": 100},
    )
    assert blocked.status_code == 400


def test_market_overview_survives_upstream_error(client, monkeypatch):
    from market_data.provider import MarketDataError, get_provider

    provider = get_provider()

    def boom(**_kwargs):
        raise MarketDataError("industry_comparison failed: Remote end closed connection without response")

    monkeypatch.setattr(provider, "get_market_overview", boom)
    monkeypatch.setattr(provider, "industry_comparison", boom)
    overview = _invoke(client, "get_market_overview", {"top_n": 3})
    assert overview.status_code == 200
    body = overview.json()["result"]
    assert body.get("degraded") is True
    rank = _invoke(client, "get_industry_ranking", {"board": "industry", "top_n": 3})
    assert rank.status_code == 200
    assert rank.json()["result"].get("degraded") is True


def test_market_overview_capability(client):
    overview = _invoke(client, "get_market_overview", {"top_n": 3})
    assert overview.status_code == 200
    body = overview.json()["result"]
    assert "industry" in body and body["industry"]["leaders"]
    assert "industry_fund_flow" in body
    assert "breadth_proxy" in body
    assert body["breadth"]["histogram"]
    assert body["index_valuation"]["pe"]
    val = _invoke(client, "get_index_valuation", {"code": "510300"})
    assert val.status_code == 200
    assert val.json()["result"]["index_code"] == "000300"
    breadth = _invoke(client, "get_market_breadth")
    assert breadth.status_code == 200
    assert breadth.json()["result"]["histogram"]
    hist = _invoke(client, "get_etf_history", {"code": "510300", "count": 12})
    assert hist.status_code == 200
    hbody = hist.json()["result"]
    assert hbody["latest"]["date"] == hbody["bars"][-1]["date"]
    assert hbody["count"] == len(hbody["bars"])
    rank = _invoke(client, "get_industry_ranking", {"board": "concept", "top_n": 3})
    assert rank.status_code == 200
    flow = _invoke(
        client,
        "get_board_fund_flow",
        {"board_type": "industry", "period": "today", "top_n": 3},
    )
    assert flow.status_code == 200


def test_news_capabilities(client):
    news = _invoke(
        client,
        "get_market_news",
        {"scope": "both", "codes": ["510300"], "limit": 8},
    )
    assert news.status_code == 200
    nbody = news.json()["result"]
    assert nbody["count"] >= 1
    assert nbody["items"][0]["summary"]
    anns = _invoke(client, "get_announcements", {"codes": ["510300"], "days": 30})
    assert anns.status_code == 200
    assert anns.json()["result"]["items"]
    missing = _invoke(client, "get_announcements", {})
    assert missing.status_code == 400
    macro = _invoke(
        client,
        "get_macro_digest",
        {"days_ahead": 5, "min_importance": 2, "include_cctv": True},
    )
    assert macro.status_code == 200
    mbody = macro.json()["result"]
    assert mbody["calendar"]
    assert mbody["cctv"]


def test_screen_etf_and_convertible_bond(client):
    etf = _invoke(client, "screen_market", {"stock_type": "etf", "top_n": 8})
    assert etf.status_code == 200
    etf_body = etf.json()["result"]
    assert etf_body["stock_type"] == "etf"
    assert any(c["code"].startswith(("51", "15", "58")) for c in etf_body["candidates"])
    etf_code = etf_body["candidates"][0]["code"]
    etf_order = _invoke(
        client, "submit_order", {"symbol": etf_code, "side": "buy", "quantity": 100}
    )
    assert etf_order.status_code == 200

    cb = _invoke(client, "screen_market", {"stock_type": "convertible_bond", "top_n": 5})
    assert cb.status_code == 200
    cb_body = cb.json()["result"]
    assert cb_body["stock_type"] == "convertible_bond"
    cb_code = cb_body["candidates"][0]["code"]
    assert cb_code.startswith(("11", "12"))
    cb_order = _invoke(
        client, "submit_order", {"symbol": cb_code, "side": "buy", "quantity": 10}
    )
    assert cb_order.status_code == 200


def test_market_snapshot_mock(client):
    r = client.post(
        "/capabilities/get_market_snapshot/invoke",
        json={"input": {"codes": ["510300"]}},
    )
    assert r.status_code == 200
    assert "510300" in r.json()["result"]["quotes"]


def test_decision_paper_trade_and_review_cr(client, monkeypatch):
    run = client.post(
        "/capabilities/start_agent_run/invoke",
        json={"input": {"trigger": "test"}},
    ).json()["result"]
    run_id = run["agent_run_id"]

    obs = client.post(
        "/capabilities/submit_observation/invoke",
        json={"input": {"content": "risk-off", "agent_run_id": run_id}},
    ).json()["result"]
    thesis = client.post(
        "/capabilities/submit_thesis/invoke",
        json={
            "input": {
                "content": "reduce beta via bond etf",
                "observation_id": obs["observation_id"],
                "agent_run_id": run_id,
            }
        },
    ).json()["result"]
    dec = client.post(
        "/capabilities/submit_decision/invoke",
        json={
            "input": {
                "summary": "buy 511010",
                "hypothesis": "rates supportive",
                "action_plan": "buy 1000",
                "observation_id": obs["observation_id"],
                "thesis_id": thesis["thesis_id"],
                "agent_run_id": run_id,
            }
        },
    ).json()["result"]

    order = client.post(
        "/capabilities/submit_order/invoke",
        json={
            "input": {
                "symbol": "511010",
                "side": "buy",
                "quantity": 1000,
                "decision_id": dec["decision_id"],
                "agent_run_id": run_id,
            }
        },
    )
    assert order.status_code == 200
    assert order.json()["result"]["status"] == "filled"

    port = client.post("/capabilities/get_portfolio/invoke", json={"input": {}})
    assert port.status_code == 200
    assert any(p["symbol"] == "511010" for p in port.json()["result"]["positions"])

    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: POSTCLOSE_NOW)
    review = client.post(
        "/capabilities/submit_review/invoke",
        json={
            "input": {
                "issue_type": "CapabilityGap",
                "content": "need relative strength tool",
                "decision_id": dec["decision_id"],
                "agent_run_id": run_id,
                "create_change_request": True,
                "change_request": {
                    "title": "Add RS analysis",
                    "problem": "missing RS",
                    "proposal": "add relative_strength capability",
                    "issue_type": "CapabilityGap",
                },
            }
        },
    )
    assert review.status_code == 200
    cr_id = review.json()["result"]["change_request_id"]
    assert cr_id

    headers = {"Authorization": "Bearer test-admin"}
    approve = client.post(
        f"/admin/change-requests/{cr_id}/approve",
        json={"notes": "ok"},
        headers=headers,
    )
    assert approve.status_code == 200
    assert approve.json()["status"] == "pending_dev"

    impl = client.post(
        f"/admin/change-requests/{cr_id}/implement",
        json={
            "capability_id": "relative_strength",
            "name": "Relative Strength",
            "description": "placeholder after human impl",
            "implementation": "noop",
        },
        headers=headers,
    )
    assert impl.status_code == 200
    assert impl.json()["status"] == "completed"

    verify = client.post(
        "/capabilities/verify_change_request/invoke",
        json={
            "input": {
                "change_request_id": cr_id,
                "passed": True,
                "evidence": "noop capability 可调用",
            }
        },
    )
    assert verify.status_code == 200
    assert verify.json()["result"]["status"] == "verified"

    caps = client.get("/capabilities").json()["capabilities"]
    assert any(c["id"] == "relative_strength" for c in caps)

    mem = client.get("/admin/memory", headers=headers)
    assert mem.status_code == 200
    assert mem.json()["investment_memory"]["decision_count"] >= 1


def test_order_blocked_when_market_closed(client, monkeypatch):
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: WEEKEND_NOW)
    r = _invoke(
        client,
        "submit_order",
        {"symbol": "510300", "side": "buy", "quantity": 100},
    )
    assert r.status_code == 400
    assert "MarketClosed" in r.json()["detail"]


def test_cr_blocked_during_intraday(client):
    r = _invoke(
        client,
        "create_change_request",
        {
            "title": "need tool",
            "problem": "gap",
            "issue_type": "CapabilityGap",
        },
    )
    assert r.status_code == 400
    assert "ReviewWindowClosed" in r.json()["detail"]


def test_settle_day_and_day_report(client, monkeypatch):
    _invoke(client, "submit_order", {"symbol": "510300", "side": "buy", "quantity": 100})
    blocked = _invoke(client, "settle_day")
    assert blocked.status_code == 400
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: POSTCLOSE_NOW)
    settled = _invoke(client, "settle_day")
    assert settled.status_code == 200
    body = settled.json()["result"]
    assert body["settled"] is True
    assert "day_pnl" in body
    assert "positions" in body
    assert body["snapshot_id"]
    assert body["settlement_id"]
    assert body["settlement"]["trade_date"]
    assert body["open_equity"] == body["settlement"]["open_equity"]
    assert body["close_equity"] == body["settlement"]["close_equity"]
    report = _invoke(client, "get_day_report")
    assert report.status_code == 200
    rbody = report.json()["result"]
    assert rbody["latest_snapshot_today"]["snapshot_id"] == body["snapshot_id"]
    assert rbody["settlement"]["id"] == body["settlement_id"]
    listed = _invoke(client, "list_settlements", {"limit": 10})
    assert listed.status_code == 200
    items = listed.json()["result"]["items"]
    assert items
    assert items[0]["id"] == body["settlement_id"]
    # 同日再清算应 upsert，仍只有一条
    again = _invoke(client, "settle_day")
    assert again.status_code == 200
    assert again.json()["result"]["settlement_id"] == body["settlement_id"]
    listed2 = _invoke(client, "list_settlements", {"limit": 10})
    assert listed2.json()["result"]["count"] == 1
    decisions = _invoke(client, "list_today_decisions")
    assert decisions.status_code == 200
    orders = _invoke(client, "list_today_orders")
    assert orders.status_code == 200
    assert orders.json()["result"]["order_count"] >= 1


def test_admin_desk_includes_settlements(client, monkeypatch):
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: POSTCLOSE_NOW)
    _invoke(client, "settle_day")
    desk = client.get("/admin/desk", headers={"Authorization": "Bearer test-admin"})
    assert desk.status_code == 200
    body = desk.json()
    assert body["settlements"]
    assert body["settlement_today"] is not None
    assert body["settlement_today"]["close_equity"] is not None
    assert "trades_history" in body


def test_empty_postclose_review_without_cr_or_lessons(client, monkeypatch):
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: POSTCLOSE_NOW)
    r = _invoke(
        client,
        "submit_review",
        {"issue_type": "NoIssue", "content": "今日无新增经验或变更请求"},
    )
    assert r.status_code == 200
    assert r.json()["result"]["change_request_id"] is None
    assert r.json()["result"]["experience_ids"] == []


def test_reject_allows_empty_reason_and_verify_failed_returns_pending(client, monkeypatch):
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: POSTCLOSE_NOW)
    created = _invoke(
        client,
        "create_change_request",
        {
            "title": "need rs",
            "problem": "missing",
            "issue_type": "DataGap",
        },
    )
    assert created.status_code == 200
    cr_id = created.json()["result"]["change_request_id"]
    headers = {"Authorization": "Bearer test-admin"}
    ok = client.post(
        f"/admin/change-requests/{cr_id}/reject",
        json={"notes": ""},
        headers=headers,
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "rejected"

    cr2 = _invoke(
        client,
        "create_change_request",
        {"title": "cap", "problem": "x", "issue_type": "CapabilityGap"},
    ).json()["result"]["change_request_id"]
    client.post(f"/admin/change-requests/{cr2}/approve", json={"notes": "ok"}, headers=headers)
    client.post(
        f"/admin/change-requests/{cr2}/implement",
        json={
            "capability_id": "cap_verify_fail",
            "name": "Temp",
            "description": "x",
            "implementation": "noop",
        },
        headers=headers,
    )
    failed = _invoke(
        client,
        "verify_change_request",
        {"change_request_id": cr2, "passed": False, "evidence": "调用结果不符合预期"},
    )
    assert failed.status_code == 200
    assert failed.json()["result"]["status"] == "pending_dev"


def test_submit_decision_persists_usage_notes(client):
    run = _invoke(client, "start_agent_run", {"trigger": "intraday"}).json()["result"]
    dec = _invoke(
        client,
        "submit_decision",
        {
            "summary": "观望",
            "agent_run_id": run["agent_run_id"],
            "usage_notes": [
                {
                    "kind": "missing_tool",
                    "content": "缺少行业板块涨跌排名",
                    "capability_id": "industry_comparison",
                },
                {"kind": "tool_error", "capability_id": "list_universe", "content": "偶发超时"},
            ],
        },
    )
    assert dec.status_code == 200
    assert len(dec.json()["result"]["usage_notes"]) == 2
    listed = _invoke(client, "list_today_decisions").json()["result"]
    hit = next(d for d in listed["items"] if d["id"] == dec.json()["result"]["decision_id"])
    assert any(n["kind"] == "missing_tool" for n in hit["usage_notes"])


def test_seed_experiences_loaded(client):
    listed = _invoke(client, "list_experiences", {"limit": 20})
    assert listed.status_code == 200
    texts = " ".join(i["content"] for i in listed.json()["result"]["items"])
    assert "大盘→行业→ETF" in texts
    assert "中长期" in texts
    assert "T+1" in texts
    assert "a-stock-data" in texts


def test_t_plus_one_blocks_same_day_sell(client, monkeypatch):
    buy = _invoke(
        client,
        "submit_order",
        {"symbol": "510300", "side": "buy", "quantity": 100},
    )
    assert buy.status_code == 200
    assert buy.json()["result"]["status"] == "filled"
    port = _invoke(client, "get_portfolio").json()["result"]
    pos = next(p for p in port["positions"] if p["symbol"] == "510300")
    assert pos["quantity"] == 100
    assert pos["sellable_quantity"] == 0
    assert pos["locked_quantity"] == 100
    blocked = _invoke(
        client,
        "submit_order",
        {"symbol": "510300", "side": "sell", "quantity": 100},
    )
    assert blocked.status_code == 400
    assert "T+1" in blocked.json()["detail"]

    from datetime import datetime

    from evo_api.services.market_session import CN_TZ

    # 下一交易日：解锁 T+1 可卖
    next_open = datetime(2026, 10, 13, 10, 0, tzinfo=CN_TZ)
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: next_open)
    port2 = _invoke(client, "get_portfolio").json()["result"]
    pos2 = next(p for p in port2["positions"] if p["symbol"] == "510300")
    assert pos2["sellable_quantity"] == 100
    sold = _invoke(
        client,
        "submit_order",
        {"symbol": "510300", "side": "sell", "quantity": 100},
    )
    assert sold.status_code == 200
    assert sold.json()["result"]["status"] == "filled"


def test_desk_and_portfolio_pnl(client):
    headers = {"Authorization": "Bearer test-admin"}
    # seed one filled order via open-session default fixture
    order = _invoke(
        client,
        "submit_order",
        {"symbol": "510300", "side": "buy", "quantity": 100},
    )
    assert order.status_code == 200
    desk = client.get("/admin/desk", headers=headers)
    assert desk.status_code == 200
    body = desk.json()
    assert "portfolio" in body and "today" in body
    port = body["portfolio"]
    assert "total_pnl" in port and "day_pnl" in port and "unrealized_pnl" in port
    assert any(p["symbol"] == "510300" for p in port["positions"])
    assert "unrealized_pnl" in port["positions"][0]
    assert isinstance(body["today"]["orders"], list)
    assert isinstance(body["today"]["trades"], list)
    assert isinstance(body["trades_history"], list)
    assert len(body["today"]["trades"]) >= 1
    # 当日成交不应出现在历史成交里
    today_ids = {t["id"] for t in body["today"]["trades"]}
    assert today_ids.isdisjoint({t["id"] for t in body["trades_history"]})
    trade = body["today"]["trades"][0]
    assert trade["symbol"] == "510300"
    assert trade["quantity"] == 100
    assert trade["amount"] == round(trade["price"] * trade["quantity"], 2)


def test_desk_decision_includes_capability_invocations(client):
    headers = {"Authorization": "Bearer test-admin"}
    run = _invoke(client, "start_agent_run", {"trigger": "intraday"}).json()["result"]
    run_id = run["agent_run_id"]
    _invoke(
        client,
        "screen_market",
        {"stock_type": "etf", "top_n": 8, "agent_run_id": run_id},
    )
    _invoke(client, "get_market_snapshot", {"codes": ["510300"]})
    _invoke(
        client,
        "submit_observation",
        {"content": "观察", "agent_run_id": run_id},
    )
    dec = _invoke(
        client,
        "submit_decision",
        {"summary": "观望", "agent_run_id": run_id},
    ).json()["result"]
    _invoke(client, "finish_agent_run", {"agent_run_id": run_id, "status": "completed"})

    desk = client.get("/admin/desk", headers=headers).json()
    decision = next(d for d in desk["today"]["decisions"] if d["id"] == dec["decision_id"])
    caps = {i["capability_id"] for i in decision["invocations"]}
    assert "start_agent_run" in caps
    assert "screen_market" in caps
    assert "submit_decision" in caps
    assert "finish_agent_run" in caps
    tagged = [i for i in decision["invocations"] if i["capability_id"] == "submit_decision"][0]
    assert tagged["input"]["agent_run_id"] == run_id
    assert "decision_id" in tagged["output"]


def test_implement_without_registering_capability(client, monkeypatch):
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: POSTCLOSE_NOW)
    cr_id = _invoke(
        client,
        "create_change_request",
        {"title": "fix fill", "problem": "x", "issue_type": "DataGap"},
    ).json()["result"]["change_request_id"]
    headers = {"Authorization": "Bearer test-admin"}
    client.post(f"/admin/change-requests/{cr_id}/approve", json={"notes": "ok"}, headers=headers)
    done = client.post(
        f"/admin/change-requests/{cr_id}/implement",
        json={"notes": "代码已合并"},
        headers=headers,
    )
    assert done.status_code == 200
    assert done.json()["status"] == "completed"
    assert done.json()["capability_id"] is None


def test_market_session_and_snapshot(client, monkeypatch):
    open_s = _invoke(client, "get_market_session")
    assert open_s.status_code == 200
    assert open_s.json()["result"]["session"] == "open"
    assert open_s.json()["result"]["can_trade"] is True
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: POSTCLOSE_NOW)
    post = _invoke(client, "get_market_session")
    assert post.json()["result"]["session"] == "postclose"
    assert post.json()["result"]["can_review_evolve"] is True
    snap = _invoke(client, "take_portfolio_snapshot")
    assert snap.status_code == 200
    assert "snapshot_id" in snap.json()["result"]


def test_lessons_persisted_postclose(client, monkeypatch):
    monkeypatch.setattr("evo_api.services.market_session.now_cn", lambda: POSTCLOSE_NOW)
    r = _invoke(
        client,
        "submit_review",
        {
            "issue_type": "NoIssue",
            "content": "复盘",
            "lessons": [{"kind": "investment", "content": "午后缩量不宜追涨"}],
        },
    )
    assert r.status_code == 200
    assert r.json()["result"]["experience_ids"]
    listed = _invoke(client, "list_experiences", {"limit": 5})
    assert any("午后缩量" in i["content"] for i in listed.json()["result"]["items"])