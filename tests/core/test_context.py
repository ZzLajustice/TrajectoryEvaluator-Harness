"""ContextManager 测试（M1 最小版）。

任务 10 只实现消息累积与请求构造；压缩逻辑在任务 22 补齐。
接口从一开始就完整 —— `needs_compaction()` 固定返回 False，`compact()` 返回 None，
这样任务 22 只需替换实现、**不动 loop**。
"""

from __future__ import annotations

from harness.contracts.protocols import LLMResponse, ToolCall, ToolResult, Usage
from harness.core.context import ContextManager, estimate_tokens


def _resp(text: str, calls: list[ToolCall] | None = None) -> LLMResponse:
    return LLMResponse(
        model="m", content=[{"type": "text", "text": text}], text=text,
        tool_calls=calls or [], finish_reason="stop",
        usage=Usage(), latency_ms=0,
    )


def _cm(budget: int = 100_000) -> ContextManager:
    return ContextManager(system_prompt="you are a coder", token_budget=budget)


def test_system_prompt_is_always_first():
    msgs = _cm().build_request(turn=0).messages
    assert msgs[0].role == "system"
    assert msgs[0].content[0]["text"] == "you are a coder"


def test_assistant_message_is_appended():
    cm = _cm()
    cm.append_assistant(_resp("hello"))
    assert cm.build_request(turn=1).messages[-1].content[0]["text"] == "hello"


def test_tool_result_is_appended_as_tool_role():
    cm = _cm()
    cm.append_tool_result(ToolResult(call_id="c1", name="f", ok=True, content="out"))
    last = cm.build_request(turn=1).messages[-1]
    assert last.role == "tool"
    assert last.content[0]["tool_call_id"] == "c1"


def test_failed_tool_result_is_marked_inline():
    """错误必须在内容里可见 —— agent 得知道它失败了才可能修正。"""
    cm = _cm()
    cm.append_tool_result(ToolResult(call_id="c1", name="f", ok=False, error="boom"))
    assert "boom" in cm.build_request(turn=1).messages[-1].content[0]["content"]


def test_message_order_is_preserved():
    cm = _cm()
    cm.append_assistant(_resp("first"))
    cm.append_tool_result(ToolResult(call_id="c1", name="f", ok=True, content="r"))
    cm.append_assistant(_resp("second"))
    roles = [m.role for m in cm.build_request(turn=2).messages]
    assert roles == ["system", "assistant", "tool", "assistant"]


def test_digest_changes_with_turn_and_history():
    cm = _cm()
    d0 = cm.build_request(turn=0).digest
    cm.append_assistant(_resp("x"))
    assert cm.build_request(turn=1).digest != d0


def test_needs_compaction_is_false_in_m1():
    """M1 不做压缩 —— 接口存在但固定返回 False，这样 loop 不必改。"""
    cm = _cm(budget=1)
    for _ in range(50):
        cm.append_assistant(_resp("y" * 200))
    assert cm.needs_compaction() is False


def test_compact_returns_none_in_m1():
    assert _cm(budget=1).compact() is None


def test_estimated_tokens_is_deterministic():
    """同样的内容必须给同样的估算 —— 否则金轨迹与重放会漂移。"""
    a, b = _cm(), _cm()
    a.append_assistant(_resp("same text"))
    b.append_assistant(_resp("same text"))
    assert a.estimated_tokens() == b.estimated_tokens()


def test_estimated_tokens_grows_with_content():
    cm = _cm()
    before = cm.estimated_tokens()
    cm.append_assistant(_resp("x" * 400))
    assert cm.estimated_tokens() > before


def test_estimate_tokens_handles_cjk():
    """中文密度不同 —— 用同一套字符/token 比会低估。"""
    assert estimate_tokens("中" * 100) > estimate_tokens("a" * 100) / 2
    assert estimate_tokens("") == 0
