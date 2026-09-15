"""FakeProvider 测试。

它是 M1 端到端路径**完全离线**的关键：没有它，垂直切片就得依赖真实 API，
测试会变慢、变贵、不确定。同时它记录所有收到的请求，供上层断言 payload。
"""

from __future__ import annotations

import pytest

from harness.contracts.protocols import LLMRequest, LLMResponse, Message
from harness.providers.fake import FakeProvider, text_response, tool_call_response


def _req(text: str = "hi") -> LLMRequest:
    return LLMRequest(model="fake", messages=[Message.user_text(text)])


async def test_returns_scripted_responses_in_order():
    p = FakeProvider([text_response("one"), text_response("two")])
    assert (await p.complete(_req())).text == "one"
    assert (await p.complete(_req())).text == "two"


async def test_raises_when_script_exhausted():
    p = FakeProvider([text_response("only")])
    await p.complete(_req())
    with pytest.raises(IndexError, match="script exhausted"):
        await p.complete(_req())


async def test_records_all_requests_for_assertion():
    p = FakeProvider([text_response("x")])
    await p.complete(_req("hello"))
    assert len(p.requests) == 1
    assert p.requests[0].messages[0].content[0]["text"] == "hello"


async def test_callable_script_can_branch_on_tool_results():
    """失败重试路径的测试需要按工具结果分支。"""

    def script(req: LLMRequest) -> LLMResponse:
        saw_error = any("ERROR" in str(m.content) for m in req.messages)
        return text_response("recovered" if saw_error else "retry")

    p = FakeProvider(script)
    assert (await p.complete(_req())).text == "retry"


async def test_tool_call_response_carries_call_id_and_finish_reason():
    r = tool_call_response("read_file", {"path": "a.py"}, call_id="c9")
    assert r.tool_calls[0].call_id == "c9"
    assert r.tool_calls[0].name == "read_file"
    assert r.finish_reason == "tool_calls"


async def test_text_response_has_stop_finish_reason():
    assert text_response("x").finish_reason == "stop"


async def test_responses_carry_usage():
    assert text_response("x").usage.input_tokens > 0
    assert tool_call_response("f", {}).usage.calls == 1


async def test_aclose_is_idempotent():
    p = FakeProvider([])
    await p.aclose()
    await p.aclose()


def test_provider_name_is_stable():
    assert FakeProvider([]).name == "fake"
