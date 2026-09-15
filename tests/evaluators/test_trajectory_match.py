"""TrajectoryMatcher 测试。

## 两个正交维度

**维度 1 — 轨迹模式**：`strict` / `unordered` / `subset` / `superset` / `in_order`

命名以 LangChain agentevals 为准（`strict`/`unordered`/`subset`/`superset`），
另加 `in_order`（有序子序列，来自 agentv 项目，实践中很有用）。
注意**不是** exact / in-order / any-order —— 那套命名是另一个项目的。

**维度 2 — 参数匹配**：`exact` / `ignore` / `subset` / `superset`

两个维度正交：可以"顺序严格但参数忽略"，也可以"顺序任意但参数全等"。

## `arg_normalizers` 是必须实现项而非可选项

代码修复任务里命令串、文件路径、时间戳天然不确定。
没有 per-tool 的归一化，整套匹配会极其脆弱 ——
临时工作目录前缀一变，golden 就永远匹配不上。
"""

from __future__ import annotations

import pytest

from harness.contracts.protocols import EvalContext
from harness.contracts.results import EvalStatus
from harness.evaluators.trajectory_match import TrajectoryMatcher
from harness.testing import TrajectoryBuilder as TB


def _traj(*tools: str):
    b = TB(run_id="r1").turn()
    for i, t in enumerate(tools):
        b = b.llm_response(tool_calls=[(t, {}, f"c{i}")]).tool_result(
            name=t, content="ok", ok=True)
    return b.run_end().build()


def _run(traj, **kw):
    return TrajectoryMatcher(**kw).evaluate(traj, EvalContext())


# ---- 模式 ----
def test_strict_requires_the_exact_sequence():
    expected = [("a", {}), ("b", {})]
    assert _run(_traj("a", "b"), mode="strict", expected=expected).status is EvalStatus.PASS
    assert _run(_traj("b", "a"), mode="strict", expected=expected).status is EvalStatus.FAIL


def test_strict_fails_on_extra_calls():
    r = _run(_traj("a", "b", "c"), mode="strict", expected=[("a", {}), ("b", {})])
    assert r.status is EvalStatus.FAIL
    assert r.findings[0].code == "trajectory.length_mismatch"


def test_unordered_ignores_order_but_not_the_set():
    expected = [("a", {}), ("b", {})]
    assert _run(_traj("b", "a"), mode="unordered",
                expected=expected).status is EvalStatus.PASS
    assert _run(_traj("a"), mode="unordered", expected=expected).status is EvalStatus.FAIL


def test_superset_allows_extra_exploration():
    """允许探索性多余调用 —— 代码修复任务最常见的情况。"""
    r = _run(_traj("search", "read", "write"), mode="superset",
             expected=[("read", {}), ("write", {})])
    assert r.status is EvalStatus.PASS


def test_superset_still_requires_all_expected_tools():
    r = _run(_traj("search", "read"), mode="superset",
             expected=[("read", {}), ("write", {})])
    assert r.status is EvalStatus.FAIL
    assert r.findings[0].code == "trajectory.missing_tool"


def test_subset_is_an_allowlist():
    """白名单语义：代理只能调 expected 里有的工具，多一个都不行。"""
    assert _run(_traj("read"), mode="subset",
                expected=[("read", {}), ("write", {})]).status is EvalStatus.PASS
    r = _run(_traj("search", "read"), mode="subset", expected=[("read", {})])
    assert r.status is EvalStatus.FAIL
    assert r.findings[0].code == "trajectory.unexpected_tool"


def test_in_order_requires_an_ordered_subsequence():
    expected = [("read", {}), ("write", {})]
    assert _run(_traj("search", "read", "write", "finish"), mode="in_order",
                expected=expected).status is EvalStatus.PASS
    assert _run(_traj("write", "read"), mode="in_order",
                expected=expected).status is EvalStatus.FAIL


def test_unknown_mode_raises_at_construction():
    """配置错误在构造期暴露，不是跑到一半才炸。"""
    with pytest.raises(ValueError, match="mode"):
        TrajectoryMatcher(mode="bogus", expected=[])


def test_unknown_arg_match_mode_raises():
    with pytest.raises(ValueError, match="tool_args_match_mode"):
        TrajectoryMatcher(mode="strict", expected=[], tool_args_match_mode="bogus")


# ---- 指标 ----
def test_tool_recall_and_precision_are_reported():
    r = _run(_traj("a", "x"), mode="superset", expected=[("a", {}), ("b", {})])
    assert r.metrics["tool_recall"] == 0.5
    assert r.metrics["tool_precision"] == 0.5


def test_perfect_match_reports_full_metrics():
    r = _run(_traj("a", "b"), mode="strict", expected=[("a", {}), ("b", {})])
    assert r.metrics["tool_recall"] == 1.0
    assert r.metrics["tool_precision"] == 1.0
    assert r.score == 1.0


# ---- 参数匹配维度 ----
def _one_call(name: str, args: dict, expected_args: dict, **kw):
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[(name, args, "c1")])
            .tool_result(name=name, content="ok", ok=True)
            .run_end().build())
    return TrajectoryMatcher(mode="strict", expected=[(name, expected_args)],
                             **kw).evaluate(traj, EvalContext())


def test_args_exact_requires_equal_arguments():
    assert _one_call("f", {"a": 1}, {"a": 1}).status is EvalStatus.PASS
    assert _one_call("f", {"a": 1}, {"a": 2}).status is EvalStatus.FAIL


def test_args_ignore_compares_names_only():
    r = _one_call("run_command", {"argv": ["pytest", "-q"]},
                  {"argv": ["pytest", "-x"]}, tool_args_match_mode="ignore")
    assert r.status is EvalStatus.PASS


def test_args_subset_allows_fewer_actual_keys():
    r = _one_call("f", {"a": 1}, {"a": 1, "b": 2}, tool_args_match_mode="subset")
    assert r.status is EvalStatus.PASS


def test_args_superset_allows_extra_actual_keys():
    r = _one_call("f", {"a": 1, "b": 2}, {"a": 1}, tool_args_match_mode="superset")
    assert r.status is EvalStatus.PASS


# ---- arg_normalizers ----
def test_normalizer_handles_nondeterministic_paths():
    """★ 临时工作目录前缀必须能被归一化掉，否则 golden 永远匹配不上。"""
    r = _one_call("read_file", {"path": "/tmp/ws_abc123/a.py"}, {"path": "a.py"},
                  arg_normalizers={"read_file": lambda a: {"path": a["path"].rsplit("/", 1)[-1]}})
    assert r.status is EvalStatus.PASS


def test_normalizer_applies_to_expected_side_too():
    """两侧都要归一化 —— 只归一化实际值会让 expected 永远对不上。"""
    r = _one_call("read_file", {"path": "/tmp/ws_1/a.py"}, {"path": "/tmp/ws_2/a.py"},
                  arg_normalizers={"read_file": lambda a: {"path": a["path"].rsplit("/", 1)[-1]}})
    assert r.status is EvalStatus.PASS


def test_normalizer_is_per_tool():
    """归一化按工具名分派 —— 不同工具的参数结构不同。"""
    r = _one_call("f", {"path": "X/a.py"}, {"path": "a.py"},
                  arg_normalizers={"other_tool": lambda a: {}})
    assert r.status is EvalStatus.FAIL  # f 没有归一化器，保持原样


# ---- 退化输入 ----
def test_empty_trajectory_does_not_crash():
    r = _run(TB(run_id="r1").build(), mode="strict", expected=[("a", {})])
    assert r.status in {EvalStatus.FAIL, EvalStatus.SKIPPED}
    assert r.status is not EvalStatus.ERROR


def test_empty_expected_with_empty_actual_passes():
    r = _run(TB(run_id="r1").build(), mode="strict", expected=[])
    assert r.status in {EvalStatus.PASS, EvalStatus.SKIPPED}


def test_truncated_trajectory_does_not_crash():
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("a", {}, "c1")])
            .tool_result(name="a", content="x", ok=True)
            .build())  # 无 run_end
    assert _run(traj, mode="strict",
                expected=[("a", {})]).status is not EvalStatus.ERROR


# ---- 元信息 ----
def test_evaluator_name_is_stable():
    assert TrajectoryMatcher(mode="strict", expected=[]).name == "TrajectoryMatcher"


def test_subscribes_declares_the_events_it_needs():
    from harness.events.types import EventType

    assert EventType.TOOL_CALL in TrajectoryMatcher.subscribes


def test_findings_carry_evidence():
    r = _run(_traj("a"), mode="strict", expected=[("a", {}), ("b", {})])
    assert r.findings
    assert r.findings[0].severity
