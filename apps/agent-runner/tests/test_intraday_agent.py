from types import SimpleNamespace

from agent_runner.intraday_agent import (
    FINALIZE_TOOL_NAME,
    INTRADAY_CAPABILITY_TOOLS,
    build_openai_tools,
    run_intraday_agent_loop,
)


def test_build_openai_tools_filters_and_adds_finalize():
    caps = [
        {
            "id": "get_portfolio",
            "name": "组合",
            "description": "查组合",
            "input_schema": {"type": "object", "properties": {"agent_run_id": {"type": "integer"}}},
        },
        {
            "id": "submit_order",
            "name": "下单",
            "description": "不应出现",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "id": "get_market_snapshot",
            "name": "快照",
            "description": "行情",
            "input_schema": {
                "type": "object",
                "properties": {"codes": {"type": "array"}, "agent_run_id": {"type": "integer"}},
                "required": ["codes", "agent_run_id"],
            },
        },
    ]
    tools = build_openai_tools(caps)
    names = [t["function"]["name"] for t in tools]
    assert "get_portfolio" in names
    assert "get_market_snapshot" in names
    assert "submit_order" not in names
    assert names[-1] == FINALIZE_TOOL_NAME
    snap = next(t for t in tools if t["function"]["name"] == "get_market_snapshot")
    assert "agent_run_id" not in snap["function"]["parameters"]["properties"]
    assert "agent_run_id" not in (snap["function"]["parameters"].get("required") or [])
    assert set(names[:-1]) <= INTRADAY_CAPABILITY_TOOLS


class _FakeFn:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, tid: str, name: str, arguments: str) -> None:
        self.id = tid
        self.function = _FakeFn(name, arguments)


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message) -> None:
        self.message = message


class _FakeCompletion:
    def __init__(self, message) -> None:
        self.choices = [_FakeChoice(message)]


class _FakeCompletions:
    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._script:
            raise AssertionError("unexpected LLM call")
        return self._script.pop(0)


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class _FakeLLM:
    def __init__(self, script: list) -> None:
        self.chat = _FakeChat(_FakeCompletions(script))


class _FakeClient:
    def __init__(self) -> None:
        self.invokes: list[tuple[str, dict]] = []

    def invoke(self, capability_id: str, payload=None):
        body = dict(payload or {})
        self.invokes.append((capability_id, body))
        if capability_id == "list_experiences":
            return {"items": [{"content": "[风格] 中长期"}]}
        if capability_id == "get_portfolio":
            return {"cash": 1e6, "positions": []}
        return {"ok": True, "capability": capability_id}


def test_agent_loop_tools_then_finalize():
    client = _FakeClient()
    script = [
        _FakeCompletion(
            _FakeMessage(
                tool_calls=[
                    _FakeToolCall("1", "list_experiences", '{"limit": 5}'),
                    _FakeToolCall("2", "get_portfolio", "{}"),
                ]
            )
        ),
        _FakeCompletion(
            _FakeMessage(
                tool_calls=[
                    _FakeToolCall(
                        "3",
                        FINALIZE_TOOL_NAME,
                        '{"observation":"观望","thesis":"中长期","decision_summary":"不下单",'
                        '"hypothesis":"无","action_plan":"观望","orders":[]}',
                    )
                ]
            )
        ),
    ]
    llm = _FakeLLM(script)
    caps = [
        {"id": "list_experiences", "description": "经验", "input_schema": {"type": "object", "properties": {}}},
        {"id": "get_portfolio", "description": "组合", "input_schema": {"type": "object", "properties": {}}},
    ]
    decision, trace = run_intraday_agent_loop(
        client=client,
        llm=llm,
        model="fake",
        system_prompt="sys",
        capabilities=caps,
        stock_type="etf",
        max_turns=5,
        extract_json=lambda _t: {},
    )
    assert decision["decision_summary"] == "不下单"
    assert [c for c, _ in client.invokes] == ["list_experiences", "get_portfolio"]
    assert any(t.get("tool") == FINALIZE_TOOL_NAME for t in trace)
    assert len(llm.chat.completions.calls) == 2
