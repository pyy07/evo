"""盘中 Agent Loop：LLM 多轮调用系统 Capability，最终 finalize 决策。"""

from __future__ import annotations

from typing import Any, Callable

from openai import OpenAI

from agent_runner.client import SystemClient
from agent_runner.tool_loop import build_capability_tools, run_capability_agent_loop

INTRADAY_CAPABILITY_TOOLS = frozenset(
    {
        "list_capabilities",
        "get_market_session",
        "get_market_overview",
        "get_industry_ranking",
        "get_board_fund_flow",
        "get_index_valuation",
        "get_market_breadth",
        "get_market_news",
        "get_announcements",
        "get_macro_digest",
        "get_market_snapshot",
        "get_etf_history",
        "get_trading_calendar",
        "list_universe",
        "screen_market",
        "get_portfolio",
        "list_experiences",
        "list_change_requests",
    }
)

FINALIZE_TOOL_NAME = "finalize_intraday_decision"

FINALIZE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": FINALIZE_TOOL_NAME,
        "description": (
            "结束本轮调研并提交最终盘中决策与工具使用心得。"
            "多数轮可观望（orders 为空）。卖出不得超过 sellable_quantity。"
            "usage_notes 供盘后复盘汇总经验与 CR，无心得可给空数组。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "observation": {"type": "string", "description": "市场观察（简体中文）"},
                "thesis": {"type": "string", "description": "投资论点（简体中文）"},
                "decision_summary": {"type": "string", "description": "决策摘要"},
                "hypothesis": {"type": "string", "description": "假设"},
                "action_plan": {"type": "string", "description": "行动计划"},
                "orders": {
                    "type": "array",
                    "description": "可选订单；无把握则为空数组",
                    "items": {
                        "type": "object",
                        "properties": {
                            "symbol": {"type": "string"},
                            "side": {"type": "string", "enum": ["buy", "sell"]},
                            "quantity": {"type": "number"},
                        },
                        "required": ["symbol", "side", "quantity"],
                    },
                },
                "usage_notes": {
                    "type": "array",
                    "description": (
                        "本轮工具使用心得：缺什么能力、哪些调用失败/难用、哪些需优化。"
                        "kind=missing_tool|tool_error|tool_improve|other；无则 []。"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": [
                                    "missing_tool",
                                    "tool_error",
                                    "tool_improve",
                                    "other",
                                ],
                            },
                            "capability_id": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["kind", "content"],
                    },
                },
            },
            "required": [
                "observation",
                "thesis",
                "decision_summary",
                "hypothesis",
                "action_plan",
                "usage_notes",
            ],
        },
    },
}


def build_openai_tools(capabilities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return build_capability_tools(capabilities, INTRADAY_CAPABILITY_TOOLS, FINALIZE_TOOL)


def run_intraday_agent_loop(
    *,
    client: SystemClient,
    llm: OpenAI,
    model: str,
    system_prompt: str,
    capabilities: list[dict[str, Any]],
    stock_type: str,
    max_turns: int = 8,
    extract_json: Callable[[str], dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return run_capability_agent_loop(
        client=client,
        llm=llm,
        model=model,
        system_prompt=system_prompt,
        user_message=(
            f"本轮盘中决策。资产类型={stock_type}。\n"
            "请先 list_experiences 与 get_portfolio；看大盘优先 get_market_overview"
            "（指数+估值+行业/概念+资金流+涨跌分布）。"
            "突发/情绪可用 get_market_news（大盘快讯；持仓或候选可 scope=stock/both+codes）；"
            "核对披露用 get_announcements；宏观日程用 get_macro_digest（盘中可选）。"
            "估值用 get_index_valuation（勿用 ETF 快照 pe_ttm=0），"
            "K 线用 get_etf_history 取最近 10–20 根，看返回的 latest。\n"
            "再按经验决定是否 screen_market/list_universe，以及对哪些代码拉快照/K线。\n"
            "个股/ETF 报价代码只能来自工具返回或持仓，不要使用固定主题 ETF 清单。\n"
            "finalize 时务必填写 usage_notes（缺工具/调用失败/需优化等；没有就 []），"
            "供盘后复盘沉淀经验与 CR。\n"
            "完成后必须调用 finalize_intraday_decision；"
            "不要直接调用 submit_observation/submit_decision/submit_order。"
        ),
        capabilities=capabilities,
        allowed_tools=INTRADAY_CAPABILITY_TOOLS,
        finalize_tool=FINALIZE_TOOL,
        max_turns=max_turns,
        extract_json=extract_json,
        force_finalize_hint="轮次已达上限，请立即调用 finalize_intraday_decision 给出本轮结论（可观望）。",
    )
