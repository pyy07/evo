"""盘后复盘 Agent Loop：先由 runner 清算，再由 LLM 用工具读日报/决策后 finalize。"""

from __future__ import annotations

from typing import Any, Callable

from openai import OpenAI

from agent_runner.client import SystemClient
from agent_runner.tool_loop import build_capability_tools, run_capability_agent_loop

POSTCLOSE_CAPABILITY_TOOLS = frozenset(
    {
        "list_capabilities",
        "get_market_session",
        "get_day_report",
        "list_settlements",
        "list_today_decisions",
        "list_today_orders",
        "get_portfolio",
        "get_etf_history",
        "get_market_snapshot",
        "get_index_valuation",
        "get_market_breadth",
        "get_market_overview",
        "get_market_news",
        "get_announcements",
        "get_macro_digest",
        "list_experiences",
        "list_change_requests",
    }
)

FINALIZE_TOOL_NAME = "finalize_postclose_review"

FINALIZE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": FINALIZE_TOOL_NAME,
        "description": (
            "结束盘后复盘并提交结论。调用前应已查阅清算日报与当日决策；"
            "经验/新 CR/验收均可省略。CapabilityGap 时优先引用 a-stock-data 端点。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "review_content": {"type": "string", "description": "复盘正文（简体中文）"},
                "issue_type": {
                    "type": "string",
                    "enum": [
                        "NoIssue",
                        "DecisionError",
                        "DataGap",
                        "CapabilityGap",
                        "ToolUsageError",
                    ],
                },
                "lessons": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": ["investment", "agent", "system"],
                            },
                            "content": {"type": "string"},
                        },
                        "required": ["content"],
                    },
                },
                "create_change_request": {"type": "boolean"},
                "change_request": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "problem": {"type": "string"},
                        "evidence": {"type": "string"},
                        "proposal": {"type": "string"},
                        "expected_benefit": {"type": "string"},
                        "issue_type": {
                            "type": "string",
                            "enum": ["DataGap", "CapabilityGap"],
                        },
                    },
                },
                "verifications": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "change_request_id": {"type": "integer"},
                            "passed": {"type": "boolean"},
                            "evidence": {"type": "string"},
                        },
                        "required": ["change_request_id", "passed"],
                    },
                },
            },
            "required": ["review_content", "issue_type"],
        },
    },
}


def build_openai_tools(capabilities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return build_capability_tools(capabilities, POSTCLOSE_CAPABILITY_TOOLS, FINALIZE_TOOL)


def run_postclose_agent_loop(
    *,
    client: SystemClient,
    llm: OpenAI,
    model: str,
    system_prompt: str,
    capabilities: list[dict[str, Any]],
    settlement: dict[str, Any],
    max_turns: int = 8,
    extract_json: Callable[[str], dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    settle_summary = {
        "date": settlement.get("date"),
        "snapshot_id": settlement.get("snapshot_id"),
        "equity": settlement.get("equity"),
        "day_pnl": settlement.get("day_pnl"),
        "day_pnl_pct": settlement.get("day_pnl_pct"),
        "position_count": len(settlement.get("positions") or []),
        "decision_count": settlement.get("decision_count"),
        "order_count": settlement.get("order_count"),
        "trade_count": settlement.get("trade_count"),
    }
    return run_capability_agent_loop(
        client=client,
        llm=llm,
        model=model,
        system_prompt=system_prompt,
        user_message=(
            "日终清算已完成（摘要如下）。请先用工具核对清算日报与当日决策，再做中长期复盘。\n"
            f"清算摘要：{settle_summary}\n"
            "重点阅读 list_today_decisions / get_day_report 中每条决策的 usage_notes"
            "（missing_tool / tool_error / tool_improve），据此沉淀 lessons 或提出 CR。\n"
            "建议工具：get_day_report、list_today_decisions、list_today_orders、"
            "list_experiences、list_change_requests；"
            "宏观与联播用 get_macro_digest，持仓披露用 get_announcements，"
            "当日资讯可用 get_market_news。\n"
            "完成后必须调用 finalize_postclose_review；"
            "不要直接调用 submit_review / create_change_request。"
        ),
        capabilities=capabilities,
        allowed_tools=POSTCLOSE_CAPABILITY_TOOLS,
        finalize_tool=FINALIZE_TOOL,
        max_turns=max_turns,
        extract_json=extract_json,
        force_finalize_hint="轮次已达上限，请立即调用 finalize_postclose_review 给出复盘结论。",
    )
