"""EfficiencyAnalyzer 测试。

**纯规则、零 LLM 成本。** 效率指标最大的价值不是"打分"，
而是提供**不需要判断力的**客观量：走了多少步、重复调了几次、失败几次。
这些数字不会因为 judge 的心情而变。
"""

from __future__ import annotations

from harness.contracts.protocols import EvalContext
from harness.contracts.results import EvalStatus
from harness.evaluators.efficiency import EfficiencyAnalyzer
from harness.testing import TrajectoryBuilder as TB


def _traj(steps: list[tuple[str, dict]]):
    b = TB(run_id="r1").turn()
    for i, (name, args) in enumerate(steps):
        b = b.llm_response(tool_calls=[(name, args, f"c{i}")]).tool_result(
            name=name, content="ok", ok=True)
    return b.run_end().build()


def _run(traj, **kw):
    return EfficiencyAnalyzer(**kw).evaluate(traj, EvalContext())


def test_step_ratio_is_one_for_the_optimal_path():
    traj = _traj([("read", {}), ("write", {}), ("test", {}), ("finish", {})])
    r = _run(traj, optimal_steps=4)
    assert r.metrics["step_ratio"] == 1.0
    assert r.status is EvalStatus.PASS


def test_step_ratio_grows_with_waste():
    traj = _traj([("read", {}) for _ in range(12)])
    r = _run(traj, optimal_steps=4)
    assert r.metrics["step_ratio"] == 3.0


def test_excessive_steps_emit_a_finding():
    traj = _traj([("read", {}) for _ in range(20)])
    r = _run(traj, optimal_steps=4)
    assert any(f.code == "efficiency.excessive_steps" for f in r.findings)
    assert r.status is EvalStatus.WARN


def test_zero_optimal_steps_does_not_divide_by_zero():
    r = _run(_traj([("a", {})]), optimal_steps=0)
    assert r.status is not EvalStatus.ERROR
    assert r.metrics["step_ratio"] == 0.0


# ---- 冗余检测 ----
def test_redundant_calls_are_counted():
    """同工具 + 同参数重复调用 —— 循环检测的基础。"""
    traj = _traj([("read_file", {"path": "a.py"})] * 4)
    r = _run(traj, optimal_steps=4)
    assert r.metrics["redundant_calls"] == 3


def test_repeated_calls_with_different_args_are_not_redundant():
    traj = _traj([("read_file", {"path": f"{c}.py"}) for c in "abc"])
    r = _run(traj, optimal_steps=3)
    assert r.metrics["redundant_calls"] == 0


def test_redundancy_tolerance_is_configurable():
    """阈值可配 —— 连续两次重试是合理的，十次不是。"""
    traj = _traj([("f", {})] * 3)
    assert not any(f.code == "efficiency.redundant_calls"
                   for f in _run(traj, redundancy_tolerance=5).findings)
    assert any(f.code == "efficiency.redundant_calls"
               for f in _run(traj, redundancy_tolerance=2).findings)


# ---- 失败计数 ----
def test_failed_tool_calls_are_counted():
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("f", {}, "c1")])
            .tool_result(name="f", content="", ok=False, error="boom",
                         error_type="nonzero_exit")
            .run_end().build())
    r = _run(traj, optimal_steps=1)
    assert r.metrics["failed_tool_calls"] == 1


def test_denied_calls_count_as_failed():
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("f", {}, "c1")])
            .tool_result(name="f", content="", ok=False, denied_by="permission")
            .run_end().build())
    assert _run(traj, optimal_steps=1).metrics["failed_tool_calls"] == 1


# ---- 退化输入 ----
def test_empty_trajectory_does_not_crash():
    r = _run(TB(run_id="r1").build(), optimal_steps=4)
    assert r.status is not EvalStatus.ERROR


def test_truncated_trajectory_does_not_crash():
    traj = TB(run_id="r1").turn().llm_response(tool_calls=[("a", {}, "c1")]).build()
    assert _run(traj, optimal_steps=2).status is not EvalStatus.ERROR


# ---- 元信息 ----
def test_name_and_subscriptions():
    from harness.events.types import EventType

    e = EfficiencyAnalyzer(optimal_steps=1)
    assert e.name == "EfficiencyAnalyzer"
    assert EventType.TOOL_CALL in e.subscribes


def test_tool_call_count_is_reported():
    r = _run(_traj([("a", {}), ("b", {})]), optimal_steps=2)
    assert r.metrics["tool_calls"] == 2
