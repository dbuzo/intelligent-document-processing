"""The MCP host loop with a scripted model: tool routing, approval gate, and result threading."""

import asyncio
from types import SimpleNamespace as NS

from mcp import types as mcp_types

from idp.host.chat_host import needs_approval, run_turn, to_claude_tools

READ = mcp_types.Tool(name="search_documents", description="search", input_schema={"type": "object"},
                      annotations=mcp_types.ToolAnnotations(read_only_hint=True))
WRITE = mcp_types.Tool(name="submit_review", description="review", input_schema={"type": "object"},
                       annotations=mcp_types.ToolAnnotations(destructive_hint=True))


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


class FakeSession:
    def __init__(self):
        self.called = []

    async def call_tool(self, name, args):
        self.called.append(name)
        return NS(content=[NS(type="text", text='{"hits": []}')], is_error=False)


def _tool_use(name, id_="t1"):
    return NS(stop_reason="tool_use", content=[NS(type="tool_use", id=id_, name=name, input={"q": "x"})])


def _final(text):
    return NS(stop_reason="end_turn", content=[NS(type="text", text=text)])


def test_tool_conversion_and_approval_policy():
    assert to_claude_tools([READ])[0] == {"name": "search_documents", "description": "search",
                                          "input_schema": {"type": "object"}}
    assert not needs_approval(READ) and needs_approval(WRITE)


def test_read_only_tool_runs_without_approval():
    llm, session = ScriptedLLM([_tool_use("search_documents"), _final("none found")]), FakeSession()
    messages = [{"role": "user", "content": "hi"}]
    out = asyncio.run(run_turn(llm, "m", session, [READ, WRITE], messages, approve=lambda *_: False))
    assert out == "none found" and session.called == ["search_documents"]
    # user, assistant(tool_use), user(tool_result), assistant(final)
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    tool_result = messages[2]["content"][0]
    assert tool_result["type"] == "tool_result" and not tool_result["is_error"]


def test_write_tool_blocked_when_user_declines():
    llm, session = ScriptedLLM([_tool_use("submit_review"), _final("ok, not approved")]), FakeSession()
    messages = [{"role": "user", "content": "approve it"}]
    asyncio.run(run_turn(llm, "m", session, [READ, WRITE], messages, approve=lambda *_: False))
    assert session.called == []
    assert messages[2]["content"][0]["is_error"] is True


def test_refusal_is_handled():
    llm = ScriptedLLM([NS(stop_reason="refusal", content=[])])
    out = asyncio.run(run_turn(llm, "m", FakeSession(), [READ], [{"role": "user", "content": "x"}]))
    assert "declined" in out
