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


def test_market_snapshot_mock(client):
    r = client.post(
        "/capabilities/get_market_snapshot/invoke",
        json={"input": {"codes": ["510300"]}},
    )
    assert r.status_code == 200
    assert "510300" in r.json()["result"]["quotes"]


def test_decision_paper_trade_and_review_cr(client):
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

    caps = client.get("/capabilities").json()["capabilities"]
    assert any(c["id"] == "relative_strength" for c in caps)

    mem = client.get("/admin/memory", headers=headers)
    assert mem.status_code == 200
    assert mem.json()["investment_memory"]["decision_count"] >= 1