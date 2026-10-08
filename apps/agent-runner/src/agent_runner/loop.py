from __future__ import annotations

import json
import re
from typing import Any

from openai import OpenAI

from agent_runner.client import SystemClient
from agent_runner.settings import RunnerSettings

SYSTEM_PROMPT = """你是「自演进投资系统」中的投资 Agent，只做 A 股 ETF 纸面（模拟）交易。

硬性规则：
1. 只能使用系统已提供的 Capability 结果，禁止编造接口或进行实盘交易。
2. 所有面向人类阅读的字段必须使用简体中文撰写（观察、论点、决策摘要、假设、行动计划、复盘内容等）。
3. 投资范围：A 股 ETF；不确定时优先使用配置中的默认 ETF。
4. 每轮流程：查看能力 → 拉取行情/历史 → 提交观察/论点/决策 → 可选下单（数量须为 100 的整数倍）→ 复盘。
5. 复盘 issue_type 只能是：DecisionError | DataGap | CapabilityGap | ToolUsageError。
6. 若缺少所需能力，应提出 CapabilityGap / Change Request，而不是臆造工具。

输出要求：
- 只返回一个 JSON 对象（可放在思考过程之后），不要用 Markdown 代码块包裹。
- JSON 字段值（字符串）一律使用简体中文。
- issue_type 与 place_order 保持英文枚举 / 布尔值。
"""


def _extract_json_object(text: str) -> dict[str, Any]:
    """Parse JSON from model output; tolerate MiniMax <think> wrappers."""
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.I).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(cleaned[start : end + 1])
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def run_once(settings: RunnerSettings | None = None) -> dict[str, Any]:
    settings = settings or RunnerSettings()
    client = SystemClient(settings.api_base, actor=settings.actor)
    caps = client.list_capabilities()
    cap_ids = [c["id"] for c in caps]

    run = client.invoke(
        "start_agent_run",
        {"trigger": "schedule", "notes": "定时/手动触发的一轮 Agent 运行"},
    )
    run_id = run["agent_run_id"]

    snapshot = client.invoke("get_market_snapshot", {"codes": [settings.default_etf]})
    history = client.invoke("get_etf_history", {"code": settings.default_etf, "count": 20})
    portfolio = client.invoke("get_portfolio", {})

    decision_payload: dict[str, Any]

    if settings.openai_api_key:
        llm = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        user = {
            "可用能力": cap_ids,
            "行情快照": snapshot,
            "近期K线": history.get("bars", [])[-5:],
            "当前组合": portfolio,
            "默认ETF": settings.default_etf,
            "agent_run_id": run_id,
        }
        completion = llm.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "根据以下上下文，用简体中文给出本轮投资判断，并决定是否买入默认 ETF 100 份（1 手）。\n"
                        "请只返回 JSON，字段如下：\n"
                        "- observation：市场观察（中文）\n"
                        "- thesis：投资论点（中文）\n"
                        "- decision_summary：决策摘要（中文）\n"
                        "- hypothesis：假设（中文）\n"
                        "- action_plan：行动计划（中文）\n"
                        "- place_order：是否下单（布尔）\n"
                        "- review_content：复盘说明（中文）\n"
                        "- issue_type：DecisionError|DataGap|CapabilityGap|ToolUsageError\n\n"
                        + json.dumps(user, ensure_ascii=False, default=str)[:12000]
                    ),
                },
            ],
            temperature=0.2,
        )
        text = completion.choices[0].message.content or "{}"
        decision_payload = _extract_json_object(text)
    else:
        decision_payload = {}

    quote = snapshot.get("quotes", {}).get(settings.default_etf, {})
    observation = decision_payload.get("observation") or (
        f"【模拟行情】对 {settings.default_etf} 的观察：最新报价 {quote}"
    )
    thesis = decision_payload.get("thesis") or (
        "一期以小仓位验证纸面交易闭环，保持有限 ETF 敞口。"
    )
    summary = decision_payload.get("decision_summary") or (
        f"小仓位买入 {settings.default_etf}"
    )
    hypothesis = decision_payload.get("hypothesis") or "纸面成交可验证下单与持仓更新链路"
    action_plan = decision_payload.get("action_plan") or "买入 100 份（1 手）"
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
        "本轮自动化运行完成，未发现需要升级系统能力的明显缺口。"
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
        {"agent_run_id": run_id, "status": "completed", "notes": "本轮 Agent 运行已完成"},
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
