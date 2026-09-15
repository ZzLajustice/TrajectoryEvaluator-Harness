"""TrajectoryBuilder 测试。

## 它作为产品的一部分发布

不是测试私有工具 —— 用户写自己的评测器时同样需要构造事件序列。
「评测器可独立单测」这个卖点，兑现方式就是它。

## 它替用户挡掉的两个坑

1. **seq 与 call_id 配对**：手写轨迹最容易错的地方，错了会让评测器
   看到悬空配对，而症状是"评测结果不对"而非报错。
2. **畸形序列的注入**：测评测器的健壮性需要故意构造坏数据
   （悬空 call、缺 RUN_END、乱序 seq），`raw_emit` 提供这个能力。
"""

from __future__ import annotations

from harness.events.types import EventType
from harness.testing import TrajectoryBuilder as TB


def test_builder_produces_a_well_formed_trajectory():
    traj = (TB(run_id="r1", task="fix bug")
            .turn()
            .llm_response(text="looking", tool_calls=[("read_file", {"path": "a.py"})])
            .tool_result(name="read_file", content="code", ok=True)
            .turn()
            .llm_response(tool_calls=[("finish", {"summary": "done"})])
            .tool_result(name="finish", content="done", ok=True)
            .run_end(status="ok")
            .build())
    assert traj.tool_sequence() == ("read_file", "finish")
    assert traj.status == "ok"


def test_seq_is_auto_assigned_and_contiguous():
    traj = (TB(run_id="r1").turn().llm_response(text="a")
            .turn().llm_response(text="b").run_end().build())
    assert [e.seq for e in traj.events] == list(range(len(traj.events)))


def test_tool_result_auto_pairs_with_the_last_unmatched_call():
    """手写轨迹最容易错的地方 —— 自动配对把它变成不可能出错。"""
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("read_file", {"path": "a.py"})])
            .tool_result(name="read_file", content="x", ok=True)
            .run_end().build())
    call = traj.tool_calls()[0]
    assert traj.result_for(call.call_id).content == "x"  # type: ignore[union-attr]


def test_explicit_call_id_is_respected():
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("f", {}, "custom_id")])
            .tool_result(name="f", content="x", ok=True, call_id="custom_id")
            .run_end().build())
    assert traj.tool_calls()[0].call_id == "custom_id"


def test_parallel_tool_calls_pair_in_order():
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("a", {}, "c1"), ("b", {}, "c2")])
            .tool_result(name="a", content="ra", ok=True)
            .tool_result(name="b", content="rb", ok=True)
            .run_end().build())
    assert traj.result_for("c1").content == "ra"  # type: ignore[union-attr]
    assert traj.result_for("c2").content == "rb"  # type: ignore[union-attr]


def test_failed_tool_result_records_error_type():
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("f", {})])
            .tool_result(name="f", content="", ok=False, error="boom",
                         error_type="nonzero_exit")
            .run_end().build())
    r = traj.tool_results()[0]
    assert r.ok is False
    assert r.error_type == "nonzero_exit"


def test_denied_result_records_the_denier():
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("f", {})])
            .tool_result(name="f", content="", ok=False, denied_by="permission")
            .run_end().build())
    assert traj.tool_results()[0].denied_by == "permission"


# ---- 退化输入 ----
def test_builder_can_omit_run_end():
    """退化输入：截断的轨迹。评测器必须不崩。"""
    traj = TB(run_id="r1").turn().llm_response(text="a").build()
    assert traj.end() is None
    assert traj.status == "unknown"


def test_empty_trajectory_can_be_built():
    traj = TB(run_id="r1").build()
    assert traj.tool_calls() == ()


def test_raw_emit_allows_dangling_calls():
    """测评测器健壮性必须能注入坏数据 —— 悬空 call。"""
    traj = (TB(run_id="r1")
            .raw_emit(EventType.TOOL_CALL, call_id="dangling", name="f", arguments={})
            .run_end().build())
    assert traj.tool_calls()[0].call_id == "dangling"
    assert traj.result_for("dangling") is None


def test_raw_emit_allows_a_compaction_event():
    traj = (TB(run_id="r1").turn().llm_response(text="a")
            .raw_emit(EventType.CONTEXT_COMPACT, reason="token_pressure",
                      messages_before=20, messages_after=6,
                      tokens_before=9000, tokens_after=3000,
                      dropped_message_digests=["d1", "d2"],
                      strategy="drop_oldest_groups")
            .run_end().build())
    assert len(traj.compactions()) == 1


def test_raw_emit_allows_a_budget_event():
    traj = (TB(run_id="r1")
            .raw_emit(EventType.BUDGET_EVENT, dimension="turns", limit=10,
                      consumed=10, action="terminate")
            .run_end(status="max_turns").build())
    assert traj.of(EventType.BUDGET_EVENT)


# ---- 截断标记（与 LocalExecutor 的输出格式对齐）----
def test_truncated_flag_is_auto_detected_from_the_marker():
    """与 `LocalExecutor._cap()` 的输出格式对齐。

    对不齐的话，GroundingChecker 的截断分支永远触发不了，是死代码。
    """
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("run_command", {"argv": ["pytest"]})])
            .tool_result(name="run_command",
                         content="head\n... [truncated] ...\ntail", ok=True)
            .run_end().build())
    assert traj.tool_results()[0].truncated is True


def test_truncated_flag_can_be_set_explicitly():
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("f", {})])
            .tool_result(name="f", content="full", ok=True, truncated=True)
            .run_end().build())
    assert traj.tool_results()[0].truncated is True


def test_untruncated_content_is_not_flagged():
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("f", {})])
            .tool_result(name="f", content="normal output", ok=True)
            .run_end().build())
    assert traj.tool_results()[0].truncated is False


# ---- 其它 ----
def test_task_is_recorded_on_run_start():
    traj = TB(run_id="r1", task="fix the widget").build()
    assert traj.start().task == "fix the widget"  # type: ignore[union-attr]


def test_role_can_be_overridden_for_judge_trajectories():
    """Judge 的轨迹也是用同一个 builder 构造的 —— 对称性在工具层就开始。"""
    traj = TB(run_id="j1", role="judge").build()
    assert traj.start().role == "judge"  # type: ignore[union-attr]


def test_run_end_records_usage_fields():
    traj = TB(run_id="r1").run_end(status="ok", final_output="done").build()
    end = traj.end()
    assert end is not None
    assert end.status == "ok"
    assert end.final_output == "done"
