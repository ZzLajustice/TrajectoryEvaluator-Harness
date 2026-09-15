"""ContextManager 测试。

## 两条正确性底线（比压缩算法本身重要得多）

1. **任务提示词绝不能被丢。** 丢了它 agent 就忘了自己在干什么，
   而且症状是"模型变笨了"而非报错 —— 极难归因。

2. **压缩后不得出现"有 tool_use 无 tool_result"的悬空配对。**
   OpenAI 兼容端点会**直接报 400**，整个 run 崩掉；
   而如果只丢 tool_result 保留 tool_use，错误信息完全指不到真正的元凶。

第 2 条决定了压缩必须**按整组丢**：一个 assistant 消息加上它引发的所有
tool 结果，要么全留要么全丢。
"""

from __future__ import annotations

from harness.contracts.protocols import LLMResponse, ToolCall, ToolResult, Usage
from harness.core.context import ContextManager, estimate_tokens


def _resp(text: str = "", calls: list[ToolCall] | None = None) -> LLMResponse:
    calls = calls or []
    content = ([{"type": "text", "text": text}] if text else []) + [
        {"type": "tool_use", "id": c.call_id, "name": c.name, "input": c.arguments}
        for c in calls
    ]
    return LLMResponse(model="m", content=content, text=text, tool_calls=calls,
                       finish_reason="tool_calls" if calls else "stop",
                       usage=Usage(), latency_ms=0)


def _turn(cm: ContextManager, n: int, *, filler: int = 200) -> None:
    """塞入 n 个完整轮次（assistant 调用工具 + 工具结果）。"""
    for i in range(n):
        cid = f"c{n}_{i}"
        cm.append_assistant(_resp(f"step {i} " + "x" * filler,
                                  [ToolCall(cid, "read_file", {"path": "a.py"})]))
        cm.append_tool_result(ToolResult(cid, "read_file", ok=True, content="y" * filler))


def _cm(budget: int = 100_000, task: str | None = "fix the bug") -> ContextManager:
    return ContextManager(system_prompt="you are a coder", token_budget=budget, task=task)


# ---- 基础结构 ----
def test_system_prompt_is_always_first():
    msgs = _cm().build_request(turn=0).messages
    assert msgs[0].role == "system"


def test_task_prompt_is_included_as_the_first_user_message():
    """★ 任务必须进上下文 —— 否则 agent 根本不知道要做什么。"""
    msgs = _cm(task="fix the bug").build_request(turn=0).messages
    assert msgs[1].role == "user"
    assert msgs[1].content[0]["text"] == "fix the bug"


def test_no_task_message_when_task_is_absent():
    msgs = _cm(task=None).build_request(turn=0).messages
    assert [m.role for m in msgs] == ["system"]


def test_assistant_message_is_appended():
    cm = _cm()
    cm.append_assistant(_resp("hello"))
    assert cm.build_request(turn=1).messages[-1].content[0]["text"] == "hello"


def test_tool_result_is_appended_as_tool_role():
    cm = _cm()
    cm.append_tool_result(ToolResult("c1", "f", ok=True, content="out"))
    last = cm.build_request(turn=1).messages[-1]
    assert last.role == "tool"
    assert last.content[0]["tool_call_id"] == "c1"


def test_failed_tool_result_is_marked_inline():
    cm = _cm()
    cm.append_tool_result(ToolResult("c1", "f", ok=False, error="boom"))
    assert "boom" in cm.build_request(turn=1).messages[-1].content[0]["content"]


def test_message_order_is_preserved():
    cm = _cm()
    cm.append_assistant(_resp("first"))
    cm.append_tool_result(ToolResult("c1", "f", ok=True, content="r"))
    cm.append_assistant(_resp("second"))
    assert [m.role for m in cm.build_request(turn=2).messages] == [
        "system", "user", "assistant", "tool", "assistant"]


# ---- 压缩触发 ----
def test_no_compaction_needed_under_budget():
    cm = _cm(budget=100_000)
    _turn(cm, 2)
    assert cm.needs_compaction() is False
    assert cm.compact() is None


def test_compaction_triggers_over_budget():
    cm = _cm(budget=200)
    _turn(cm, 10)
    assert cm.needs_compaction() is True


def test_compact_reduces_the_message_count():
    cm = _cm(budget=200)
    _turn(cm, 10)
    before = len(cm.build_request(turn=0).messages)
    event = cm.compact()
    after = len(cm.build_request(turn=0).messages)
    assert event is not None
    assert after < before
    assert event.tokens_after < event.tokens_before


# ---- 压缩的安全性 ----
def test_task_prompt_survives_compaction():
    """★ 丢了任务，agent 就忘了自己在干什么 —— 且症状是"变笨"而非报错。"""
    cm = _cm(budget=200, task="fix the widget bug")
    _turn(cm, 20)
    cm.compact()
    msgs = cm.build_request(turn=0).messages
    assert msgs[0].role == "system"
    assert msgs[1].role == "user"
    assert msgs[1].content[0]["text"] == "fix the widget bug"


def test_compaction_never_breaks_tool_call_pairing():
    """★ 悬空配对会让下一次 API 调用直接 400，且错误信息指不到元凶。"""
    cm = _cm(budget=200)
    _turn(cm, 20)
    cm.compact()
    msgs = cm.build_request(turn=0).messages

    pending: set[str] = set()
    for m in msgs:
        for block in m.content:
            if block.get("type") == "tool_use":
                pending.add(block["id"])
            elif block.get("type") == "tool_result":
                assert block["tool_call_id"] in pending, (
                    f"orphaned tool_result: {block['tool_call_id']} "
                    "— 压缩切断了 tool_use / tool_result 配对")
                pending.discard(block["tool_call_id"])
    assert not pending, f"orphaned tool_use: {pending} — 压缩留下了没有结果的调用"


def test_recent_messages_are_kept():
    """压缩要保尾部 —— 当前正在做的事不能被丢掉。"""
    cm = _cm(budget=200)
    _turn(cm, 20)
    cm.compact()
    texts = [b.get("text", "") for m in cm.build_request(turn=0).messages
             for b in m.content]
    assert any("step 19" in t for t in texts)


# ---- 事件内容 ----
def test_compact_event_records_strategy_and_reason():
    cm = _cm(budget=200)
    _turn(cm, 10)
    event = cm.compact()
    assert event is not None
    assert event.reason == "token_pressure"
    assert event.strategy
    assert event.messages_before > event.messages_after
    assert event.dropped_message_digests


def test_compact_is_idempotent_when_under_budget():
    cm = _cm(budget=100_000)
    _turn(cm, 2)
    assert cm.compact() is None


def test_compaction_actually_frees_space():
    """压缩完必须真的低于预算，否则下一轮又会触发，陷入死循环。"""
    cm = _cm(budget=300)
    _turn(cm, 30)
    cm.compact()
    assert cm.needs_compaction() is False


# ---- token 估算 ----
def test_estimated_tokens_is_deterministic():
    """确定性是 record/replay 字节级一致的前提。"""
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
    assert estimate_tokens("中" * 100) > estimate_tokens("a" * 100) / 2
    assert estimate_tokens("") == 0


def test_digest_changes_with_history():
    cm = _cm()
    d0 = cm.build_request(turn=0).digest
    cm.append_assistant(_resp("x"))
    assert cm.build_request(turn=1).digest != d0
