"""通用 Capability tool-calling agent loop。"""

from __future__ import annotations

import json
from typing import Any, Callable

from openai import OpenAI

from agent_runner.client import SystemClient


def json_schema_for_openai(schema: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(schema or {"type": "object", "properties": {}})
    raw.setdefault("type", "object")
    props = dict(raw.get("properties") or {})
    props.pop("agent_run_id", None)
    required = [r for r in (raw.get("required") or []) if r != "agent_run_id"]
    out: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


def build_capability_tools(
    capabilities: list[dict[str, Any]],
    allowed: frozenset[str],
    finalize_tool: dict[str, Any],
) -> list[dict[str, Any]]:
    by_id = {c.get("id"): c for c in capabilities if c.get("id")}
    tools: list[dict[str, Any]] = []
    for cap_id in sorted(allowed):
        cap = by_id.get(cap_id)
        if not cap:
            continue
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": cap_id,
                    "description": (cap.get("description") or cap.get("name") or cap_id)[:400],
                    "parameters": json_schema_for_openai(cap.get("input_schema")),
                },
            }
        )
    tools.append(finalize_tool)
    return tools


def truncate_result(result: Any, limit: int = 6000) -> str:
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "...(truncated)"


def parse_tool_args(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def message_to_dict(message: Any) -> dict[str, Any]:
    tool_calls = getattr(message, "tool_calls", None) or []
    payload: dict[str, Any] = {
        "role": "assistant",
        "content": message.content or None,
    }
    if tool_calls:
        payload["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments or "{}",
                },
            }
            for tc in tool_calls
        ]
    return payload


def run_capability_agent_loop(
    *,
    client: SystemClient,
    llm: OpenAI,
    model: str,
    system_prompt: str,
    user_message: str,
    capabilities: list[dict[str, Any]],
    allowed_tools: frozenset[str],
    finalize_tool: dict[str, Any],
    max_turns: int = 8,
    extract_json: Callable[[str], dict[str, Any]],
    force_finalize_hint: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    finalize_name = finalize_tool["function"]["name"]
    tools = build_capability_tools(capabilities, allowed_tools, finalize_tool)
    tool_names = {t["function"]["name"] for t in tools}
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    trace: list[dict[str, Any]] = []
    decision: dict[str, Any] = {}

    for _turn in range(max(1, max_turns)):
        completion = llm.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=0.2,
        )
        choice = completion.choices[0].message
        messages.append(message_to_dict(choice))
        tool_calls = list(choice.tool_calls or [])

        if not tool_calls:
            decision = extract_json(choice.content or "")
            if decision:
                trace.append({"event": "final_text", "keys": list(decision.keys())})
            break

        finished = False
        for tc in tool_calls:
            name = tc.function.name
            args = parse_tool_args(tc.function.arguments)
            if name == finalize_name:
                decision = args
                trace.append({"tool": name, "input": args, "output": {"ok": True}})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(
                            {"status": "accepted", "message": "已接收，本轮结束"},
                            ensure_ascii=False,
                        ),
                    }
                )
                finished = True
                continue

            if name not in tool_names or name not in allowed_tools:
                err = {"error": f"不允许调用工具：{name}"}
                trace.append({"tool": name, "input": args, "output": err})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": truncate_result(err),
                    }
                )
                continue

            try:
                result = client.invoke(name, args)
                trace.append(
                    {
                        "tool": name,
                        "input": args,
                        "output_preview": truncate_result(result, 500),
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": truncate_result(result),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                err = {"error": str(exc)}
                trace.append({"tool": name, "input": args, "output": err})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": truncate_result(err),
                    }
                )

        if finished:
            break
    else:
        hint = force_finalize_hint or f"轮次已达上限，请立即调用 {finalize_name} 给出结论。"
        messages.append({"role": "user", "content": hint})
        completion = llm.chat.completions.create(
            model=model,
            messages=messages,
            tools=[finalize_tool],
            tool_choice={"type": "function", "function": {"name": finalize_name}},
            temperature=0.1,
        )
        choice = completion.choices[0].message
        for tc in choice.tool_calls or []:
            if tc.function.name == finalize_name:
                decision = parse_tool_args(tc.function.arguments)
                trace.append({"tool": finalize_name, "input": decision, "forced": True})
                break
        if not decision:
            decision = extract_json(choice.content or "")

    return decision, trace
