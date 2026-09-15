"""agent loop 与 Run 抽象测试。

这是 M1 的核心：把事件模型、Trajectory、Tool、store、loop 串成一条可运行路径。

三条必须守住的契约：
  1. `seq` 严格单调从 0 开始 —— db 与回放都依赖它
  2. 工具失败不崩 run（未知工具、中间件异常都要转成 ToolResult）
  3. 终止语义清晰：finish → OK，纯文本 → NO_FINISH，轮次耗尽 → MAX_TURNS
"""

from __future__ import annotations

from typing import Any

from harness.contracts.protocols import ToolCall, ToolResult
from harness.contracts.spec import Budget, ModelRef, RunRole, RunSpec, RunStatus, ToolPolicy
from harness.core.registry import ToolRegistry
from harness.core.run import Run, RunDeps
from harness.core.tools.finish import FinishTool
from harness.events.types import EventType
from harness.providers.fake import FakeProvider, text_response, tool_call_response
from harness.store.jsonl import JsonlStore


class _EchoTool:
    """回显工具 —— 用于验证工具结果被喂回下一次请求。"""

    name = "echo"

    @property
    def description(self) -> str:
        return "echoes"

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "parameters": {"type": "object", "properties": {}}}

    async def invoke(self, call: ToolCall, ws: Any) -> ToolResult:
        return ToolResult(call_id=call.call_id, name=self.name, ok=True,
                          content=f"echo:{call.arguments.get('msg', '')}")


class _BoomTool:
    name = "boom"

    @property
    def description(self) -> str:
        return "always fails"

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "parameters": {"type": "object", "properties": {}}}

    async def invoke(self, call: ToolCall, ws: Any) -> ToolResult:
        raise RuntimeError("tool exploded")


def _deps(provider: Any, tmp_path: Any, extra_tools: list[Any] | None = None) -> RunDeps:
    reg = ToolRegistry()
    reg.register(FinishTool())
    for t in (extra_tools or []):
        reg.register(t)
    return RunDeps(provider=provider, store=JsonlStore(tmp_path), tools=reg)


def _spec(budget: Budget | None = None, *, allow: list[str] | None = None) -> RunSpec:
    return RunSpec(
        role=RunRole.SUT,
        system_prompt="sys",
        model=ModelRef(provider="fake", model="fake"),
        tools=ToolPolicy(allow=allow),
        budget=budget or Budget(max_turns=5),
    )


# ---- 终止语义 ----
async def test_finish_terminates_with_ok(tmp_path):
    p = FakeProvider([tool_call_response("finish", {"summary": "done"})])
    result = await Run(_spec(), _deps(p, tmp_path)).execute()
    assert result.status is RunStatus.OK
    assert result.final_output == "done"
    assert result.turns == 1


async def test_text_only_response_terminates_immediately_as_no_finish(tmp_path):
    """模型返回纯文本 = 它停止行动了。

    不继续消费剩余轮次 —— 再问下去模型也不会调 finish，只是白烧 token。
    """
    p = FakeProvider([text_response("I think it's done")] * 5)
    result = await Run(_spec(Budget(max_turns=3)), _deps(p, tmp_path)).execute()
    assert result.status is RunStatus.NO_FINISH
    assert result.turns == 1
    assert len(p.requests) == 1


async def test_tool_calls_without_finish_exhaust_turns_as_max_turns(tmp_path):
    """持续调工具但从不 finish —— 与 NO_FINISH 是不同的失败模式。"""
    p = FakeProvider([tool_call_response("echo", {"msg": "x"})] * 5)
    result = await Run(_spec(Budget(max_turns=3)), _deps(p, tmp_path, [_EchoTool()])).execute()
    assert result.status is RunStatus.MAX_TURNS
    assert result.turns == 3


# ---- 事件流 ----
async def test_trajectory_contains_the_full_event_stream(tmp_path):
    p = FakeProvider([tool_call_response("finish", {"summary": "done"})])
    result = await Run(_spec(), _deps(p, tmp_path)).execute()
    types = {e.type for e in result.trajectory.events}
    assert {
        EventType.RUN_START, EventType.TURN_START, EventType.LLM_REQUEST,
        EventType.LLM_RESPONSE, EventType.TOOL_CALL, EventType.TOOL_RESULT,
        EventType.RUN_END,
    } <= types


async def test_seq_is_strictly_monotonic_from_zero(tmp_path):
    p = FakeProvider([
        tool_call_response("echo", {"msg": "a"}, call_id="c1"),
        tool_call_response("finish", {"summary": "d"}, call_id="c2"),
    ])
    result = await Run(_spec(), _deps(p, tmp_path, [_EchoTool()])).execute()
    seqs = [e.seq for e in result.trajectory.events]
    assert seqs == list(range(len(seqs)))


async def test_run_start_carries_spec_snapshot(tmp_path):
    """评测器不该需要回查 suite 配置才能理解一次 run。"""
    p = FakeProvider([tool_call_response("finish", {"summary": "d"})])
    result = await Run(_spec(), _deps(p, tmp_path)).execute()
    start = result.trajectory.start()
    assert start is not None
    assert start.role == "sut"
    assert "system_prompt" in start.spec_json


async def test_run_end_records_usage_and_counts(tmp_path):
    p = FakeProvider([tool_call_response("finish", {"summary": "d"})])
    result = await Run(_spec(), _deps(p, tmp_path)).execute()
    end = result.trajectory.end()
    assert end is not None
    assert end.status == RunStatus.OK.value
    assert end.tool_calls == 1
    assert end.input_tokens > 0


# ---- 工具结果回喂 ----
async def test_tool_result_is_fed_back_to_the_next_request(tmp_path):
    p = FakeProvider([
        tool_call_response("echo", {"msg": "ping"}, call_id="c1"),
        tool_call_response("finish", {"summary": "d"}, call_id="c2"),
    ])
    await Run(_spec(), _deps(p, tmp_path, [_EchoTool()])).execute()
    assert len(p.requests) == 2
    tool_msgs = [m for m in p.requests[1].messages if m.role == "tool"]
    assert tool_msgs and "echo:ping" in tool_msgs[0].content[0]["content"]


# ---- 失败不崩 ----
async def test_unknown_tool_becomes_a_failed_result_not_a_crash(tmp_path):
    p = FakeProvider([
        tool_call_response("does_not_exist", {}, call_id="c1"),
        tool_call_response("finish", {"summary": "d"}, call_id="c2"),
    ])
    result = await Run(_spec(), _deps(p, tmp_path)).execute()
    failed = [r for r in result.trajectory.tool_results() if not r.ok]
    assert len(failed) == 1
    assert failed[0].error_type == "unknown_tool"


async def test_tool_exception_becomes_a_failed_result_not_a_crash(tmp_path):
    p = FakeProvider([
        tool_call_response("boom", {}, call_id="c1"),
        tool_call_response("finish", {"summary": "d"}, call_id="c2"),
    ])
    result = await Run(_spec(), _deps(p, tmp_path, [_BoomTool()])).execute()
    failed = [r for r in result.trajectory.tool_results() if not r.ok]
    assert len(failed) == 1
    assert failed[0].error_type == "sandbox_error"
    assert "exploded" in (failed[0].error or "")


# ---- 轨迹落盘 ----
async def test_trajectory_persists_to_store(tmp_path):
    p = FakeProvider([tool_call_response("finish", {"summary": "d"})])
    deps = _deps(p, tmp_path)
    result = await Run(_spec(), deps).execute()
    stored = await deps.store.get(result.run_id)
    assert len(stored.events) == len(result.trajectory.events)
    assert (tmp_path / f"{result.run_id}.jsonl").exists()


async def test_dependency_injection_allows_swapping_the_provider(tmp_path):
    """Run 不认识任何具体 provider/store —— 这是能用 Fake 跑端到端的前提。"""
    provider = FakeProvider([tool_call_response("finish", {"summary": "d"})])
    deps = _deps(provider, tmp_path)
    result = await Run(_spec(), deps).execute()
    assert result.status is RunStatus.OK
    assert deps.provider is provider
