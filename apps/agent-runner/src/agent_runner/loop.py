from __future__ import annotations

import json
import re
import time
from typing import Any, Literal

from openai import OpenAI

from agent_runner.client import SystemClient
from agent_runner.intraday_agent import run_intraday_agent_loop
from agent_runner.postclose_agent import run_postclose_agent_loop
from agent_runner.settings import RunnerSettings

Mode = Literal["intraday", "postclose"]

INTRADAY_PROMPT = """你是「自演进投资系统」中的投资 Agent：中长期价值导向，不是短线/日内交易者。

当前是【盘中】Agent Loop：
1. 通过工具调用系统 Capability 获取数据；禁止编造未出现的代码或实盘交易。
2. 面向人类的字段必须用简体中文。
3. 风格：侧重中长期配置与价值逻辑；避免追分时涨跌、频繁调仓。
4. 选股代码来源（按优先级）：
   - list_experiences 中的经验流程（如大盘→行业→ETF）；
   - 看大盘优先 get_market_overview，细分再用 get_industry_ranking / get_board_fund_flow / get_index_valuation / get_market_breadth；
   - 资讯：get_market_news（快讯）、get_announcements（公告摘要）、get_macro_digest（宏观日历/联播）；
   - get_portfolio 持仓与可卖数量；
   - screen_market / list_universe 返回的候选。
5. get_market_snapshot / get_etf_history 的 codes 必须来自上面工具结果或持仓；
   禁止每轮机械复用固定主题 ETF 列表。
6. 多数轮观察即可；仅在中长期逻辑或风险显著变化时交易。卖出不超过 sellable_quantity（T+1）。
7. 缺行业排名等能力时，在 observation 记缺口；盘中不要提 CR / 不要调用 submit_*。
8. 下单数量须符合手数；不可交易指数本身。
9. finalize 时除观察/决策/订单外，必须输出 usage_notes（工具使用心得：
   missing_tool / tool_error / tool_improve / other）；无心得则 []。
10. 调研结束后必须调用 finalize_intraday_decision；orders 可为空。
"""

POSTCLOSE_PROMPT = """你是「自演进投资系统」中的投资 Agent：中长期价值导向，不是短线交易者。

当前是【盘后复盘】Agent Loop（清算已由系统先完成）：
1. 通过工具读取清算日报、当日决策（含 usage_notes）、订单、经验与 CR；禁止编造数据。
2. 正文用简体中文；侧重中长期逻辑与仓位风险，而非日内波段得失。
3. 必须汇总当日决策里的 usage_notes（缺工具/调用失败/需优化），据此写 lessons 或 CR；
   无实质问题可不硬凑。
4. 跟进过往 CR：拒绝理由要读；已完成的可以验收（passed true/false）。
5. 可用 get_macro_digest / get_announcements / get_market_news 补充宏观与披露上下文。
6. 禁止下单；不要直接调用 submit_review / create_change_request。
7. issue_type：NoIssue | DecisionError | DataGap | CapabilityGap | ToolUsageError。
8. CapabilityGap/DataGap 时优先封装 a-stock-data 端点（industry_comparison、board_fund_flow、
   资讯聚合、指数估值等），proposal 写明端点名。
9. 结束后必须调用 finalize_postclose_review。
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


def next_action(session: dict[str, Any]) -> str:
    if session.get("session") == "open":
        return "intraday"
    if session.get("postclose_due") and not session.get("postclose_completed_today"):
        return "postclose"
    return "idle"


def run_once(settings: RunnerSettings | None = None, *, mode: Mode | None = None) -> dict[str, Any]:
    settings = settings or RunnerSettings()
    client = SystemClient(settings.api_base, actor=settings.actor)
    if mode is None:
        session = client.invoke("get_market_session", {})
        inferred = next_action(session)
        mode = "postclose" if inferred == "postclose" else "intraday"
    if mode == "postclose":
        return run_postclose(client, settings)
    return run_intraday(client, settings)


def _orders_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("orders")
    if not isinstance(raw, list):
        raw = []
    if payload.get("place_order") and payload.get("symbol"):
        raw = [
            {
                "symbol": payload.get("symbol"),
                "side": payload.get("side") or "buy",
                "quantity": payload.get("quantity") or 100,
            },
            *raw,
        ]
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip()
        side = str(item.get("side") or "buy").lower()
        try:
            qty = float(item.get("quantity") or 100)
        except (TypeError, ValueError):
            continue
        if not symbol or side not in {"buy", "sell"} or qty <= 0:
            continue
        key = (symbol, side)
        if key in seen:
            continue
        seen.add(key)
        out.append({"symbol": symbol, "side": side, "quantity": qty})
    return out


def _bare_code(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())[-6:]


def compact_candidates(rows: list[Any], held: list[str], limit: int = 30) -> list[dict[str, Any]]:
    held_set = {_bare_code(c) for c in held if _bare_code(c)}
    out: list[dict[str, Any]] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        code = _bare_code(row.get("code"))
        if not code:
            continue
        out.append(
            {
                "code": code,
                "name": row.get("name") or code,
                "price": row.get("price"),
                "change_pct": row.get("change_pct"),
                "amount_wan": row.get("amount_wan"),
                "held": code in held_set,
            }
        )
    return out


def run_intraday(client: SystemClient, settings: RunnerSettings) -> dict[str, Any]:
    caps = client.list_capabilities()
    run = client.invoke(
        "start_agent_run",
        {"trigger": "intraday", "notes": "盘中 agent loop（工具自选→finalize→落库）"},
    )
    run_id = run["agent_run_id"]
    client.begin_run(run_id)
    try:
        return _run_intraday_body(client, settings, caps, run_id)
    finally:
        client.end_run()


def _run_intraday_body(
    client: SystemClient,
    settings: RunnerSettings,
    capabilities: list[dict[str, Any]],
    run_id: int,
) -> dict[str, Any]:
    tool_trace: list[dict[str, Any]] = []
    decision_payload: dict[str, Any] = {}

    if settings.openai_api_key:
        llm = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        decision_payload, tool_trace = run_intraday_agent_loop(
            client=client,
            llm=llm,
            model=settings.openai_model,
            system_prompt=INTRADAY_PROMPT,
            capabilities=capabilities,
            stock_type=settings.stock_type,
            max_turns=settings.intraday_max_tool_turns,
            extract_json=_extract_json_object,
        )
    else:
        # 无 LLM：最小可读路径，仍走 finalize 字段默认值
        portfolio = client.invoke("get_portfolio", {})
        experiences = client.invoke("list_experiences", {"limit": 10})
        tool_trace = [
            {"tool": "get_portfolio", "fallback": True},
            {"tool": "list_experiences", "fallback": True},
        ]
        decision_payload = {
            "observation": "无 LLM，仅拉取组合与经验后观望。",
            "thesis": "等待模型可用后再做中长期配置判断。",
            "decision_summary": "观望，不交易",
            "hypothesis": "无模型，不假设",
            "action_plan": "不下单",
            "orders": [],
            "usage_notes": [
                {
                    "kind": "other",
                    "content": "本轮未启用 LLM，无法产出实质工具使用心得。",
                }
            ],
            "_fallback_context": {
                "portfolio": portfolio,
                "experiences": experiences.get("items", [])[:5],
            },
        }

    observation = decision_payload.get("observation") or (
        "本轮 Agent Loop 完成；无明确交易信号，观望。"
    )
    thesis = decision_payload.get("thesis") or "遵循中长期价值经验，等待更清晰信号。"
    summary = decision_payload.get("decision_summary") or "观望，不交易"
    hypothesis = decision_payload.get("hypothesis") or "等待大盘与主题相对强弱更明确"
    action_plan = decision_payload.get("action_plan") or "不下单"
    usage_notes = decision_payload.get("usage_notes") or []
    if not isinstance(usage_notes, list):
        usage_notes = []
    orders = _orders_from_payload(decision_payload)

    obs = client.invoke(
        "submit_observation",
        {
            "content": observation,
            "data": {
                "tool_trace": tool_trace,
                "usage_notes": usage_notes,
                "agent_loop": True,
            },
        },
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
            "usage_notes": usage_notes,
            "observation_id": obs["observation_id"],
            "thesis_id": th["thesis_id"],
            "agent_run_id": run_id,
        },
    )
    order_results = []
    for order in orders:
        order_results.append(
            client.invoke(
                "submit_order",
                {
                    "symbol": order["symbol"],
                    "side": order["side"],
                    "quantity": order["quantity"],
                    "decision_id": dec["decision_id"],
                    "agent_run_id": run_id,
                },
            )
        )
    client.invoke(
        "finish_agent_run",
        {"agent_run_id": run_id, "status": "completed", "notes": "盘中 agent loop 完成"},
    )
    return {
        "mode": "intraday",
        "agent_run_id": run_id,
        "observation_id": obs["observation_id"],
        "thesis_id": th["thesis_id"],
        "decision_id": dec["decision_id"],
        "orders": order_results,
        "tool_trace": tool_trace,
        "llm_used": bool(settings.openai_api_key),
    }


def run_postclose(client: SystemClient, settings: RunnerSettings) -> dict[str, Any]:
    caps = client.list_capabilities()
    run = client.invoke(
        "start_agent_run",
        {"trigger": "postclose", "notes": "盘后先清算再复盘 agent loop"},
    )
    run_id = run["agent_run_id"]
    client.begin_run(run_id)
    try:
        return _run_postclose_body(client, settings, caps, run_id)
    finally:
        client.end_run()


def _run_postclose_body(
    client: SystemClient,
    settings: RunnerSettings,
    capabilities: list[dict[str, Any]],
    run_id: int,
) -> dict[str, Any]:
    # 1) 固定清算：估值 + 快照 + 每日清算记录（日初/日终）
    settlement = client.invoke("settle_day", {"agent_run_id": run_id})
    tool_trace: list[dict[str, Any]] = [{"tool": "settle_day", "phase": "settlement"}]
    decision_payload: dict[str, Any] = {}

    # 2) 复盘 agent loop（LLM 自选工具）
    if settings.openai_api_key:
        llm = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        decision_payload, review_trace = run_postclose_agent_loop(
            client=client,
            llm=llm,
            model=settings.openai_model,
            system_prompt=POSTCLOSE_PROMPT,
            capabilities=capabilities,
            settlement=settlement,
            max_turns=settings.postclose_max_tool_turns,
            extract_json=_extract_json_object,
        )
        tool_trace.extend(review_trace)
    else:
        report = client.invoke("get_day_report", {})
        tool_trace.append({"tool": "get_day_report", "fallback": True})
        decision_payload = {
            "review_content": (
                f"无 LLM。清算完成：日盈亏 {settlement.get('day_pnl')}，"
                f"权益 {settlement.get('equity')}。详见日报。"
            ),
            "issue_type": "NoIssue",
            "lessons": [],
            "create_change_request": False,
            "_fallback_report": report,
        }

    issue_type = decision_payload.get("issue_type") or "NoIssue"
    if issue_type not in {
        "NoIssue",
        "DecisionError",
        "DataGap",
        "CapabilityGap",
        "ToolUsageError",
    }:
        issue_type = "NoIssue"
    review_content = decision_payload.get("review_content") or (
        f"盘后复盘完成。清算日盈亏 {settlement.get('day_pnl')}，本日无新增经验或变更请求。"
    )
    lessons = decision_payload.get("lessons") or []
    create_cr = bool(decision_payload.get("create_change_request", False))
    review_body: dict[str, Any] = {
        "issue_type": issue_type,
        "content": review_content,
        "agent_run_id": run_id,
        "create_change_request": create_cr,
        "lessons": lessons,
    }
    if create_cr and decision_payload.get("change_request"):
        review_body["change_request"] = decision_payload["change_request"]
    review = client.invoke("submit_review", review_body)

    verifications = []
    for item in decision_payload.get("verifications") or []:
        if not isinstance(item, dict) or "change_request_id" not in item:
            continue
        verifications.append(
            client.invoke(
                "verify_change_request",
                {
                    "change_request_id": item["change_request_id"],
                    "passed": bool(item.get("passed")),
                    "evidence": item.get("evidence") or "",
                },
            )
        )

    client.invoke(
        "finish_agent_run",
        {"agent_run_id": run_id, "status": "completed", "notes": "盘后清算+复盘完成"},
    )
    return {
        "mode": "postclose",
        "agent_run_id": run_id,
        "settlement": settlement,
        "review": review,
        "verifications": verifications,
        "tool_trace": tool_trace,
        "llm_used": bool(settings.openai_api_key),
    }


def run_serve(settings: RunnerSettings | None = None) -> None:
    settings = settings or RunnerSettings()
    client = SystemClient(settings.api_base, actor=settings.actor)
    while True:
        try:
            session = client.invoke("get_market_session", {})
            action = next_action(session)
            if action == "intraday":
                run_intraday(client, settings)
                time.sleep(max(60, int(settings.intraday_interval_seconds)))
            elif action == "postclose":
                run_postclose(client, settings)
                time.sleep(30)
            else:
                time.sleep(20)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"agent-runner serve error: {exc}", flush=True)
            time.sleep(20)
