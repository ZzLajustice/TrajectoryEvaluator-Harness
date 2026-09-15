"""Trajectory 只读视图测试。

轨迹是评测器与被测 agent 之间**唯一的交互面**，因此它的契约必须严格：
  - 返回不可变集合，评测器改不了轨迹
  - 按 call_id 查 ToolResult 是 O(1)（GroundingChecker 会频繁调用）
  - JSONL 往返无损
"""

from __future__ import annotations

from harness.events.trajectory import Trajectory
from harness.events.types import (
    EventType,
    RunEndEvent,
    RunStartEvent,
    ToolCallEvent,
    ToolResultEvent,
)


def _traj() -> Trajectory:
    return Trajectory.from_events("r1", [
        RunStartEvent(run_id="r1", seq=0, type=EventType.RUN_START,
                      role="sut", model="m", provider="fake", task="fix it"),
        ToolCallEvent(run_id="r1", seq=1, type=EventType.TOOL_CALL,
                      call_id="c1", name="read_file", arguments={"path": "a.py"}),
        ToolResultEvent(run_id="r1", seq=2, type=EventType.TOOL_RESULT,
                        call_id="c1", name="read_file", ok=True, content="X"),
        RunEndEvent(run_id="r1", seq=3, type=EventType.RUN_END,
                    status="ok", turns=1, tool_calls=1),
    ])


def test_tool_sequence_returns_names_in_order():
    assert _traj().tool_sequence() == ("read_file",)


def test_result_for_is_looked_up_by_call_id():
    assert _traj().result_for("c1").content == "X"  # type: ignore[union-attr]


def test_result_for_missing_call_id_returns_none():
    assert _traj().result_for("nope") is None


def test_accessors_return_tuples_not_lists():
    """返回 tuple 而非 list —— 强制评测器不改轨迹。"""
    t = _traj()
    assert isinstance(t.tool_calls(), tuple)
    assert isinstance(t.tool_results(), tuple)
    assert isinstance(t.events, tuple)


def test_jsonl_roundtrip():
    t = _traj()
    back = Trajectory.from_jsonl(t.to_jsonl())
    assert back.tool_sequence() == ("read_file",)
    assert back.events == t.events


def test_indexes_are_built_at_construction():
    """slots=True 下不能事后挂属性 —— 索引必须由 from_events 经构造参数传入。"""
    t = _traj()
    assert t.result_for("c1") is not None
    # 重复查询命中同一对象（说明走的是索引而非线性扫描）
    assert t.result_for("c1") is t.result_for("c1")


def test_status_and_final_output_derived_from_run_end():
    t = _traj()
    assert t.status == "ok"
    assert t.end() is not None
    assert t.end().turns == 1  # type: ignore[union-attr]


def test_end_returns_none_when_run_end_absent():
    """退化输入：截断的轨迹。评测器必须不崩。"""
    t = Trajectory.from_events("r1", [
        RunStartEvent(run_id="r1", seq=0, type=EventType.RUN_START,
                      role="sut", model="m", provider="fake"),
    ])
    assert t.end() is None


def test_empty_trajectory_is_valid():
    t = Trajectory.from_events("r1", [])
    assert t.events == ()
    assert t.tool_sequence() == ()
    assert t.result_for("anything") is None


def test_of_filters_by_event_type():
    t = _traj()
    assert len(t.of(EventType.TOOL_CALL)) == 1
    assert len(t.of(EventType.TOOL_CALL, EventType.TOOL_RESULT)) == 2
    assert t.of(EventType.CONTEXT_COMPACT) == ()


def test_run_id_is_preserved():
    assert _traj().run_id == "r1"
