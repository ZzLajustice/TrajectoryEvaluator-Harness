"""值对象与 Protocol 的测试。

「评测器不依赖 core」这条架构约束的落点：**评测器需要的每一个类型都在 contracts/ 里**。
本文件验证这些类型本身可用，且 Protocol 是结构化子类型（runtime_checkable），
便于单测注入假实现。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from harness.contracts.protocols import (
    DirEntry,
    EvalContext,
    JudgeCase,
    JudgeVerdict,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Message,
    Middleware,
    ProcessResult,
    Tool,
    ToolCall,
    ToolResult,
    Usage,
)
from harness.contracts.results import (
    EvalResult,
    EvalStatus,
    EvidenceRef,
    Finding,
    Severity,
)


# ---- 值对象 ----
def test_usage_addition_is_associative():
    a = Usage(input_tokens=1, cost_usd=0.1, calls=1)
    b = Usage(input_tokens=2, cost_usd=0.2, calls=1)
    c = Usage()
    assert (a + b) + c == a + (b + c)
    assert (a + b).input_tokens == 3
    assert (a + b).cost_usd == pytest.approx(0.3)


def test_tool_result_marks_denial_source():
    r = ToolResult(call_id="c1", name="run_command", ok=False, denied_by="permission")
    assert r.denied_by == "permission"
    assert r.truncated is False


def test_message_factories_build_normalized_blocks():
    u = Message.user_text("hi")
    assert u.role == "user" and u.content[0]["text"] == "hi"
    t = Message.tool_result("c1", "out", ok=True)
    assert t.role == "tool"
    assert t.content[0]["tool_call_id"] == "c1"
    assert t.content[0]["content"] == "out"


def test_message_tool_result_marks_errors_inline():
    """错误也走同一条 content 通道 —— GroundingChecker 依赖原文可见。"""
    t = Message.tool_result("c1", "boom", ok=False)
    assert "boom" in t.content[0]["content"]


def test_llm_response_carries_raw_for_lossless_replay():
    r = LLMResponse(
        model="m", content=[], text="", tool_calls=[], finish_reason="stop",
        usage=Usage(), latency_ms=5, raw={"id": "chatcmpl-1"},
    )
    assert r.raw["id"] == "chatcmpl-1"


def test_process_result_is_plain_dataclass():
    p = ProcessResult(stdout="out", stderr="", returncode=0)
    assert p.timed_out is False and p.truncated is False


def test_dir_entry_holds_metadata():
    e = DirEntry(name="a.py", is_dir=False, size=12)
    assert e.name == "a.py" and e.size == 12


# ---- 结果模型 ----
def test_finding_carries_evidence_back_to_event_seq():
    f = Finding(
        code="grounding.fabricated_result",
        message="claimed 12 passed",
        severity=Severity.CRITICAL,
        evidence=[EvidenceRef(seq=7, note="assistant claim")],
    )
    assert f.evidence[0].seq == 7


def test_finding_defaults_to_minor_severity():
    assert Finding(code="x", message="y").severity is Severity.MINOR


def test_eval_error_status_is_distinct_from_fail():
    """评测器抛异常与判定失败必须分开。

    ERROR 表示评测器自己有 bug，FAIL 表示被测 agent 有问题。
    混淆会让「评测器崩了」被记成「agent 失败了」，污染整份报告。
    """
    assert EvalStatus.ERROR != EvalStatus.FAIL
    assert {s.value for s in EvalStatus} == {"pass", "fail", "warn", "skipped", "error"}


def test_eval_result_score_defaults_to_none_meaning_not_applicable():
    r = EvalResult(evaluator="E", run_id="r1", status=EvalStatus.SKIPPED)
    assert r.score is None
    assert r.findings == []


def test_result_models_reject_extra_fields():
    with pytest.raises(ValidationError):
        Finding(code="x", message="y", bogus=1)  # type: ignore[call-arg]


# ---- Judge 侧值对象（依赖倒置的接口面）----
def test_judge_verdict_points_back_to_its_own_run():
    """◀ 这是元评测能工作的关键：judge 判定要能回溯到它自己的轨迹。"""
    v = JudgeVerdict(verdict="fail", score=0.0, rationale="never verified",
                     judge_run_id="j1")
    assert v.judge_run_id == "j1"
    assert v.verdict in {"pass", "fail", "partial", "uncertain"}


def test_judge_case_bundles_what_the_judge_needs():
    from harness.events.trajectory import Trajectory

    c = JudgeCase(case_id="c1", task="fix bug",
                  traj=Trajectory.from_events("r1", []), rubric="rate 0-1")
    assert c.traj.run_id == "r1" and c.rubric


def test_eval_context_defaults_to_no_judge():
    """纯规则评测器不需要 judge —— EvalContext 必须允许它为 None。"""
    ctx = EvalContext()
    assert ctx.judge is None and ctx.store is None


# ---- Protocol 是结构化子类型 ----
def test_tool_protocol_is_structural():
    class FakeTool:
        name = "t"

        @property
        def description(self) -> str:
            return "d"

        def schema(self) -> dict:
            return {}

        async def invoke(self, call: ToolCall, ws: object) -> ToolResult: ...

    assert isinstance(FakeTool(), Tool)


def test_llm_provider_protocol_is_structural():
    class FakeProvider:
        name = "fake"

        async def complete(self, req: LLMRequest) -> LLMResponse: ...

        async def aclose(self) -> None: ...

    assert isinstance(FakeProvider(), LLMProvider)


def test_middleware_protocol_is_structural():
    class FakeMw:
        name = "mw"

        async def handle(self, ctx: object, nxt: object) -> ToolResult: ...

    assert isinstance(FakeMw(), Middleware)
