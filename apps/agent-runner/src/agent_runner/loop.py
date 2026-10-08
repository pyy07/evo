from __future__ import annotations

import json
from typing import Any

from openai import OpenAI

from agent_runner.client import SystemClient
from agent_runner.settings import RunnerSettings

SYSTEM_PROMPT = """You are an investment agent for a self-evolving paper-trading system.
You MUST only use system capabilities via the provided tool results — never invent APIs or trade live.
Universe: A-share ETFs. Prefer the configured default ETF if unsure.
Workflow each run:
1) Inspect capabilities
2) Fetch market snapshot / history
3) Submit observation, thesis, decision
4) Optionally submit_order (lot multiples of 100)
5) Submit review with issue_type in DecisionError|DataGap|CapabilityGap|ToolUsageError
If a needed capability is missing, create_change_request / submit_review with CapabilityGap.
Return a final JSON object summarizing ids created.
"""


def run_once(settings: RunnerSettings | None = None) -> dict[str, Any]:
    settings = settings or RunnerSettings()
    client = SystemClient(settings.api_base, actor=settings.actor)
    caps = client.list_capabilities()
    cap_ids = [c["id"] for c in caps]

    run = client.invoke("start_agent_run", {"trigger": "schedule", "notes": "agent-runner once"})
    run_id = run["agent_run_id"]

    snapshot = client.invoke("get_market_snapshot", {"codes": [settings.default_etf]})
    history = client.invoke("get_etf_history", {"code": settings.default_etf, "count": 20})
    portfolio = client.invoke("get_portfolio", {})

    decision_payload: dict[str, Any]

    if settings.openai_api_key:
        llm = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        user = {
            "capabilities": cap_ids,
            "snapshot": snapshot,
            "history_tail": history.get("bars", [])[-5:],
            "portfolio": portfolio,
            "default_etf": settings.default_etf,
            "agent_run_id": run_id,
        }
        completion = llm.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "Given this context, propose observation/thesis/decision text and whether to buy "
                        "100*lot of default ETF. Reply JSON keys: "
                        "observation, thesis, decision_summary, hypothesis, action_plan, "
                        "place_order (bool), review_content, issue_type.\n"
                        + json.dumps(user, ensure_ascii=False, default=str)[:12000]
                    ),
                },
            ],
            temperature=0.2,
        )
        text = completion.choices[0].message.content or "{}"
        try:
            start = text.find("{")
            end = text.rfind("}")
            decision_payload = json.loads(text[start : end + 1])
        except Exception:  # noqa: BLE001
            decision_payload = {}
    else:
        decision_payload = {}

    observation = decision_payload.get("observation") or (
        f"Mock mode observation on {settings.default_etf}: "
        f"price={snapshot.get('quotes', {}).get(settings.default_etf, {})}"
    )
    thesis = decision_payload.get("thesis") or (
        "Maintain modest ETF exposure for phase-1 paper loop validation."
    )
    summary = decision_payload.get("decision_summary") or f"Buy small lot of {settings.default_etf}"
    hypothesis = decision_payload.get("hypothesis") or "Paper trade validates execution path"
    action_plan = decision_payload.get("action_plan") or "buy 100 shares/units"
    place_order = bool(decision_payload.get("place_order", True))
    issue_type = decision_payload.get("issue_type") or "ToolUsageError"
    if issue_type not in {
        "DecisionError",
        "DataGap",
        "CapabilityGap",
        "ToolUsageError",
    }:
        issue_type = "ToolUsageError"
    review_content = decision_payload.get("review_content") or (
        "Phase-1 automated run completed; no material gap detected."
    )

    obs = client.invoke(
        "submit_observation",
        {"content": observation, "data": {"snapshot": snapshot}, "agent_run_id": run_id},
    )
    th = client.invoke(
        "submit_thesis",
        {
            "content": thesis,
            "observation_id": obs["observation_id"],
            "agent_run_id": run_id,
        },
    )
    dec = client.invoke(
        "submit_decision",
        {
            "summary": summary,
            "hypothesis": hypothesis,
            "action_plan": action_plan,
            "observation_id": obs["observation_id"],
            "thesis_id": th["thesis_id"],
            "agent_run_id": run_id,
        },
    )

    order_result = None
    if place_order:
        order_result = client.invoke(
            "submit_order",
            {
                "symbol": settings.default_etf,
                "side": "buy",
                "quantity": 100,
                "decision_id": dec["decision_id"],
                "agent_run_id": run_id,
            },
        )

    review = client.invoke(
        "submit_review",
        {
            "issue_type": issue_type,
            "content": review_content,
            "decision_id": dec["decision_id"],
            "agent_run_id": run_id,
            "create_change_request": issue_type in ("CapabilityGap", "DataGap"),
        },
    )
    client.invoke(
        "finish_agent_run",
        {"agent_run_id": run_id, "status": "completed", "notes": "runner finished"},
    )

    return {
        "agent_run_id": run_id,
        "observation_id": obs["observation_id"],
        "thesis_id": th["thesis_id"],
        "decision_id": dec["decision_id"],
        "order": order_result,
        "review": review,
        "llm_used": bool(settings.openai_api_key),
    }