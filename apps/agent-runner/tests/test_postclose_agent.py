from agent_runner.postclose_agent import (
    FINALIZE_TOOL_NAME,
    POSTCLOSE_CAPABILITY_TOOLS,
    build_openai_tools,
    run_postclose_agent_loop,
)


def test_postclose_tools_include_settlement_readers():
    caps = [
        {"id": "get_day_report", "description": "日报", "input_schema": {"type": "object", "properties": {}}},
        {"id": "list_today_decisions", "description": "决策", "input_schema": {"type": "object", "properties": {}}},
        {"id": "list_today_orders", "description": "订单", "input_schema": {"type": "object", "properties": {}}},
        {"id": "submit_review", "description": "不应出现", "input_schema": {"type": "object", "properties": {}}},
        {"id": "settle_day", "description": "清算由 runner 先做", "input_schema": {"type": "object", "properties": {}}},
    ]
    tools = build_openai_tools(caps)
    names = [t["function"]["name"] for t in tools]
    assert "get_day_report" in names
    assert "list_today_decisions" in names
    assert "list_today_orders" in names
    assert "submit_review" not in names
    assert "settle_day" not in names  # runner 强制清算，不给模型重复清算
    assert names[-1] == FINALIZE_TOOL_NAME
    assert set(names[:-1]) <= POSTCLOSE_CAPABILITY_TOOLS


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

    def create(self, **kwargs):
        return self._script.pop(0)


class _FakeLLM:
    def __init__(self, script: list) -> None:
        self.chat = type("C", (), {"completions": _FakeCompletions(script)})()


class _FakeClient:
    def invoke(self, capability_id: str, payload=None):
        if capability_id == "get_day_report":
            return {"date": "2026-10-09", "portfolio": {"day_pnl": -12.5}}
        if capability_id == "list_today_decisions":
            return {"items": [{"id": 1, "summary": "观望"}], "count": 1}
        return {"ok": True}


def test_postclose_loop_reads_then_finalizes():
    script = [
        _FakeCompletion(
            _FakeMessage(
                tool_calls=[
                    _FakeToolCall("1", "get_day_report", "{}"),
                    _FakeToolCall("2", "list_today_decisions", "{}"),
                ]
            )
        ),
        _FakeCompletion(
            _FakeMessage(
                tool_calls=[
                    _FakeToolCall(
                        "3",
                        FINALIZE_TOOL_NAME,
                        '{"review_content":"清算正常","issue_type":"NoIssue","lessons":[]}',
                    )
                ]
            )
        ),
    ]
    decision, trace = run_postclose_agent_loop(
        client=_FakeClient(),
        llm=_FakeLLM(script),
        model="fake",
        system_prompt="sys",
        capabilities=[
            {"id": "get_day_report", "description": "d", "input_schema": {"type": "object", "properties": {}}},
            {"id": "list_today_decisions", "description": "d", "input_schema": {"type": "object", "properties": {}}},
        ],
        settlement={"date": "2026-10-09", "day_pnl": -12.5, "equity": 1e6, "positions": []},
        max_turns=5,
        extract_json=lambda _t: {},
    )
    assert decision["issue_type"] == "NoIssue"
    assert any(t.get("tool") == "get_day_report" for t in trace)
    assert any(t.get("tool") == FINALIZE_TOOL_NAME for t in trace)
